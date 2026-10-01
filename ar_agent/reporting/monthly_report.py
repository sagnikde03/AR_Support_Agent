"""
Monthly AR Consolidated Report (F-19).

Auto-generated monthly summary emailed via Postmark to Chris, Greg, and Leila:
  - Total AR balance
  - Invoices resolved vs still open this month
  - Amount collected vs written off
  - Escalated accounts
  - 6-month no-response invoice flagging (reviewed at monthly AR calls)
  - Cadence performance metrics

Agent compiles the numbers; Brie writes the narrative overlay if needed.
Triggered by ar_poller on the first business day of each month.
"""

import html
import logging
import os
from datetime import datetime, timezone

from ar_agent.integrations import postmark_client
from ar_agent.state import invoice_db

logger = logging.getLogger(__name__)

# TODO: the module docstring also promises "amount collected vs written off",
# "escalated accounts", and "cadence performance metrics". Those need history
# tables the state engine does not keep yet — tracked separately, not in scope
# for this change.


def _recipients() -> list[str]:
    """Monthly report recipients: Chris, Greg, Leila."""
    emails = os.environ.get(
        "MONTHLY_REPORT_RECIPIENTS",
        "",
    ).split(",")
    return [e.strip() for e in emails if e.strip()]


def _leila_email() -> str:
    return os.environ.get("AR_CC_EMAIL", "")


def generate() -> dict:
    """
    Compile and send the monthly AR report via Postmark.
    Returns the report data dict.
    """
    today_str = datetime.now(timezone.utc).strftime("%B %Y")

    # Check deliverability before doing any query or body-building work
    recipients = _recipients()
    if not recipients:
        logger.warning("MONTHLY_REPORT_RECIPIENTS not set — monthly report not sent")
        return {"status": "skipped", "reason": "no recipients configured"}

    summary = invoice_db.get_ar_summary()
    at_risk = invoice_db.get_at_risk_invoices()
    no_response = invoice_db.get_no_response_invoices(months=6)
    overdue = invoice_db.get_overdue_invoices(min_days=1)

    total_ar = summary["total_ar_cents"] / 100
    at_risk_total = summary["at_risk_cents"] / 100
    at_risk_count = summary["at_risk_count"]

    # Accounts 90+ days overdue — recommend review
    severely_overdue = [inv for inv in overdue if inv.get("days_overdue", 0) >= 90]

    no_response_total = sum(inv["amount_cents"] for inv in no_response) / 100

    html_body = _build_html(
        today_str=today_str,
        total_ar=total_ar,
        open_count=summary["open_invoice_count"],
        at_risk_count=at_risk_count,
        at_risk_total=at_risk_total,
        at_risk_accounts=at_risk,
        severely_overdue=severely_overdue,
        no_response=no_response,
        no_response_total=no_response_total,
    )
    text_body = _build_text(
        today_str=today_str,
        total_ar=total_ar,
        open_count=summary["open_invoice_count"],
        at_risk_count=at_risk_count,
        at_risk_total=at_risk_total,
        severely_overdue=severely_overdue,
        no_response=no_response,
        no_response_total=no_response_total,
    )

    try:
        postmark_client.send_email(
            to=recipients,
            subject=f"CapLinked AR Monthly Report — {today_str}",
            html_body=html_body,
            text_body=text_body,
            tag="ar-monthly-report",
        )
        status = "sent"
    except Exception as exc:
        logger.error("Monthly report send failed: %s", exc)
        status = "failed"

    report_data = {
        "month": today_str,
        "status": status,
        "total_ar": total_ar,
        "open_count": summary["open_invoice_count"],
        "at_risk_count": at_risk_count,
        "at_risk_total": at_risk_total,
        "no_response_count": len(no_response),
        "no_response_total": no_response_total,
        "severely_overdue_count": len(severely_overdue),
    }
    logger.info("Monthly report %s: %s", status, report_data)
    return report_data


