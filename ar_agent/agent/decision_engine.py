"""
Decision Engine (F-14).

Per-classification decision rules: what action to take and what reply (if any)
to send based on the classification, current invoice state, days overdue, and
dollar amount.

Hard rules enforced here:
  - W9 requests always route to human — never handled autonomously
  - Classification confidence < 0.60 always escalates
  - Dollar threshold (default $5,000) — above this, escalate regardless of category
"""

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from ar_agent.agent.response_classifier import Classification
from ar_agent.state import invoice_db
from ar_agent.templates import email_templates

logger = logging.getLogger(__name__)

def _escalation_threshold_cents() -> int:
    """
    Dollar threshold above which invoices always escalate after first
    follow-up. Read per-call so the /ar threshold Slack command (stored in
    the settings table, shared across processes) takes effect immediately;
    falls back to the AR_ESCALATION_THRESHOLD_USD env var.
    """
    stored = invoice_db.get_setting("escalation_threshold_usd")
    raw = stored or os.environ.get("AR_ESCALATION_THRESHOLD_USD", "5000")
    try:
        return int(float(raw) * 100)
    except (ValueError, TypeError):
        logger.warning("Invalid escalation threshold %r — using $5,000 default", raw)
        return 500_000

# W9 / vendor tax form requests always route to a human (security constraint
# from Brie's process doc). Detected deterministically — never left to the
# classifier alone.
_W9_PATTERN = re.compile(r"\bw[\s-]?9\b|\bvendor\s+tax\s+form\b", re.IGNORECASE)


def is_w9_request(text: str | None) -> bool:
    """Deterministic W9 / vendor tax form detection on raw text."""
    return bool(text and _W9_PATTERN.search(text))


@dataclass
class Action:
    action: str          # "stop_dunning" | "record_p2p" | "draft_reply" | "escalate" | "route_greg" | "no_action"
    draft_template: email_templates.EmailTemplate | None = None
    escalation_reason: str = ""
    escalate_to: list[str] = field(default_factory=list)
    state_updates: dict = field(default_factory=dict)
    notes: str = ""


