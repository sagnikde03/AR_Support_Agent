"""
Human Override Controls — Slack (F-20) & Shadow Mode Approvals (F-21).

Slack slash commands for Leila and Brie to control the AR Agent:

  /ar pause <invoice_id> [reason]  — pause cadence for a specific invoice
  /ar resume <invoice_id>          — resume cadence
  /ar note <invoice_id> <text>     — add a private note to an invoice
  /ar approve <outreach_id>        — approve a shadow-queued email and send it
  /ar skip <outreach_id>           — skip (discard) a shadow-queued email
  /ar queue                        — list all pending shadow queue items
  /ar status <invoice_id>          — show current status of an invoice
  /ar threshold <amount>           — change the auto-manage dollar threshold

Block Kit action handlers (from Approve/Skip buttons in shadow queue messages)
are also handled here via handle_block_action().
"""

import logging
import os
import re

from ar_agent.cadence import reminder_actions
from ar_agent.state import invoice_db

logger = logging.getLogger(__name__)

# Commands that change agent behaviour or send customer-facing email.
# Restricted to the allowlist; read-only commands stay open to the team.
_PRIVILEGED = {"pause", "resume", "approve", "skip", "threshold", "note"}


def _authorized_user_ids() -> set[str]:
    """
    Slack user IDs permitted to run privileged commands.

    Defaults to the escalation-routing IDs already configured (Leila, Brie,
    Chris), plus any extras in AR_ADMIN_SLACK_USERS (comma-separated).
    """
    ids = {
        os.environ.get("SLACK_USER_LEILA", ""),
        os.environ.get("SLACK_USER_BRIE", ""),
        os.environ.get("SLACK_USER_CHRIS", ""),
    }
    ids.update(u.strip() for u in os.environ.get("AR_ADMIN_SLACK_USERS", "").split(","))
    return {i for i in ids if i}


def _is_authorized(user_id: str) -> bool:
    allowed = _authorized_user_ids()
    if not allowed:
        # No allowlist configured: permit only in explicit dev mode, so a
        # misconfigured production deploy cannot hand out control.
        if os.environ.get("ENVIRONMENT", "") == "dev":
            logger.warning("No AR admin allowlist configured — allowing %s (ENVIRONMENT=dev)", user_id)
            return True
        logger.error("No AR admin allowlist configured — denying privileged command from %s", user_id)
        return False
    return user_id in allowed


def handle_command(command: str, args: str, user_id: str, user_name: str) -> str:
    """
    Process a /ar slash command. Returns the response text to send back to Slack.

    Args:
        command   : always "ar"
        args      : everything after "/ar " e.g. "pause INV-001 waiting on PO"
        user_id   : Slack user ID
        user_name : Slack display name
    """
    parts = args.strip().split(None, 2)
    if not parts:
        return _help_text()

    subcommand = parts[0].lower()

    if subcommand in _PRIVILEGED and not _is_authorized(user_id):
        logger.warning("Unauthorized /ar %s attempt by %s (%s)", subcommand, user_name, user_id)
        return (
            f":lock: `/ar {subcommand}` is restricted to the AR team. "
            "Read-only commands (`status`, `queue`, `help`) are available to everyone."
        )

    if subcommand == "pause":
        return _cmd_pause(parts[1:], user_id, user_name)
    if subcommand == "resume":
        return _cmd_resume(parts[1:], user_id, user_name)
    if subcommand == "note":
        return _cmd_note(parts[1:], user_id, user_name)
    if subcommand == "approve":
        return _cmd_approve(parts[1:], user_id, user_name)
    if subcommand == "skip":
        return _cmd_skip(parts[1:], user_id, user_name)
    if subcommand == "queue":
        return _cmd_queue()
    if subcommand == "status":
        return _cmd_status(parts[1:])
    if subcommand == "threshold":
        return _cmd_threshold(parts[1:], user_id, user_name)
    if subcommand == "help":
        return _help_text()

    return f"Unknown subcommand `{subcommand}`. Try `/ar help`."


