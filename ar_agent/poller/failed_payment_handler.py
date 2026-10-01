"""
Failed Payment Detection & Alert (F-26).

When the 4-hour polling loop detects a failed autopay attempt in FreshBooks,
this handler:
  1. Sends a Slack alert to Brie and Leila with account name, invoice amount,
     and failure reason.
  2. Queues a customer-facing failed payment email via Postmark (shadow mode
     respected — goes through the same approval flow as cadence emails).
  3. Logs the failed payment event in the state engine.

The agent NEVER retries the payment. Retry is always manual by Brie/Leila
in FreshBooks after reviewing the Slack alert.
"""

import logging
import os

from ar_agent.integrations import postmark_client, slack_notify
from ar_agent.state import invoice_db
from ar_agent.templates import email_templates

logger = logging.getLogger(__name__)


def _shadow_mode() -> bool:
    return os.environ.get("SHADOW_MODE", "true").lower() not in ("false", "0", "no")


def handle_failed_payment(invoice_id: str, failure_reason: str) -> dict:
    """
    Process a failed autopay event for the given invoice.

    Args:
        invoice_id     : FreshBooks invoice ID
        failure_reason : reason string from FreshBooks (e.g. "Insufficient funds")

    Returns:
        Status dict
    """
    invoice = invoice_db.get_invoice(invoice_id)
    if not invoice:
        logger.warning("Failed payment event for unknown invoice %s", invoice_id)
        return {"status": "skipped", "reason": "invoice not found"}

    # An invoice keeps its failed status in FreshBooks until someone retries,
    # so every 4-hour poll re-reports the same failure. Alert once per
    # failure: suppress if we already logged failed-payment outreach for this
    # invoice. Cleared automatically when the invoice is paid (status leaves
    # the failure set) or when a fresh attempt produces a new outreach record.
    if _already_alerted(invoice_id):
        logger.debug("Failed payment for %s already alerted — suppressing repeat", invoice_id)
        return {"status": "skipped", "reason": "already_alerted_for_this_failure"}

    amount = invoice["amount_cents"] / 100
    account_name = invoice["account_name"]
    payment_link = invoice.get("freshbooks_payment_link") or ""

    # ── Log note in state engine ───────────────────────────────────────────────
    invoice_db.add_note(
        invoice_id,
        f"Autopay failed: {failure_reason}",
        "ar_agent",
    )

    # ── Slack alert to Brie and Leila ─────────────────────────────────────────
    leila = slack_notify.user_mention("SLACK_USER_LEILA", "Leila")
    brie = slack_notify.user_mention("SLACK_USER_BRIE", "Brie")

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f":x: Autopay Failed — {account_name}",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Account:* {account_name}"},
                {"type": "mrkdwn", "text": f"*Invoice:* {invoice_id}"},
                {"type": "mrkdwn", "text": f"*Amount:* {amount:,.2f} {invoice.get('currency', 'USD')}"},
                {"type": "mrkdwn", "text": f"*Failure reason:* {failure_reason}"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{brie} {leila} — please retry the payment manually in FreshBooks "
                    "and update the account if a new payment method is needed.\n"
                    f":warning: *The agent will not retry this payment automatically.*"
                ),
            },
        },
    ]
    alert_posted = slack_notify.post(
        f"Autopay failed: {account_name} — {amount:,.2f} {invoice.get('currency', 'USD')}. Reason: {failure_reason}",
        blocks=blocks,
    )

    # ── Queue customer-facing failed payment email ─────────────────────────────
    # Check for in-conversation payment signals before sending (F-12/F-13):
    # if the customer recently confirmed manual payment, skip the failed payment notice
    if _recent_payment_signal(invoice_id):
        logger.info(
            "Skipping failed payment notice for %s — recent payment signal on file", invoice_id
        )
        return {
            "status": "alert_sent_no_email",
            "reason": "Recent payment confirmation signal — skipped customer email",
        }

    template = email_templates.failed_payment(
        account_name=account_name,
        invoice_number=invoice_id,
        amount=amount,
        payment_link=payment_link,
        currency=invoice.get("currency", "USD"),
    )

    tier1_contacts = invoice_db.get_tier1_contacts(invoice["account_id"])
    if not tier1_contacts:
        logger.warning("No tier-1 contacts for %s — Slack alert sent, customer email skipped", invoice_id)
        return {"status": "alert_sent_no_contacts"}

    to_str = ", ".join(tier1_contacts)
    shadow = _shadow_mode()

    # Rendered bodies stored so a shadow approval sends exactly this content
    outreach_id = invoice_db.record_outreach(
        invoice_id=invoice_id,
        stage=0,  # stage 0 = non-cadence send
        email_to=to_str,
        subject=template.subject,
        template_name="failed_payment",
        shadow_mode=shadow,
        html_body=template.html_body,
        text_body=template.text_body,
    )

    if shadow:
        # Surface the queued customer email for approval — same flow as cadence
        slack_notify.post(
            f":hourglass_flowing_sand: Failed-payment email to {account_name} queued for approval "
            f"(ID {outreach_id}). Approve with `/ar approve {outreach_id}` or discard with `/ar skip {outreach_id}`."
        )
    else:
        try:
            result = postmark_client.send_email(
                to=to_str,
                subject=template.subject,
                html_body=template.html_body,
                text_body=template.text_body,
                tag="ar-failed-payment",
                metadata={"invoice_id": invoice_id, "reason": failure_reason},
            )
            invoice_db.update_outreach_sent(outreach_id, result.get("MessageID", ""))
        except Exception as exc:
            invoice_db.update_outreach_failed(outreach_id, str(exc))
            logger.error("Failed payment email send error: %s", exc)

    logger.info(
        "Failed payment handled: invoice=%s account=%s shadow=%s",
        invoice_id, account_name, shadow,
    )
    return {
        "status": "shadow_queued" if shadow else "sent",
        "outreach_id": outreach_id,
        "slack_alert": "sent" if alert_posted else "failed",
    }


def _already_alerted(invoice_id: str) -> bool:
    """
    True if a failed-payment notice has already been recorded for this
    invoice since the last time it was paid.

    mark_payment_received() flips the invoice to 'paid', and a subsequent
    failure arrives as a new event on a re-opened invoice, so scoping the
    check to outreach recorded after the last payment keeps a genuine second
    failure alertable while suppressing the same one every poll cycle.
    """
    invoice = invoice_db.get_invoice(invoice_id)
    last_paid = (invoice or {}).get("actual_payment_date") or ""
    for record in invoice_db.get_outreach_history(invoice_id)["records"]:
        if record.get("template_name") != "failed_payment":
            continue
        recorded_at = record.get("created_at") or record.get("sent_at") or ""
        if not last_paid or recorded_at > last_paid:
            return True
    return False


def _recent_payment_signal(invoice_id: str) -> bool:
    """
    Check if the state engine has a recent in-conversation payment signal
    for this invoice (category 1 reply received and processed).
    """
    return invoice_db.has_recent_payment_signal(invoice_id)
