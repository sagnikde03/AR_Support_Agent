"""
Escalation Rule Engine (F-16).

Routes escalations to the correct human via Slack with full account context.
Every escalation posts a structured Slack message with: account name, amount,
days overdue, classification, reason, and recommended next action.

Routing targets (from spec):
  Leila  — disputes, high-value invoices, portal/PO requests, customer requests person,
            bankruptcy declarations, cancellation/closure
  Brie   — partial payments, P2P ambiguous, daily operations escalations
  Greg   — sales or account activation inquiries (category 9)
  Alex   — support-related questions (cross-agent handoff to Support Agent)
  Chris  — C-level account escalations (definition pending), bankruptcy/legal
"""

import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)


def _slack_webhook() -> str:
    return os.environ.get("SLACK_WEBHOOK_URL", "")


def _slack_bot_token() -> str:
    return os.environ.get("SLACK_BOT_TOKEN", "")


def _ops_channel() -> str:
    return os.environ.get("AR_OPS_SLACK_CHANNEL", "#ar-operations")


def _leila_user_id() -> str:
    return os.environ.get("SLACK_USER_LEILA", "")


def _brie_user_id() -> str:
    return os.environ.get("SLACK_USER_BRIE", "")


def _greg_user_id() -> str:
    return os.environ.get("SLACK_USER_GREG", "")


def _alex_user_id() -> str:
    return os.environ.get("SLACK_USER_ALEX", "")


def _chris_user_id() -> str:
    return os.environ.get("SLACK_USER_CHRIS", "")


_ROUTING_MAP: dict[str, str] = {
    "Leila": _leila_user_id,
    "Brie": _brie_user_id,
    "Greg": _greg_user_id,
    "Alex": _alex_user_id,
    "Support (Alex)": _alex_user_id,
    "Chris": _chris_user_id,
}


def _resolve_mentions(escalate_to: list[str]) -> str:
    mentions = []
    for name in escalate_to:
        uid_fn = _ROUTING_MAP.get(name)
        if uid_fn:
            uid = uid_fn()
            if uid:
                mentions.append(f"<@{uid}>")
                continue
        mentions.append(name)
    return " ".join(mentions) if mentions else "@here"


def _post_slack(message: str, blocks: list | None = None) -> bool:
    """Post to Slack. Returns True only if the message was actually delivered."""
    try:
        webhook = _slack_webhook()
        if webhook:
            payload: dict[str, Any] = {"text": message}
            if blocks:
                payload["blocks"] = blocks
            resp = requests.post(webhook, json=payload, timeout=10)
            return resp.ok
        token = _slack_bot_token()
        if token:
            payload = {"channel": _ops_channel(), "text": message}
            if blocks:
                payload["blocks"] = blocks
            resp = requests.post(
                "https://slack.com/api/chat.postMessage",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            return resp.ok and resp.json().get("ok", False)
        logger.error(
            "No Slack configuration (SLACK_WEBHOOK_URL / SLACK_BOT_TOKEN) — escalation NOT delivered: %s",
            message,
        )
        return False
    except Exception as exc:
        logger.error("Slack post failed: %s — message: %s", exc, message)
        return False


def _format_account_context(invoice: dict) -> str:
    amount = invoice.get("amount_cents", 0) / 100
    days_overdue = invoice.get("days_overdue", 0)
    stage = invoice.get("cadence_stage", 0)
    attempts = invoice.get("attempt_count", 0)
    p2p_date = invoice.get("current_p2p_date")
    non_complier = invoice.get("known_non_complier", False)

    lines = [
        f"*Account:* {invoice.get('account_name', 'Unknown')}",
        f"*Invoice:* {invoice.get('invoice_id', 'N/A')}",
        f"*Amount:* {amount:,.2f} {invoice.get('currency', 'USD')}",
        f"*Days overdue:* {days_overdue}",
        f"*Status:* {invoice.get('status', 'unknown')}",
        f"*Cadence stage:* {stage}",
        f"*Outreach attempts:* {attempts}",
    ]
    if p2p_date:
        lines.append(f"*P2P date on record:* {p2p_date}")
    if non_complier:
        lines.append("*Known non-complier:* Yes")
    return "\n".join(lines)


def escalate(
    invoice: dict,
    classification: dict,
    action: dict,
    reason: str,
) -> dict:
    """
    Post a Slack escalation message with full account context.

    Args:
        invoice        : invoice dict from invoice_db
        classification : classification result dict
        action         : action dict from decision_engine
        reason         : human-readable escalation reason

    Returns:
        dict with status and channel
    """
    escalate_to: list[str] = action.get("escalate_to", ["Leila"])
    mentions = _resolve_mentions(escalate_to)
    context = _format_account_context(invoice)

    category_name = classification.get("category_name", "unknown")
    confidence = classification.get("confidence", 0.0)
    notes = action.get("notes") or classification.get("notes", "")

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f":rotating_light: AR Escalation — {invoice.get('account_name', 'Unknown')}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": context,
            },
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*Classification:* {category_name} ({confidence:.0%} confidence)",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Reason:* {reason}",
                },
            ],
        },
    ]
    if notes:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Agent notes:* {notes}"},
        })
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*Assigned to:* {mentions}"},
    })

    plain_text = (
        f"AR escalation: {invoice.get('account_name')} — {reason}. "
        f"Assigned to: {', '.join(escalate_to)}"
    )
    posted = _post_slack(plain_text, blocks=blocks)

    if posted:
        logger.info(
            "Escalation posted: invoice=%s reason=%s assigned=%s",
            invoice.get("invoice_id"), reason, escalate_to,
        )
    else:
        logger.error(
            "Escalation NOT delivered to Slack: invoice=%s reason=%s assigned=%s",
            invoice.get("invoice_id"), reason, escalate_to,
        )
    return {"action": "escalated", "escalated_to": escalate_to, "reason": reason, "posted": posted}