def handle_block_action(action_id: str, value: str, user_id: str, user_name: str) -> str:
    """
    Handle Block Kit button clicks from shadow queue approval messages.

    action_id format: ar_approve_<outreach_id> or ar_skip_<outreach_id>
    """
    match = re.match(r"ar_(approve|skip)_(\d+)$", action_id)
    if not match:
        return "Unknown action."

    action = match.group(1)
    outreach_id = int(match.group(2))

    # Approve/Skip send or discard customer email — same allowlist as the
    # equivalent slash commands.
    if not _is_authorized(user_id):
        logger.warning("Unauthorized %s button press by %s (%s)", action, user_name, user_id)
        return f":lock: Only the AR team can {action} queued emails."

    if action == "approve":
        result = reminder_actions.send_approved_shadow_email(outreach_id, user_id)
        if result.get("status") == "sent":
            return f":white_check_mark: Email sent by {user_name}. Message ID: {result.get('message_id', 'N/A')}"
        return f":x: Send failed: {result.get('error', 'unknown error')}"

    if action == "skip":
        invoice_db.skip_shadow_item(outreach_id, user_id)
        return f":no_entry_sign: Email skipped by {user_name}."

    return "Unknown action."


# ── Subcommand handlers ───────────────────────────────────────────────────────

def _cmd_pause(args: list[str], user_id: str, user_name: str) -> str:
    if not args:
        return "Usage: `/ar pause <invoice_id> [reason]`"
    invoice_id = args[0]
    reason = args[1] if len(args) > 1 else f"Manually paused by {user_name}"

    invoice = invoice_db.get_invoice(invoice_id)
    if not invoice:
        return f":x: Invoice `{invoice_id}` not found."

    invoice_db.pause_cadence(invoice_id, reason)
    invoice_db.add_note(invoice_id, f"Cadence paused by {user_name}: {reason}", user_id)
    return f":pause_button: Cadence paused for `{invoice_id}` ({invoice['account_name']}). Reason: {reason}"


def _cmd_resume(args: list[str], user_id: str, user_name: str) -> str:
    if not args:
        return "Usage: `/ar resume <invoice_id>`"
    invoice_id = args[0]

    invoice = invoice_db.get_invoice(invoice_id)
    if not invoice:
        return f":x: Invoice `{invoice_id}` not found."

    invoice_db.resume_cadence(invoice_id)
    invoice_db.add_note(invoice_id, f"Cadence resumed by {user_name}", user_id)
    return f":arrow_forward: Cadence resumed for `{invoice_id}` ({invoice['account_name']})."


def _cmd_note(args: list[str], user_id: str, user_name: str) -> str:
    if len(args) < 2:
        return "Usage: `/ar note <invoice_id> <note text>`"
    invoice_id = args[0]
    note_text = args[1]

    invoice = invoice_db.get_invoice(invoice_id)
    if not invoice:
        return f":x: Invoice `{invoice_id}` not found."

    invoice_db.add_note(invoice_id, note_text, user_id)
    return f":memo: Note added to `{invoice_id}` ({invoice['account_name']}): _{note_text}_"


def _cmd_approve(args: list[str], user_id: str, user_name: str) -> str:
    if not args:
        return "Usage: `/ar approve <outreach_id>`"
    try:
        outreach_id = int(args[0])
    except ValueError:
        return f":x: Invalid outreach ID: `{args[0]}`"

    result = reminder_actions.send_approved_shadow_email(outreach_id, user_id)
    if result.get("status") == "sent":
        return f":white_check_mark: Email sent. Message ID: {result.get('message_id', 'N/A')}"
    if result.get("status") == "error":
        return f":x: Error: {result.get('reason', 'unknown')}"
    return f":x: Send failed: {result.get('error', 'unknown error')}"


