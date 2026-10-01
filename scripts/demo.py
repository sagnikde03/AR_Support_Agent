"""
AR Agent demo launcher — one command per demo step, no env vars to remember.

    python scripts/demo.py seed        # fresh demo DB with 8 invoices
    python scripts/demo.py server      # start API + dashboard on :8001 (leave running)
    python scripts/demo.py cadence     # run one cadence cycle (shadow-queues emails)
    python scripts/demo.py reply-payment   # simulate "we sent check #4021" reply + show result
    python scripts/demo.py reply-w9        # simulate a W9 request + show the guard fired
    python scripts/demo.py report      # generate the daily Slack report
    python scripts/demo.py status      # /ar status + /ar queue output
    python scripts/demo.py approve <id>    # approve a shadow-queued email (CLI stand-in for the Slack button)
    python scripts/demo.py inspect <invoice_id>   # show one invoice's state + notes (verify after a curl)
    python scripts/demo.py lastlog     # show the last orchestrator audit-log entry

Demo environment (set automatically, .env still supplies API keys / Slack):
    AR_DB_PATH=data/demo_ar_state.db   SHADOW_MODE=true
    BH_START_HOUR=0  BH_END_HOUR=24    ENVIRONMENT=dev
"""

import os
import sys
from pathlib import Path

AR_AGENT_ROOT = Path(__file__).parent.parent

# Demo env must be set BEFORE any ar_agent import (modules read env at import).
# load_dotenv() in the modules will NOT override these.
os.environ.setdefault("AR_DB_PATH", "data/demo_ar_state.db")
os.environ.setdefault("SHADOW_MODE", "true")
os.environ.setdefault("BH_START_HOUR", "0")
os.environ.setdefault("BH_END_HOUR", "24")
os.environ.setdefault("ENVIRONMENT", "dev")

sys.path.insert(0, str(AR_AGENT_ROOT))
os.chdir(AR_AGENT_ROOT)

# Load .env (API keys, SLACK_WEBHOOK_URL for the demo channel). Does not
# override the demo defaults above.
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

WEBHOOK_URL = "http://localhost:8001/webhook/inbound"


def cmd_seed() -> None:
    db = Path(os.environ["AR_DB_PATH"])
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()
    from scripts.seed_invoices import seed_demo
    from ar_agent.state import invoice_db
    invoice_db.init_db()
    n = seed_demo()
    summary = invoice_db.get_ar_summary()
    print(f"Fresh demo DB: {n} invoices seeded, "
          f"{summary['total_ar_cents']/100:,.2f} USD outstanding.")
    print("Next: python scripts/demo.py server")


def cmd_server() -> None:
    import uvicorn
    print("AR Agent demo server -> http://localhost:8001/dashboard  (Ctrl+C to stop)")
    uvicorn.run("api.main:app", port=8001, log_level="warning")


def cmd_cadence() -> None:
    from ar_agent.cadence import reminder_actions
    result = reminder_actions.run_cadence_cycle()
    print("Cadence cycle:", result)
    from ar_agent.state import invoice_db
    queue = invoice_db.get_shadow_queue()
    print(f"\nShadow queue now holds {len(queue)} email(s) awaiting approval.")
    print("Refresh the dashboard to see the pending banner, or run: python scripts/demo.py status")


def _post_reply(sender: str, subject: str, body: str) -> None:
    import requests
    resp = requests.post(WEBHOOK_URL, json={
        "FromFull": {"Email": sender},
        "Subject": subject,
        "TextBody": body,
    }, timeout=120)
    print(f"Webhook response: {resp.status_code} {resp.json()}")


def cmd_reply_payment() -> None:
    print('Simulating customer reply: "we sent check #4021 on Tuesday..."\n')
    _post_reply(
        "finance@charlie-demo.example",
        "Re: Charlie & Associates — Invoice #DEMO-003",
        "Hi, we sent check #4021 on Tuesday for the full amount. "
        "Should arrive within the week. Thanks, Dana",
    )
    import json
    from ar_agent.state import invoice_db
    inv = invoice_db.get_invoice("DEMO-003")
    print(f"\nDEMO-003 after the reply:")
    print(f"  cadence paused : {bool(inv['paused'])} ({inv['pause_reason']})")
    for note in json.loads(inv["notes"]):
        print(f"  note           : {note['text']}")