def _build_html(
    today_str: str,
    total_ar: float,
    open_count: int,
    at_risk_count: int,
    at_risk_total: float,
    at_risk_accounts: list,
    severely_overdue: list,
    no_response: list,
    no_response_total: float,
) -> str:
    # account_name and currency come from FreshBooks (customer-controlled) —
    # escape before interpolating into HTML.
    def _row(inv: dict) -> str:
        return (
            f"<tr><td>{html.escape(str(inv['account_name']))}</td>"
            f"<td>{inv['amount_cents']/100:,.2f} {html.escape(str(inv.get('currency', 'USD')))}</td>"
            f"<td>{int(inv['days_overdue'])}</td></tr>"
        )

    at_risk_rows = "".join(
        _row(inv)
        for inv in sorted(at_risk_accounts, key=lambda x: x["amount_cents"], reverse=True)[:15]
    )
    no_response_rows = "".join(
        _row(inv)
        for inv in sorted(no_response, key=lambda x: x["amount_cents"], reverse=True)[:10]
    )

    return f"""<!DOCTYPE html>
<html>
<head><style>
  body {{ font-family: Arial, sans-serif; color: #333; max-width: 700px; margin: 0 auto; }}
  h1 {{ color: #1E5C97; }}
  h2 {{ color: #444; border-bottom: 1px solid #ddd; padding-bottom: 4px; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 24px; }}
  th {{ background: #1E5C97; color: white; padding: 8px; text-align: left; }}
  td {{ padding: 8px; border-bottom: 1px solid #eee; }}
  .stat-box {{ background: #f5f5f5; padding: 16px; margin: 8px 0; border-radius: 4px; }}
  .highlight {{ color: #c0392b; font-weight: bold; }}
</style></head>
<body>
<h1>CapLinked AR Monthly Report — {today_str}</h1>
<p><em>Compiled by the AR Agent. Narrative overlay by Brie as needed.</em></p>

<h2>Summary</h2>
<div class="stat-box">
  <strong>Total AR balance:</strong> {total_ar:,.2f} USD<br>
  <strong>Open invoices:</strong> {open_count}<br>
  <strong>At-risk accounts (45+ days):</strong> {at_risk_count} / {at_risk_total:,.2f} USD<br>
  <strong>Severely overdue (90+ days):</strong> {len(severely_overdue)}<br>
  <strong>No-response 6+ months:</strong> {len(no_response)} / {no_response_total:,.2f} USD
</div>

<h2>At-Risk Accounts (45+ Days Overdue)</h2>
{"<table><tr><th>Account</th><th>Amount</th><th>Days Overdue</th></tr>" + at_risk_rows + "</table>" if at_risk_accounts else "<p>None.</p>"}

<h2>6-Month No-Response — Review at Monthly AR Call</h2>
<p>The following accounts have open invoices with no client response for 6+ months.
Final deletion decision requires Leila, Greg, and Chris.</p>
{"<table><tr><th>Account</th><th>Amount</th><th>Days Overdue</th></tr>" + no_response_rows + "</table>" if no_response else "<p>None.</p>"}

<p style="color:#888;font-size:12px;">This report was generated automatically by the CapLinked AR Agent.
For questions about individual accounts, contact billing@caplinked.com.</p>
</body>
</html>"""


def _build_text(
    today_str: str,
    total_ar: float,
    open_count: int,
    at_risk_count: int,
    at_risk_total: float,
    severely_overdue: list,
    no_response: list,
    no_response_total: float,
) -> str:
    no_response_lines = "\n".join(
        f"- {inv['account_name']}: {inv['amount_cents']/100:,.2f} "
        f"{inv.get('currency', 'USD')} ({inv['days_overdue']} days overdue)"
        for inv in no_response[:10]
    ) or "None."

    return f"""CapLinked AR Monthly Report — {today_str}
Compiled by the AR Agent.

SUMMARY
-------
Total AR balance: {total_ar:,.2f} USD
Open invoices: {open_count}
At-risk accounts (45+ days): {at_risk_count} / {at_risk_total:,.2f} USD
Severely overdue (90+ days): {len(severely_overdue)}
No-response 6+ months: {len(no_response)} / {no_response_total:,.2f} USD

6-MONTH NO-RESPONSE — REVIEW AT MONTHLY AR CALL
These accounts have open invoices with no client response for 6+ months.
Final deletion decision requires Leila, Greg, and Chris.

{no_response_lines}

See the AR dashboard for full account details.
"""
