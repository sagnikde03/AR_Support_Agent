"""
Reminder Actions & Shadow Mode (F-06, F-21).

Executes the cadence actions computed by scheduler.py. In shadow mode
(SHADOW_MODE=true), emails are queued in the DB and posted to Slack for
human approval instead of sending. In live mode, emails send immediately
via Postmark after the business hours check.

Shadow mode exit criterion (Leila): zero false-autonomy events over one
full calendar month before switching to autonomous.
"""

import logging
import os

from ar_agent.cadence import scheduler, timing
from ar_agent.integrations import postmark_client
from ar_agent.state import invoice_db
from ar_agent.templates import email_templates

logger = logging.getLogger(__name__)


def _shadow_mode() -> bool:
    return os.environ.get("SHADOW_MODE", "true").lower() not in ("false", "0", "no")


def _slack_webhook() -> str:
    return os.environ.get("SLACK_WEBHOOK_URL", "")


def _slack_bot_token() -> str:
    return os.environ.get("SLACK_BOT_TOKEN", "")


def _ops_channel() -> str:
    return os.environ.get("AR_OPS_SLACK_CHANNEL", "#ar-operations")


def _post_slack(message: str, blocks: list | None = None) -> None:
    """Post a message to the AR operations Slack channel."""
    try:
        import requests
        webhook = _slack_webhook()
        if webhook:
            payload = {"text": message}
            if blocks:
                payload["blocks"] = blocks
            requests.post(webhook, json=payload, timeout=10)
            return
        # Fall back to Slack Bolt client if a bot token is available
        token = _slack_bot_token()
        if token:
            payload = {"channel": _ops_channel(), "text": message}
            if blocks:
                payload["blocks"] = blocks
            requests.post(
                "https://slack.com/api/chat.postMessage",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
    except Exception as exc:
        logger.error("Slack notification failed: %s", exc)


def _build_shadow_approval_blocks(outreach_id: int, action: scheduler.CadenceAction, template: email_templates.EmailTemplate) -> list:
    """Build Slack Block Kit message for shadow mode approval."""
    amount_str = f"{action.amount:,.2f} {action.currency}"
    stage_labels = {1: "7 days before due", 2: "Due today", 3: "7 days overdue", 4: "14 days overdue", 5: "21 days overdue", 6: "28 days overdue", 7: "Suspension risk (45+ days)"}
    stage_label = stage_labels.get(action.stage, f"Stage {action.stage}")

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":mailbox: *AR Email Queued for Approval*\n*Account:* {action.account_name}\n*Amount:* {amount_str}\n*Days overdue:* {action.days_overdue}\n*Stage:* {stage_label}\n*Subject:* {template.subject}",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve & Send"},
                    "style": "primary",
                    "action_id": f"ar_approve_{outreach_id}",
                    "value": str(outreach_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Skip"},
                    "style": "danger",
                    "action_id": f"ar_skip_{outreach_id}",
                    "value": str(outreach_id),
                },
            ],
        },
    ]


def process_action(action: scheduler.CadenceAction) -> dict:
    """
    Process a single cadence action: build template, queue/send, and log.

    Returns a status dict with what happened.
    """
    if not timing.should_send_now():
        next_window = timing.format_next_window()
        logger.info(
            "Outside business hours — skipping %s stage %d (next window: %s)",
            action.invoice_id, action.stage, next_window
        )
        return {"status": "deferred", "reason": "outside_business_hours", "next_window": next_window}

    # Build template
    template_kwargs = {
        "account_name":   action.account_name,
        "invoice_number": action.invoice_number,
        "amount":         action.amount,
        "currency":       action.currency,
        "payment_link":   action.payment_link,
    }
    # Stages 3-7 also receive days_overdue
    if action.stage >= 3:
        template_kwargs["days_overdue"] = action.days_overdue
    # Stages 1-2 receive due_date
    if action.stage in (1, 2):
        template_kwargs["due_date"] = action.due_date

    try:
        template = email_templates.get_cadence_template(action.stage, **template_kwargs)
    except ValueError as exc:
        logger.error("Template error for %s stage %d: %s", action.invoice_id, action.stage, exc)
        return {"status": "error", "reason": str(exc)}

    # Determine recipients
    tier1_contacts = invoice_db.get_tier1_contacts(action.account_id)
    if not tier1_contacts:
        logger.warning("No tier-1 contacts for account %s — skipping send", action.account_id)
        return {"status": "skipped", "reason": "no_contacts"}

    # For stage 4+ (14 days) with 3+ unanswered attempts, include tier 2
    history = invoice_db.get_outreach_history(action.invoice_id)
    email_to = tier1_contacts
    if action.stage >= 4 and history["total_sent"] >= 3:
        tier2 = invoice_db.get_tier2_contacts(action.account_id)
        if tier2:
            email_to = tier1_contacts + tier2

    to_str = ", ".join(email_to)

    shadow = _shadow_mode()

    outreach_id = invoice_db.record_outreach(
        invoice_id=action.invoice_id,
        stage=action.stage,
        email_to=to_str,
        subject=template.subject,
        template_name=template.template_name,
        shadow_mode=shadow,
        html_body=template.html_body,
        text_body=template.text_body,
    )

    if shadow:
        # Advance cadence stage + last_outreach_at now so compute_action_queue()
        # does not re-queue this same invoice/stage on every poll while the
        # approval sits in Slack. The attempt is counted once, here.
        invoice_db.increment_attempt(action.invoice_id, action.stage)
        blocks = _build_shadow_approval_blocks(outreach_id, action, template)
        _post_slack(
            f"AR email queued for approval: {action.account_name} — {template.subject}",
            blocks=blocks,
        )
        logger.info(
            "Shadow queued: invoice=%s stage=%d outreach_id=%d",
            action.invoice_id, action.stage, outreach_id
        )
        return {"status": "shadow_queued", "outreach_id": outreach_id}

    # Live mode: send immediately
    return _send_live(action, template, to_str, outreach_id)


