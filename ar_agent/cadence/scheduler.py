"""
Communication Cadence Engine — scheduler (F-06).

Determines which invoices need action on the current poll cycle and what
cadence stage to send. Reads from the invoice state engine; does not send
emails directly — that is reminder_actions.py.

Stage mapping:
  Stage 1 — 7 days before due
  Stage 2 — due date
  Stage 3 — 7 days overdue
  Stage 4 — 14 days overdue
  Stage 5 — 21 days overdue
  Stage 6 — 28 days overdue
  Stage 7 — 45+ days overdue (suspension risk — always Slack-approved before send)

Cadence NEVER pauses when a P2P commitment is recorded. The scheduler checks
P2P compliance separately via check_p2p_compliance().
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

from ar_agent.state import invoice_db

logger = logging.getLogger(__name__)

# Days-overdue thresholds that trigger a new cadence stage send.
# A stage is sent once — repeat sends at the same stage are blocked unless
# days_overdue has crossed the next boundary.
_OVERDUE_STAGE_MAP = [
    (45, 7),   # suspension risk — always Slack-approved before send
    (28, 6),
    (21, 5),
    (14, 4),
    (7,  3),
    (0,  2),   # due today
    (-7, 1),   # 7 days before due (days_overdue is negative when not yet due)
]


@dataclass
class CadenceAction:
    invoice_id: str
    account_id: str
    account_name: str
    amount: float
    currency: str
    invoice_number: str
    due_date: str
    days_overdue: int
    stage: int
    is_suspension_risk: bool
    payment_link: str


def _compute_stage(days_overdue: int) -> int | None:
    """
    Return the cadence stage to send for the given days_overdue value,
    or None if no send is needed.
    """
    for threshold, stage in _OVERDUE_STAGE_MAP:
        if days_overdue >= threshold:
            return stage
    return None


def _days_overdue_today(due_date_str: str) -> int:
    """
    Compute days overdue as of today (UTC). Negative means not yet due.
    """
    today = datetime.now(timezone.utc).date()
    try:
        due = date.fromisoformat(due_date_str)
    except ValueError:
        logger.warning("Invalid due_date format: %s", due_date_str)
        return 0
    return (today - due).days


def compute_action_queue() -> list[CadenceAction]:
    """
    Evaluate all active invoices and return those that need an email sent
    on this poll cycle.

    An invoice is queued if:
      1. It is not paid, written off, or paused.
      2. The current days_overdue falls at or past a stage boundary.
      3. The invoice has not already been sent at this stage.
      4. The last outreach was not within the last 6 hours (debounce).
    """
    invoices = invoice_db.get_invoices_for_cadence()
    actions: list[CadenceAction] = []

    for inv in invoices:
        invoice_id = inv["invoice_id"]
        days_overdue = _days_overdue_today(inv["due_date"])

        # Sync days_overdue in the DB
        if days_overdue != inv.get("days_overdue", 0):
            invoice_db.update_days_overdue(invoice_id, days_overdue)

        # Determine suspension risk (45+ days)
        is_suspension_risk = days_overdue >= 45

        # Determine which stage should fire
        target_stage = _compute_stage(days_overdue)
        if target_stage is None:
            continue

        # Skip if we have already sent this stage for this invoice
        current_stage = inv.get("cadence_stage", 0)
        if current_stage >= target_stage:
            continue

        # Debounce: skip if last outreach was within the last 6 hours
        last_at = inv.get("last_outreach_at")
        if last_at:
            try:
                last_dt = datetime.fromisoformat(last_at)
                hours_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
                if hours_since < 6:
                    logger.debug(
                        "Skipping invoice %s — last outreach %.1f hours ago", invoice_id, hours_since
                    )
                    continue
            except (ValueError, TypeError):
                pass

        actions.append(
            CadenceAction(
                invoice_id=invoice_id,
                account_id=inv["account_id"],
                account_name=inv["account_name"],
                amount=inv["amount_cents"] / 100,
                currency=inv.get("currency", "USD"),
                invoice_number=invoice_id,  # FreshBooks invoice ID is used as the display number
                due_date=inv["due_date"],
                days_overdue=days_overdue,
                stage=target_stage,
                is_suspension_risk=is_suspension_risk,
                payment_link=inv.get("freshbooks_payment_link") or "",
            )
        )

    return actions


def check_p2p_compliance() -> list[dict]:
    """
    Check all pending P2P commitments whose promise date has passed.

    Returns a list of non-complied records for escalation handling.
    The caller (ar_poller) escalates each via escalation_engine.
    """
    overdue_p2p = invoice_db.get_pending_p2p_records()
    non_complied = []

    for record in overdue_p2p:
        invoice = invoice_db.get_invoice(record["invoice_id"])
        if not invoice or invoice["status"] in ("paid", "written_off"):
            continue

        # 3-day processing tolerance for bank/check clearance
        try:
            promise_date = date.fromisoformat(record["promise_date"])
        except (ValueError, TypeError):
            logger.warning(
                "Invalid promise_date %r on P2P record %s — skipping",
                record.get("promise_date"), record.get("id"),
            )
            continue
        today = datetime.now(timezone.utc).date()
        days_past_promise = (today - promise_date).days

        if days_past_promise >= 3:
            invoice_db.mark_p2p_non_complied(record["id"])
            invoice_db.mark_non_complier(record["invoice_id"])
            non_complied.append({
                "invoice_id": record["invoice_id"],
                "account_name": record["account_name"],
                "amount_cents": record["amount_cents"],
                "promise_date": record["promise_date"],
                "days_past_promise": days_past_promise,
                "p2p_id": record["id"],
            })

    return non_complied