def decide(classification: dict | Classification, invoice: dict, email_text: str | None = None) -> dict:
    """
    Compute the AR action for a given classification + invoice.

    Args:
        classification : Classification dataclass or dict (from orchestrator tool result)
        invoice        : invoice dict from invoice_db.get_invoice()
        email_text     : raw inbound email text, when available — used for the
                         deterministic W9 guard

    Returns:
        Action dict for the orchestrator to report and execute
    """
    if isinstance(classification, dict):
        category = int(classification.get("category", 0))
        confidence = float(classification.get("confidence", 0.0))
        requires_escalation = classification.get("requires_escalation", False)
        payment_signal = classification.get("payment_signal")
        p2p_details = classification.get("p2p_details")
        category_name = classification.get("category_name", "")
        notes = classification.get("notes", "")
        w9_flagged = bool(classification.get("is_w9_request", False))
    else:
        category = classification.category
        confidence = classification.confidence
        requires_escalation = classification.requires_escalation
        payment_signal = classification.payment_signal
        p2p_details = classification.p2p_details
        category_name = classification.category_name
        notes = classification.notes
        w9_flagged = classification.is_w9_request

    # ── Hard rule: W9 / vendor tax form requests always go to a human ─────────
    # Checked before any category routing, confidence gate, or dollar threshold.
    if w9_flagged or is_w9_request(email_text) or is_w9_request(notes):
        return _to_dict(Action(
            action="escalate",
            escalation_reason="W9 / vendor tax form request — always handled by a human",
            escalate_to=["Leila", "Brie"],
            notes=notes,
        ))

    invoice_id = invoice["invoice_id"]
    account_name = invoice["account_name"]
    amount_cents = invoice["amount_cents"]
    days_overdue = invoice.get("days_overdue", 0)
    payment_link = invoice.get("freshbooks_payment_link") or ""
    invoice_number = invoice_id

    # ── Hard rule: high-value invoices escalate past first follow-up ──────────
    currency = invoice.get("currency", "USD")
    if amount_cents >= _escalation_threshold_cents() and days_overdue > 7:
        return _to_dict(Action(
            action="escalate",
            escalation_reason=f"High-value invoice ({amount_cents/100:,.2f} {currency}) overdue {days_overdue} days",
            escalate_to=["Leila"],
            notes=f"Dollar threshold exceeded. Category was: {category_name}",
        ))

    # ── Escalation flag (set by classifier, includes its confidence gate) ─────
    if requires_escalation:
        return _to_dict(Action(
            action="escalate",
            escalation_reason=f"Low confidence ({confidence:.0%}) or escalation flag on category {category}",
            escalate_to=["Leila", "Brie"],
            notes=notes,
        ))

    # ── Category routing ──────────────────────────────────────────────────────

    if category == 1:
        # Payment confirmation: stop dunning, acknowledge
        invoice_db.pause_cadence(invoice_id, "In-conversation payment confirmation received")
        payment_note = ""
        if payment_signal:
            parts = []
            if payment_signal.get("check_number"):
                parts.append(f"check #{payment_signal['check_number']}")
            if payment_signal.get("wire_reference"):
                parts.append(f"wire ref {payment_signal['wire_reference']}")
            if payment_signal.get("send_date"):
                parts.append(f"sent {payment_signal['send_date']}")
            payment_note = ", ".join(parts)
        invoice_db.add_note(invoice_id, f"Payment confirmation received: {payment_note}", "ar_agent")
        return _to_dict(Action(
            action="stop_dunning",
            state_updates={"payment_signal": payment_signal},
            notes=f"Dunning paused. Signal: {payment_note}. Awaiting FreshBooks confirmation.",
        ))

    if category == 2:
        # P2P commitment: record and acknowledge
        if p2p_details and p2p_details.get("promise_date"):
            invoice_db.record_p2p_commitment(
                invoice_id,
                p2p_details["promise_date"],
                p2p_details.get("payment_method_stated"),
            )
            tmpl = email_templates.p2p_acknowledgment(
                account_name=account_name,
                invoice_number=invoice_number,
                amount=amount_cents / 100,
                promise_date=p2p_details["promise_date"],
                payment_method=p2p_details.get("payment_method_stated") or "",
            )
            return _to_dict(Action(
                action="record_p2p",
                draft_template=tmpl,
                state_updates={"p2p_details": p2p_details},
                notes=f"P2P recorded for {p2p_details['promise_date']}",
            ))
        else:
            # P2P intent but no date — escalate for Brie to clarify
            return _to_dict(Action(
                action="escalate",
                escalation_reason="P2P commitment detected but no specific date extracted",
                escalate_to=["Brie"],
                notes=notes,
            ))

    if category == 3:
        # Dispute → Leila
        return _to_dict(Action(
            action="escalate",
            escalation_reason="Invoice dispute or line-item query",
            escalate_to=["Leila", "Brie"],
            notes=notes,
        ))

    if category == 4:
        # Escalation/cancellation → Leila + Support
        return _to_dict(Action(
            action="escalate",
            escalation_reason="Customer escalation or cancellation request",
            escalate_to=["Leila", "Support (Alex)"],
            notes=notes,
        ))

    if category == 5:
        # Partial payment → Brie to confirm and update FreshBooks
        return _to_dict(Action(
            action="escalate",
            escalation_reason="Partial payment — needs manual verification in FreshBooks",
            escalate_to=["Brie"],
            notes=notes,
        ))

    if category == 6:
        # Portal/PO# request → Leila
        return _to_dict(Action(
            action="escalate",
            escalation_reason="PO number or payment portal submission request",
            escalate_to=["Leila"],
            notes=notes,
        ))

    if category == 7:
        # Routine billing FAQ — agent can handle
        faq_note = notes.lower()
        if "autopay" in faq_note or "auto pay" in faq_note or "automatic" in faq_note:
            tmpl = email_templates.autopay_setup_faq(
                account_name=account_name,
                invoice_number=invoice_number,
            )
        else:
            tmpl = email_templates.payment_instructions_faq(
                account_name=account_name,
                invoice_number=invoice_number,
                amount=amount_cents / 100,
                payment_link=payment_link,
            )
        return _to_dict(Action(
            action="draft_reply",
            draft_template=tmpl,
            notes="Routine billing FAQ — agent handling",
        ))

    if category == 8:
        # Out-of-office — log and continue cadence
        invoice_db.add_note(invoice_id, "Auto-reply or OOO received", "ar_agent")
        return _to_dict(Action(
            action="no_action",
            notes="Out-of-office or no meaningful response — cadence continues",
        ))

    if category == 9:
        # Sales/activation → Greg
        return _to_dict(Action(
            action="route_greg",
            escalation_reason="Sales or account activation inquiry",
            escalate_to=["Greg"],
            notes=notes,
        ))

    # Unknown category
    return _to_dict(Action(
        action="escalate",
        escalation_reason=f"Unknown classification category: {category}",
        escalate_to=["Leila", "Brie"],
        notes=notes,
    ))


def _to_dict(action: Action) -> dict:
    result: dict[str, Any] = {
        "action": action.action,
        "escalation_reason": action.escalation_reason,
        "escalate_to": action.escalate_to,
        "state_updates": action.state_updates,
        "notes": action.notes,
    }
    if action.draft_template:
        result["draft_template"] = {
            "subject": action.draft_template.subject,
            "html_body": action.draft_template.html_body,
            "text_body": action.draft_template.text_body,
            "template_name": action.draft_template.template_name,
        }
    return result