def _cmd_skip(args: list[str], user_id: str, user_name: str) -> str:
    if not args:
        return "Usage: `/ar skip <outreach_id>`"
    try:
        outreach_id = int(args[0])
    except ValueError:
        return f":x: Invalid outreach ID: `{args[0]}`"

    invoice_db.skip_shadow_item(outreach_id, user_id)
    return f":no_entry_sign: Outreach `{outreach_id}` skipped by {user_name}."


def _cmd_queue() -> str:
    queue = invoice_db.get_shadow_queue()
    if not queue:
        return ":white_check_mark: No emails pending approval."

    lines = [f"*{len(queue)} email(s) pending approval:*"]
    for item in queue[:10]:
        amount = item.get("amount_cents", 0) / 100
        lines.append(
            f"• ID `{item['id']}` — {item.get('account_name', '?')} "
            f"({amount:,.2f} {item.get('currency', 'USD')}, stage {item['cadence_stage']}) — {item['subject']}\n"
            f"  `/ar approve {item['id']}` or `/ar skip {item['id']}`"
        )
    if len(queue) > 10:
        lines.append(f"_...and {len(queue) - 10} more._")
    return "\n".join(lines)


def _cmd_status(args: list[str]) -> str:
    if not args:
        return "Usage: `/ar status <invoice_id>`"
    invoice_id = args[0]

    invoice = invoice_db.get_invoice(invoice_id)
    if not invoice:
        return f":x: Invoice `{invoice_id}` not found."

    amount = invoice["amount_cents"] / 100
    history = invoice_db.get_outreach_history(invoice_id)
    p2p = invoice_db.get_p2p_record(invoice_id)

    lines = [
        f"*Invoice `{invoice_id}` — {invoice['account_name']}*",
        f"Amount: {amount:,.2f} {invoice.get('currency', 'USD')}",
        f"Due: {invoice['due_date']} ({invoice['days_overdue']} days overdue)",
        f"Status: {invoice['status']}",
        f"Stage: {invoice.get('cadence_stage', 0)}",
        f"Attempts: {history['total_sent']}",
        f"Paused: {'Yes — ' + invoice.get('pause_reason', '') if invoice.get('paused') else 'No'}",
    ]
    if p2p:
        lines.append(f"P2P commitment: {p2p['promise_date']} ({p2p['status']})")
    if invoice.get("known_non_complier"):
        lines.append(":red_circle: Known non-complier")

    return "\n".join(lines)


def _cmd_threshold(args: list[str], user_id: str, user_name: str) -> str:
    if not args:
        current = invoice_db.get_setting(
            "escalation_threshold_usd",
            os.environ.get("AR_ESCALATION_THRESHOLD_USD", "5000"),
        )
        return f"Current threshold: {float(current):,.2f} USD\nUsage: `/ar threshold <amount>`"
    try:
        amount = float(args[0].replace("$", "").replace(",", ""))
    except ValueError:
        return f":x: Invalid amount: `{args[0]}`"
    if amount <= 0:
        return f":x: Threshold must be positive: `{args[0]}`"

    # Stored in the shared settings table so all processes (webhook, poller,
    # slackbot) pick it up immediately and it survives restarts.
    invoice_db.set_setting("escalation_threshold_usd", str(amount), user_id)
    logger.info("AR escalation threshold changed to %.2f USD by %s (%s)", amount, user_name, user_id)
    return f":gear: Escalation threshold updated to {amount:,.2f} USD by {user_name}."


def _help_text() -> str:
    return """:robot_face: *AR Agent Commands*

`/ar pause <invoice_id> [reason]` — pause cadence for an invoice
`/ar resume <invoice_id>` — resume paused cadence
`/ar note <invoice_id> <text>` — add a note to an invoice
`/ar approve <outreach_id>` — approve and send a shadow-queued email
`/ar skip <outreach_id>` — discard a shadow-queued email
`/ar queue` — list all emails pending shadow approval
`/ar status <invoice_id>` — show current invoice status
`/ar threshold <amount>` — change the auto-manage dollar threshold
`/ar help` — show this message"""
