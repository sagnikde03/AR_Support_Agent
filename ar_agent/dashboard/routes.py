"""
Admin Dashboard (F-23) — FastAPI routes.

Read-only internal web UI that replaces the manual Collections Sheet.
Shows all active invoices with status, stage, attempt count, last contact,
client notes, shadow queue, and performance metrics.

No write operations from the UI — all overrides go through Slack (F-20).

Mounted at /dashboard in api/main.py.
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

from ar_agent.state import invoice_db

router = APIRouter(prefix="/dashboard")


# ── HTML dashboard ────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def dashboard_html():
    """Serve the main dashboard HTML page."""
    return HTMLResponse(_DASHBOARD_HTML)


# ── API endpoints ─────────────────────────────────────────────────────────────

@router.get("/api/invoices")
async def list_invoices(
    status: str | None = Query(None),
    min_days_overdue: int = Query(0),
    limit: int = Query(100),
):
    """List all active invoices with current status and stage (paused included)."""
    invoices = invoice_db.get_all_active_invoices()
    if status:
        invoices = [inv for inv in invoices if inv.get("status") == status]
    if min_days_overdue > 0:
        invoices = [inv for inv in invoices if inv.get("days_overdue", 0) >= min_days_overdue]

    result = []
    for inv in invoices[:limit]:
        result.append({
            "invoice_id":   inv["invoice_id"],
            "account_name": inv["account_name"],
            "amount":       inv["amount_cents"] / 100,
            "currency":     inv.get("currency", "USD"),
            "due_date":     inv["due_date"],
            "status":       inv["status"],
            "days_overdue": inv.get("days_overdue", 0),
            "stage":        inv.get("cadence_stage", 0),
            "attempts":     inv.get("attempt_count", 0),
            "last_contact": inv.get("last_outreach_at"),
            "paused":       bool(inv.get("paused")),
            "p2p_date":     inv.get("current_p2p_date"),
            "non_complier": bool(inv.get("known_non_complier")),
        })

    return {"invoices": result, "total": len(result)}


@router.get("/api/invoices/{invoice_id}")
async def get_invoice_detail(invoice_id: str):
    """Detailed view of a single invoice with full history."""
    invoice = invoice_db.get_invoice(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    history = invoice_db.get_outreach_history(invoice_id)
    contacts = invoice_db.get_contacts(invoice["account_id"])
    p2p = invoice_db.get_p2p_record(invoice_id)

    notes = []
    try:
        notes = json.loads(invoice.get("notes", "[]"))
    except (json.JSONDecodeError, TypeError):
        pass

    return {
        "invoice": {
            "invoice_id":   invoice["invoice_id"],
            "account_id":   invoice["account_id"],
            "account_name": invoice["account_name"],
            "amount":       invoice["amount_cents"] / 100,
            "currency":     invoice.get("currency", "USD"),
            "due_date":     invoice["due_date"],
            "issue_date":   invoice.get("issue_date"),
            "status":       invoice["status"],
            "days_overdue": invoice.get("days_overdue", 0),
            "stage":        invoice.get("cadence_stage", 0),
            "attempts":     invoice.get("attempt_count", 0),
            "last_contact": invoice.get("last_outreach_at"),
            "paused":       bool(invoice.get("paused")),
            "pause_reason": invoice.get("pause_reason"),
            "p2p_date":     invoice.get("current_p2p_date"),
            "p2p_method":   invoice.get("current_p2p_method"),
            "p2p_complied": invoice.get("p2p_complied"),
            "non_complier": bool(invoice.get("known_non_complier")),
            "payment_link": invoice.get("freshbooks_payment_link"),
            "notes":        notes,
        },
        "outreach_history": history,
        "contacts": contacts,
        "p2p_record": p2p,
    }


@router.get("/api/metrics")
async def get_metrics():
    """Performance metrics: collection rate, false-autonomy count, avg days to payment."""
    summary = invoice_db.get_ar_summary()
    return {
        "open_invoices":    summary["open_invoice_count"],
        "total_ar":         summary["total_ar_cents"] / 100,
        "at_risk_count":    summary["at_risk_count"],
        "at_risk_total":    summary["at_risk_cents"] / 100,
        "shadow_pending":   summary["shadow_pending_count"],
        "generated_at":     datetime.now(timezone.utc).isoformat(),
    }


@router.get("/api/queue")
async def get_shadow_queue():
    """Return all emails pending shadow mode approval."""
    queue = invoice_db.get_shadow_queue()
    result = []
    for item in queue:
        result.append({
            "id":           item["id"],
            "invoice_id":   item["invoice_id"],
            "account_name": item.get("account_name"),
            "amount":       item.get("amount_cents", 0) / 100,
            "stage":        item["cadence_stage"],
            "email_to":     item["email_to"],
            "subject":      item["subject"],
            "queued_at":    item.get("created_at") or item.get("sent_at"),
        })
    return {"queue": result, "total": len(result)}


@router.get("/api/reports/daily")
async def get_daily_report_data():
    """Latest daily report data (same data as the Slack report, JSON format)."""
    summary = invoice_db.get_ar_summary()
    at_risk = invoice_db.get_at_risk_invoices()
    return {
        "open_invoices":  summary["open_invoice_count"],
        "total_ar":       summary["total_ar_cents"] / 100,
        "at_risk_count":  summary["at_risk_count"],
        "at_risk_total":  summary["at_risk_cents"] / 100,
        "sent_today":     invoice_db.get_today_outreach_count(),
        "shadow_pending": summary["shadow_pending_count"],
        "at_risk_accounts": [
            {
                "invoice_id":   inv["invoice_id"],
                "account_name": inv["account_name"],
                "amount":       inv["amount_cents"] / 100,
                "days_overdue": inv["days_overdue"],
                "stage":        inv.get("cadence_stage"),
                "last_contact": inv.get("last_outreach_at"),
            }
            for inv in sorted(at_risk, key=lambda x: x["amount_cents"], reverse=True)
        ],
    }


# ── Minimal dashboard HTML ─────────────────────────────────────────────────────
# A lightweight, dependency-free read-only view. No JS framework required.

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CapLinked AR Dashboard</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; background: #f5f5f5; color: #333; }
  .header { background: #1E5C97; color: white; padding: 16px 24px; display: flex; align-items: center; gap: 12px; }
  .header h1 { margin: 0; font-size: 20px; }
  .badge { background: rgba(255,255,255,0.2); border-radius: 4px; padding: 2px 8px; font-size: 12px; }
  .container { max-width: 1200px; margin: 24px auto; padding: 0 24px; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .stat { background: white; border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
  .stat .value { font-size: 28px; font-weight: bold; color: #1E5C97; }
  .stat .label { font-size: 12px; color: #888; margin-top: 4px; }
  .stat.danger .value { color: #e74c3c; }
  .stat.warning .value { color: #e67e22; }
  table { width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
  th { background: #1E5C97; color: white; text-align: left; padding: 12px 16px; font-size: 13px; }
  td { padding: 12px 16px; border-bottom: 1px solid #f0f0f0; font-size: 13px; }
  tr:hover td { background: #f9f9f9; }
  .status-open { color: #27ae60; font-weight: 500; }
  .status-overdue { color: #e67e22; font-weight: 500; }
  .status-at_risk { color: #e74c3c; font-weight: 500; }
  .status-suspended { color: #8e44ad; font-weight: 500; }
  .loading { text-align: center; padding: 40px; color: #888; }
  .section-title { font-size: 16px; font-weight: 600; margin: 24px 0 12px; }
  .queue-count { display: inline-block; background: #e74c3c; color: white; border-radius: 12px; padding: 1px 8px; font-size: 12px; margin-left: 8px; }
  .shadow-note { background: #fff3cd; border: 1px solid #ffc107; border-radius: 6px; padding: 12px 16px; margin-bottom: 24px; font-size: 13px; }
</style>
</head>
<body>
<div class="header">
  <h1>CapLinked AR Dashboard</h1>
  <span class="badge" id="shadowBadge"></span>
  <span style="margin-left:auto;font-size:13px;opacity:0.8">Internal — read-only. Overrides via Slack /ar</span>
</div>
<div class="container">
  <div id="shadowNote"></div>
  <div class="stats" id="statsGrid"><div class="loading">Loading...</div></div>
  <div class="section-title">Active Invoices</div>
  <table>
    <thead>
      <tr><th>Account</th><th>Amount</th><th>Due Date</th><th>Days Overdue</th><th>Status</th><th>Stage</th><th>Attempts</th><th>Last Contact</th></tr>
    </thead>
    <tbody id="invoiceRows"><tr><td colspan="8" class="loading">Loading...</td></tr></tbody>
  </table>
</div>
<script>
// Invoice fields (account names, statuses, currencies) originate in
// FreshBooks and are customer-controlled — escape before interpolating.
function esc(v) {
  if (v === null || v === undefined) return '';
  return String(v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
async function load() {
  const [metricsRes, invoicesRes, queueRes] = await Promise.all([
    fetch('/dashboard/api/metrics'), fetch('/dashboard/api/invoices?limit=200'), fetch('/dashboard/api/queue')
  ]);
  const metrics = await metricsRes.json();
  const inv = await invoicesRes.json();
  const queue = await queueRes.json();

  document.getElementById('statsGrid').innerHTML = `
    <div class="stat"><div class="value">${metrics.open_invoices}</div><div class="label">Open Invoices</div></div>
    <div class="stat"><div class="value">$${metrics.total_ar.toLocaleString('en-US',{minimumFractionDigits:0,maximumFractionDigits:0})}</div><div class="label">Total AR Balance</div></div>
    <div class="stat ${metrics.at_risk_count > 0 ? 'danger' : ''}"><div class="value">${metrics.at_risk_count}</div><div class="label">At-Risk (45+ days)</div></div>
    <div class="stat ${metrics.shadow_pending > 0 ? 'warning' : ''}"><div class="value">${metrics.shadow_pending}</div><div class="label">Shadow Queue</div></div>
  `;

  if (queue.total > 0) {
    document.getElementById('shadowNote').innerHTML = `<div class="shadow-note">⏳ <strong>${queue.total} email(s) pending Slack approval</strong> in shadow mode. Use <code>/ar queue</code> to review and approve.</div>`;
    document.getElementById('shadowBadge').textContent = `Shadow mode — ${queue.total} pending`;
  }

  const rows = inv.invoices.map(i => `<tr>
    <td><strong>${esc(i.account_name)}</strong></td>
    <td>${Number(i.amount).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})} ${esc(i.currency)}</td>
    <td>${esc(i.due_date)}</td>
    <td>${Number(i.days_overdue)}</td>
    <td class="status-${esc(i.status)}">${esc(i.status)}${i.paused?' (paused)':''}</td>
    <td>${Number(i.stage)}</td>
    <td>${Number(i.attempts)}</td>
    <td>${i.last_contact ? esc(String(i.last_contact).substring(0,10)) : '—'}</td>
  </tr>`).join('');
  document.getElementById('invoiceRows').innerHTML = rows || '<tr><td colspan="8" style="text-align:center;color:#888">No active invoices.</td></tr>';
}
load();
setInterval(load, 60000);
</script>
</body>
</html>"""
