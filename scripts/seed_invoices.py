"""
Invoice seeding utility — stand-in for FreshBooks sync until F-03 credentials land.

Two modes:

  CSV mode (real data — export from FreshBooks UI, the same export Brie
  already produces for her reconciliation workflow):

      python scripts/seed_invoices.py --csv invoices.csv

    Expected columns (header row required, extra columns ignored):
      invoice_id, account_id, account_name, amount, due_date [YYYY-MM-DD],
      currency (optional, default USD), payment_link (optional),
      contact_emails (optional — semicolon-separated)

  Demo mode (synthetic data for end-to-end shadow-mode testing):

      python scripts/seed_invoices.py --demo

Both modes are idempotent — re-running updates existing rows via upsert.
"""

import argparse
import csv
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from ar_agent.state import invoice_db  # noqa: E402


def seed_from_csv(csv_path: str) -> int:
    count = 0
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            invoice_db.upsert_invoice({
                "invoice_id":   row["invoice_id"].strip(),
                "account_id":   row["account_id"].strip(),
                "account_name": row["account_name"].strip(),
                "amount":       row["amount"].strip(),
                "due_date":     row["due_date"].strip(),
                "currency":     (row.get("currency") or "USD").strip(),
                "freshbooks_payment_link": (row.get("payment_link") or "").strip() or None,
            })
            for email in (row.get("contact_emails") or "").split(";"):
                email = email.strip()
                if email:
                    invoice_db.add_contact(
                        account_id=row["account_id"].strip(),
                        email=email,
                        contact_type="freshbooks_primary",
                        tier=1,
                    )
            count += 1
    return count


# Demo accounts covering every cadence stage plus P2P and non-complier cases.
# Days-overdue offsets: negative = not yet due.
_DEMO_INVOICES = [
    ("DEMO-001", "acc-alpha",   "Alpha Ventures",        440.00,  -6, "billing@alpha-demo.example"),    # stage 1 fires (7d pre-due)
    ("DEMO-002", "acc-bravo",   "Bravo Holdings",        890.00,   0, "ap@bravo-demo.example"),         # due today
    ("DEMO-003", "acc-charlie", "Charlie & Associates",  440.00,   8, "finance@charlie-demo.example"),  # 7d overdue
    ("DEMO-004", "acc-delta",   "Delta Partners",       1250.00,  15, "ap@delta-demo.example"),         # 14d overdue
    ("DEMO-005", "acc-echo",    "Echo Capital",          440.00,  22, "billing@echo-demo.example"),     # 21d overdue
    ("DEMO-006", "acc-foxtrot", "Foxtrot Industries",    675.00,  30, "accounts@foxtrot-demo.example"), # 28d overdue
    ("DEMO-007", "acc-golf",    "Golf Enterprises",     2100.00,  50, "ap@golf-demo.example"),          # suspension risk
    ("DEMO-008", "acc-hotel",   "Hotel Group LLC",      7500.00,  10, "cfo@hotel-demo.example"),        # above $5k threshold → escalates
]


def seed_demo() -> int:
    today = date.today()
    for invoice_id, account_id, account_name, amount, days_overdue, email in _DEMO_INVOICES:
        due = today - timedelta(days=days_overdue)
        invoice_db.upsert_invoice({
            "invoice_id":   invoice_id,
            "account_id":   account_id,
            "account_name": account_name,
            "amount":       amount,
            "due_date":     due.isoformat(),
            "currency":     "USD",
            "freshbooks_payment_link": f"https://my.freshbooks.com/#/link/{invoice_id.lower()}",
        })
        invoice_db.add_contact(account_id, email, "freshbooks_primary", tier=1)
        invoice_db.update_days_overdue(invoice_id, days_overdue)

    # One P2P commitment already past its promise date (non-compliance path).
    # Cleared first so repeated --demo runs leave exactly one record rather
    # than stacking a duplicate commitment on every run.
    invoice_db.delete_p2p_records("DEMO-005")
    invoice_db.record_p2p_commitment("DEMO-005", (today - timedelta(days=5)).isoformat(), "check")
    return len(_DEMO_INVOICES)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the AR invoice state engine.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--csv", help="Path to a FreshBooks-export CSV")
    group.add_argument("--demo", action="store_true", help="Load synthetic demo invoices")
    args = parser.parse_args()

    invoice_db.init_db()
    if args.demo:
        count = seed_demo()
        print(f"Seeded {count} demo invoices (all cadence stages + P2P + high-value cases).")
    else:
        count = seed_from_csv(args.csv)
        print(f"Seeded {count} invoices from {args.csv}.")

    summary = invoice_db.get_ar_summary()
    print(f"State engine now has {summary['open_invoice_count']} open invoices, "
          f"{summary['total_ar_cents']/100:,.2f} USD outstanding.")


if __name__ == "__main__":
    main()