def cmd_reply_w9() -> None:
    print('Simulating customer reply asking for a W-9 form...\n')
    _post_reply(
        "ap@delta-demo.example",
        "Re: Delta Partners — Invoice #DEMO-004",
        "Before we can process payment we need you to send us a completed "
        "W-9 form for our vendor records.",
    )
    import json
    log_files = sorted(Path("logs").glob("*.jsonl"))
    if log_files:
        last = json.loads(open(log_files[-1], encoding="utf-8").readlines()[-1])
        print(f"\nAudit log: action={last.get('action_taken')} "
              f"w9_guard={last.get('w9_guard')} provider={last.get('provider')}")
        print("provider=none -> the request never reached the LLM; escalated deterministically.")


def cmd_report() -> None:
    from ar_agent.reporting import daily_report
    data = daily_report.generate()
    posted = data.pop("posted", False)
    print("Daily report data:", data)
    print(f"Slack delivery: {'posted' if posted else 'NOT posted (Slack not configured)'}")


def cmd_status() -> None:
    from ar_agent.slack_bot.command_handler import handle_command
    print(handle_command("ar", "status DEMO-005", "U_DEMO", "demo-user"))
    print()
    print(handle_command("ar", "queue", "U_DEMO", "demo-user"))
    print()
    print("NOTE: `/ar approve N` is what users will type IN SLACK once deployed.")
    print("      Locally, use:  python scripts/demo.py approve N")


def cmd_inspect(invoice_id: str) -> None:
    import json
    from ar_agent.state import invoice_db
    inv = invoice_db.get_invoice(invoice_id)
    if not inv:
        print(f"Invoice {invoice_id} not found.")
        return
    print(f"{invoice_id} — {inv['account_name']}")
    print(f"  amount         : {inv['amount_cents']/100:,.2f} {inv.get('currency', 'USD')}")
    print(f"  status         : {inv['status']} | stage {inv.get('cadence_stage', 0)} | "
          f"{inv.get('days_overdue', 0)} days overdue")
    print(f"  cadence paused : {bool(inv['paused'])}"
          + (f" ({inv['pause_reason']})" if inv.get("pause_reason") else ""))
    if inv.get("current_p2p_date"):
        print(f"  P2P on record  : {inv['current_p2p_date']} via {inv.get('current_p2p_method') or '?'}")
    for note in json.loads(inv.get("notes", "[]")):
        print(f"  note           : {note['text']}")


def cmd_lastlog() -> None:
    import json
    log_files = sorted(Path("logs").glob("*.jsonl"))
    if not log_files:
        print("No orchestrator logs yet.")
        return
    last = json.loads(open(log_files[-1], encoding="utf-8").readlines()[-1])
    keys = ("interaction_id", "invoice_id", "classification", "category_name",
            "confidence", "action_taken", "w9_guard", "provider", "summary")
    for k in keys:
        if k in last:
            print(f"  {k:15}: {last[k]}")


def cmd_approve(outreach_id: str) -> None:
    from ar_agent.slack_bot.command_handler import handle_command
    print(handle_command("ar", f"approve {outreach_id}", "U_DEMO", "demo-user"))
    print("\n(Note: with FreshBooks stubbed, cadence sends abort on the missing "
          "invoice PDF — that failure IS the attachment contract working.)")


def main() -> None:
    commands = {
        "seed": cmd_seed,
        "server": cmd_server,
        "cadence": cmd_cadence,
        "reply-payment": cmd_reply_payment,
        "reply-w9": cmd_reply_w9,
        "report": cmd_report,
        "status": cmd_status,
    }
    if len(sys.argv) < 2:
        print(__doc__)
        return
    step = sys.argv[1]
    if step == "approve":
        if len(sys.argv) < 3:
            print("Usage: python scripts/demo.py approve <outreach_id>")
            return
        cmd_approve(sys.argv[2])
        return
    if step == "inspect":
        if len(sys.argv) < 3:
            print("Usage: python scripts/demo.py inspect <invoice_id>")
            return
        cmd_inspect(sys.argv[2])
        return
    if step == "lastlog":
        cmd_lastlog()
        return
    fn = commands.get(step)
    if not fn:
        print(f"Unknown step '{step}'.\n{__doc__}")
        return
    fn()


if __name__ == "__main__":
    main()