def escalate_p2p_non_compliance(record: dict) -> dict:
    """Slack alert when a P2P commitment is not fulfilled."""
    amount = record.get("amount_cents", 0) / 100
    leila_mention = f"<@{_leila_user_id()}>" if _leila_user_id() else "Leila"
    brie_mention = f"<@{_brie_user_id()}>" if _brie_user_id() else "Brie"

    message = (
        f":warning: *P2P Non-Compliance* — {record.get('account_name', 'Unknown')}\n"
        f"Invoice: {record.get('invoice_id')} | Amount: {amount:,.2f} {record.get('currency', 'USD')}\n"
        f"Promised by: {record.get('promise_date')} | {record.get('days_past_promise', 0)} days past promise\n"
        f"{leila_mention} {brie_mention} — please follow up."
    )
    posted = _post_slack(message)
    return {"action": "p2p_non_compliance_alerted", "posted": posted}


def escalate_unknown(classification: dict) -> dict:
    """Escalate when no invoice was found for the sender's email."""
    leila_mention = f"<@{_leila_user_id()}>" if _leila_user_id() else "Leila"
    category_name = classification.get("category_name", "unknown")

    message = (
        f":question: *AR Reply — No Invoice Match*\n"
        f"Received a reply from an unmatched email address.\n"
        f"Classification: {category_name} ({classification.get('confidence', 0):.0%} confidence)\n"
        f"{leila_mention} — please review."
    )
    posted = _post_slack(message)
    return {"action": "escalated", "escalated_to": ["Leila"], "reason": "No invoice match for sender email", "posted": posted}


def post_high_value_alert(invoice: dict) -> bool:
    """Alert for invoices above the escalation threshold. Returns delivery status."""
    amount = invoice.get("amount_cents", 0) / 100
    leila_mention = f"<@{_leila_user_id()}>" if _leila_user_id() else "Leila"
    message = (
        f":moneybag: *High-Value Invoice Alert* — {invoice.get('account_name')}\n"
        f"Invoice: {invoice.get('invoice_id')} | Amount: {amount:,.2f} {invoice.get('currency', 'USD')} | "
        f"Days overdue: {invoice.get('days_overdue', 0)}\n"
        f"{leila_mention} — this account needs personal follow-up."
    )
    return _post_slack(message)