def _send_live(
    action: scheduler.CadenceAction,
    template: email_templates.EmailTemplate,
    to_str: str,
    outreach_id: int,
) -> dict:
    """Send the email immediately via Postmark and update the state engine."""
    try:
        # PDF fetch happens via FreshBooks client (F-03) — stubbed until credentials are configured
        pdf_bytes = _fetch_invoice_pdf(action.invoice_id)

        # Every cadence reminder must carry the invoice PDF. If it cannot be
        # fetched (FreshBooks unavailable), fail the send rather than deliver
        # a reminder without the attachment.
        if not pdf_bytes:
            invoice_db.update_outreach_failed(
                outreach_id, "invoice PDF unavailable — send aborted (FreshBooks not configured?)"
            )
            logger.error(
                "PDF unavailable for invoice %s — live send aborted", action.invoice_id
            )
            return {"status": "failed", "outreach_id": outreach_id, "error": "invoice_pdf_unavailable"}

        result = postmark_client.send_with_invoice_pdf(
            to=to_str,
            subject=template.subject,
            html_body=template.html_body,
            text_body=template.text_body,
            invoice_pdf_bytes=pdf_bytes,
            invoice_filename=f"Invoice-{action.invoice_number}.pdf",
            tag="ar-cadence",
            metadata={"invoice_id": action.invoice_id, "stage": str(action.stage)},
        )

        message_id = result.get("MessageID", "")
        invoice_db.update_outreach_sent(outreach_id, message_id)
        invoice_db.increment_attempt(action.invoice_id, action.stage)

        logger.info(
            "Sent: invoice=%s stage=%d to=%s message_id=%s",
            action.invoice_id, action.stage, to_str, message_id
        )
        return {"status": "sent", "outreach_id": outreach_id, "message_id": message_id}

    except Exception as exc:
        invoice_db.update_outreach_failed(outreach_id, str(exc))
        logger.error("Send failed: invoice=%s stage=%d error=%s", action.invoice_id, action.stage, exc)
        return {"status": "failed", "outreach_id": outreach_id, "error": str(exc)}


def send_approved_shadow_email(outreach_id: int, approved_by: str) -> dict:
    """
    Called by Slack command handler when a human approves a shadow-queued email.
    Fetches the queued record, sends via Postmark, and updates state.
    """
    row = invoice_db.get_outreach_record(outreach_id)
    if not row:
        return {"status": "error", "reason": "outreach record not found"}
    if row["status"] != "queued_shadow":
        return {"status": "error", "reason": f"unexpected status: {row['status']}"}

    record = row
    invoice = invoice_db.get_invoice(record["invoice_id"])
    if not invoice:
        return {"status": "error", "reason": "invoice not found"}

    # Rendered bodies were stored at queue time — send the approved content verbatim
    html_body = record.get("html_body")
    text_body = record.get("text_body")
    if not html_body or not text_body:
        return {"status": "error", "reason": "queued record has no rendered body — cannot send"}

    invoice_db.approve_shadow_item(outreach_id, approved_by)

    # Cadence reminders (stage >= 1) must carry the invoice PDF. Stage 0 is a
    # non-cadence send (e.g. failed-payment notice) — no attachment by design.
    is_cadence = record.get("cadence_stage", 0) >= 1
    pdf_bytes = _fetch_invoice_pdf(record["invoice_id"]) if is_cadence else None
    try:
        if is_cadence and not pdf_bytes:
            invoice_db.update_outreach_failed(
                outreach_id, "invoice PDF unavailable — send aborted (FreshBooks not configured?)"
            )
            return {"status": "failed", "error": "invoice_pdf_unavailable"}

        common = {
            "to":        record["email_to"],
            "subject":   record["subject"],
            "html_body": html_body,
            "text_body": text_body,
        }
        if is_cadence:
            result = postmark_client.send_with_invoice_pdf(
                **common,
                invoice_pdf_bytes=pdf_bytes,
                invoice_filename=f"Invoice-{record['invoice_id']}.pdf",
                tag="ar-cadence-approved",
            )
        else:
            result = postmark_client.send_email(**common, tag="ar-approved")

        message_id = result.get("MessageID", "")
        invoice_db.update_outreach_sent(outreach_id, message_id)
        # No increment_attempt here — the attempt was counted at queue time
        return {"status": "sent", "message_id": message_id}

    except Exception as exc:
        invoice_db.update_outreach_failed(outreach_id, str(exc))
        return {"status": "failed", "error": str(exc)}


def run_cadence_cycle() -> dict:
    """
    Execute one full cadence cycle: compute queue, process each action.
    Called by ar_poller on every poll tick.
    """
    actions = scheduler.compute_action_queue()
    results = {"total": len(actions), "sent": 0, "shadow_queued": 0, "deferred": 0, "skipped": 0, "failed": 0}

    for action in actions:
        result = process_action(action)
        status = result.get("status", "unknown")
        if status in results:
            results[status] += 1
        else:
            results["failed"] += 1

    logger.info("Cadence cycle complete: %s", results)
    return results


def _fetch_invoice_pdf(invoice_id: str) -> bytes | None:
    """
    Fetch invoice PDF from FreshBooks. Stubbed until F-03 credentials are configured.
    Returns None if FreshBooks client is not available.
    """
    try:
        from ar_agent.integrations import freshbooks_client
        return freshbooks_client.get_invoice_pdf(invoice_id)
    except Exception as exc:
        logger.debug("PDF fetch unavailable for %s: %s", invoice_id, exc)
        return None
