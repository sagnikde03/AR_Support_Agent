"""
Delinquency Tracking & Daily Report (F-09).

Compiles a daily summary for Leila delivered via Slack:
  - Invoices contacted today
  - Payments received today
  - New invoices entering at-risk status
  - Outstanding balances by account
  - Shadow queue backlog

Replaces Brie's manual daily tracking. Triggered by the 4-hour polling loop
at the start of each business day.
"""

import logging
from datetime import datetime, timezone

from ar_agent.integrations import slack_notify
from ar_agent.state import invoice_db

logger = logging.getLogger(__name__)


def _format_totals(by_currency: list[dict]) -> str:
    """Render outstanding totals, one figure per currency."""
    if not by_currency:
        return "0.00 USD"
    return " · ".join(f"{c['cents']/100:,.2f} {c['currency']}" for c in by_currency)


def generate() -> dict:
    """
    Compile and post the daily AR report to Slack.
    Returns the report data dict.
    """
    now = datetime.now(timezone.utc)
    today_str = f"{now:%B} {now.day}, {now.year}"  # cross-platform (no %-d)
    summary = invoice_db.get_ar_summary()

    sent_today = invoice_db.get_today_outreach_count()
    at_risk = invoice_db.get_at_risk_invoices()

    # Totals are per-currency: summing cents across currencies is meaningless.
    by_currency = invoice_db.get_ar_totals_by_currency()
    total_ar_display = _format_totals(by_currency)
    total_ar = summary["total_ar_cents"] / 100  # retained for the returned data dict
    at_risk_amount = summary["at_risk_cents"] / 100
    shadow_pending = summary["shadow_pending_count"]

    leila = slack_notify.user_mention("SLACK_USER_LEILA", "Leila")

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f":ledger: Daily AR Report — {today_str}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Open invoices:* {summary['open_invoice_count']}"},
                {"type": "mrkdwn", "text": f"*Total AR balance:* {total_ar_display}"},
                {"type": "mrkdwn", "text": f"*Outreach sent today:* {sent_today}"},
                {"type": "mrkdwn", "text": f"*At-risk accounts (45+ days):* {summary['at_risk_count']}"},
            ],
        },
    ]

    if shadow_pending > 0:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":hourglass_flowing_sand: *{shadow_pending} email(s) pending approval in shadow queue.* Use `/ar queue` to review.",
            },
        })

    if at_risk:
        at_risk_lines = []
        for inv in at_risk[:10]:  # cap at 10 for readability
            amount = inv["amount_cents"] / 100
            at_risk_lines.append(
                f"• {inv['account_name']} — {amount:,.2f} {inv.get('currency', 'USD')} ({inv['days_overdue']} days overdue)"
            )
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*At-risk accounts:*\n" + "\n".join(at_risk_lines),
            },
        })
        if len(at_risk) > 10:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"_...and {len(at_risk) - 10} more. See dashboard for full list._"},
            })

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"{leila} — daily AR summary above."},
    })

    plain_text = (
        f"Daily AR Report ({today_str}): "
        f"{summary['open_invoice_count']} open invoices, "
        f"{total_ar_display} outstanding, "
        f"{summary['at_risk_count']} at-risk accounts."
    )
    posted = slack_notify.post(plain_text, blocks=blocks)

    report_data = {
        "date": today_str,
        "posted": posted,
        "totals_by_currency": by_currency,
        "open_invoice_count": summary["open_invoice_count"],
        "total_ar": total_ar,
        "at_risk_count": summary["at_risk_count"],
        "at_risk_amount": at_risk_amount,
        "sent_today": sent_today,
        "shadow_pending": shadow_pending,
    }
    logger.info("Daily report posted: %s", report_data)
    return report_data
