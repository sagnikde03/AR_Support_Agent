"""
Risk-of-Suspension Weekly Report (F-10).

Auto-generates the weekly report Brie currently produces manually:
all accounts at 45+ days overdue, sorted by amount, with last-contact
date and current cadence stage. Delivered via Slack to Leila.

Rules-based — no LLM. Triggered by ar_poller on Monday mornings.
"""

import logging
from datetime import datetime, timezone

from ar_agent.integrations import slack_notify
from ar_agent.state import invoice_db

logger = logging.getLogger(__name__)


def generate() -> dict:
    """
    Compile and post the weekly at-risk AR report to Slack.
    Returns the report data dict.
    """
    now = datetime.now(timezone.utc)
    today_str = f"{now:%B} {now.day}, {now.year}"  # cross-platform (no %-d)
    at_risk = invoice_db.get_at_risk_invoices()

    # Sort by amount descending
    at_risk_sorted = sorted(at_risk, key=lambda x: x["amount_cents"], reverse=True)

    leila = slack_notify.user_mention("SLACK_USER_LEILA", "Leila")
    brie = slack_notify.user_mention("SLACK_USER_BRIE", "Brie")

    total_at_risk_cents = sum(inv["amount_cents"] for inv in at_risk_sorted)
    total_at_risk = total_at_risk_cents / 100

    stage_labels = {
        1: "Pre-due", 2: "Due", 3: "+7d", 4: "+14d",
        5: "+21d", 6: "+28d", 7: "Suspension risk",
    }

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f":warning: Weekly At-Risk AR Report — {today_str}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{len(at_risk_sorted)} accounts at 45+ days overdue*\n"
                    f"*Total at-risk balance: {total_at_risk:,.2f} USD*"
                ),
            },
        },
    ]

    if not at_risk_sorted:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": ":white_check_mark: No accounts at 45+ days overdue. Great week!"},
        })
    else:
        rows = []
        for inv in at_risk_sorted[:20]:  # cap at 20 rows
            amount = inv["amount_cents"] / 100
            stage = stage_labels.get(inv.get("cadence_stage", 0), str(inv.get("cadence_stage", 0)))
            # The key exists with a NULL value for never-contacted invoices,
            # so a dict default alone is not enough.
            raw_contact = inv.get("last_outreach_at")
            last_contact = raw_contact[:10] if raw_contact else "Never"
            non_complier_flag = " :red_circle:" if inv.get("known_non_complier") else ""
            rows.append(
                f"• *{inv['account_name']}*{non_complier_flag} — {amount:,.2f} {inv.get('currency', 'USD')} "
                f"({inv['days_overdue']} days, stage {stage}, last contact: {last_contact})"
            )

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(rows)},
        })
        if len(at_risk_sorted) > 20:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"_...and {len(at_risk_sorted) - 20} more. See dashboard for full list._",
                },
            })

    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"{leila} {brie} — weekly at-risk summary above. :red_circle: = known non-complier.",
        },
    })

    plain_text = (
        f"Weekly AR At-Risk Report ({today_str}): "
        f"{len(at_risk_sorted)} accounts at 45+ days overdue, "
        f"{total_at_risk:,.2f} USD total."
    )
    posted = slack_notify.post(plain_text, blocks=blocks)

    report_data = {
        "date": today_str,
        "posted": posted,
        "at_risk_count": len(at_risk_sorted),
        "total_at_risk": total_at_risk,
        "accounts": [
            {
                "invoice_id": inv["invoice_id"],
                "account_name": inv["account_name"],
                "amount": inv["amount_cents"] / 100,
                "days_overdue": inv["days_overdue"],
                "stage": inv.get("cadence_stage"),
            }
            for inv in at_risk_sorted
        ],
    }
    logger.info("Weekly report posted: %d at-risk accounts", len(at_risk_sorted))
    return report_data
