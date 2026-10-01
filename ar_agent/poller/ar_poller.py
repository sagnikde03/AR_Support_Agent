"""
4-Hour Polling Loop & Invoice Reconciliation (F-08).

Recurring background process (supervisord-managed) that runs every 4 hours:
  1. Polls FreshBooks for new/updated invoices and payment status
  2. Syncs invoice state to the SQLite state engine (replacing the manual
     Excel + ListDiff reconciliation workflow)
  3. Detects failed autopay attempts and routes to failed_payment_handler
  4. Runs the cadence engine for all invoices needing action
  5. Checks P2P compliance for overdue commitments
  6. Fires scheduled reports (daily at 8 AM, weekly on Monday, monthly on 1st)

FreshBooks polling (steps 1-3) depends on F-03 credentials. The loop structure
and all other steps run independently.
"""

import logging
import os
import time
from datetime import datetime, timezone

from ar_agent.cadence import reminder_actions, scheduler
from ar_agent.reporting import daily_report, monthly_report, weekly_report
from ar_agent.state import invoice_db
from ar_agent.agent import escalation_engine
from ar_agent.poller import failed_payment_handler

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = int(os.environ.get("AR_POLL_INTERVAL_SECONDS", str(4 * 3600)))

# Reports fire on the first poll cycle at/after 13:00 UTC (8 AM ET) of the
# relevant period. Markers live in the settings table so restarts and
# multi-hour poll gaps never double-fire or skip a report.
_REPORT_HOUR_UTC = int(os.environ.get("AR_REPORT_HOUR_UTC", "13"))


# Eligibility checks are pure — they never write. run_once() persists the
# marker only after a report is actually delivered, so a failed report is
# retried on the next cycle instead of being silently skipped for the period.

def _daily_period(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _weekly_period(now: datetime) -> str:
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _monthly_period(now: datetime) -> str:
    return now.strftime("%Y-%m")


def _should_fire_daily_report(now: datetime) -> bool:
    return (
        now.hour >= _REPORT_HOUR_UTC
        and invoice_db.get_setting("last_daily_report") != _daily_period(now)
    )


def _should_fire_weekly_report(now: datetime) -> bool:
    # Once per ISO week, from the report hour on Monday (or later in the week
    # if the poller was down on Monday).
    in_window = now.weekday() > 0 or now.hour >= _REPORT_HOUR_UTC
    return in_window and invoice_db.get_setting("last_weekly_report") != _weekly_period(now)


def _should_fire_monthly_report(now: datetime) -> bool:
    # Once per calendar month, from the report hour on the 1st (or later if
    # the poller was down).
    in_window = now.day > 1 or now.hour >= _REPORT_HOUR_UTC
    return in_window and invoice_db.get_setting("last_monthly_report") != _monthly_period(now)


def _sync_freshbooks() -> dict:
    """
    Poll FreshBooks and sync invoice state. Stubbed until F-03 credentials
    are configured. Returns summary of changes.
    """
    # Only the import is guarded as "not configured" — a failure anywhere in
    # the sync itself must surface as a real error, not a silent skip.
    try:
        from ar_agent.integrations import freshbooks_client
    except ImportError:
        logger.debug("FreshBooks client not available (F-03 pending) — sync skipped")
        return {"status": "skipped", "reason": "freshbooks_client not configured"}

    try:
        invoices = freshbooks_client.list_invoices()
        new_count = 0
        updated_count = 0
        failed_payments = []

        for fb_invoice in invoices:
            existing = invoice_db.get_invoice(fb_invoice["invoice_id"])
            invoice_db.upsert_invoice(fb_invoice)

            if not existing:
                new_count += 1
                # Add primary email contact for new invoices
                for email in fb_invoice.get("contact_emails", []):
                    invoice_db.add_contact(
                        account_id=fb_invoice["account_id"],
                        email=email,
                        contact_type="freshbooks_primary",
                        tier=1,
                    )
            else:
                # Detect status changes (paid, failed autopay)
                if fb_invoice.get("status") == "paid" and existing.get("status") != "paid":
                    invoice_db.mark_payment_received(fb_invoice["invoice_id"])
                elif fb_invoice.get("autopay_failed"):
                    failed_payments.append({
                        "invoice_id": fb_invoice["invoice_id"],
                        "failure_reason": fb_invoice.get("autopay_failure_reason", "Unknown"),
                    })
                updated_count += 1

        return {
            "status": "synced",
            "new": new_count,
            "updated": updated_count,
            "failed_payments": failed_payments,
        }

    except Exception as exc:
        logger.error("FreshBooks sync failed: %s", exc, exc_info=True)
        return {"status": "failed", "reason": str(exc)}


def run_once() -> dict:
    """
    Execute one full poll cycle. Called on each iteration of the loop.
    """
    now = datetime.now(timezone.utc)
    logger.info("AR poller cycle starting at %s", now.isoformat())
    results: dict = {"timestamp": now.isoformat()}

    # Step 1: Sync FreshBooks (requires F-03 credentials)
    sync_result = _sync_freshbooks()
    results["freshbooks_sync"] = sync_result

    # Step 2: Handle failed payments detected in sync
    for fp in sync_result.get("failed_payments", []):
        fp_result = failed_payment_handler.handle_failed_payment(
            fp["invoice_id"], fp["failure_reason"]
        )
        results.setdefault("failed_payments_handled", []).append(fp_result)

    # Step 3: Run cadence engine
    cadence_result = reminder_actions.run_cadence_cycle()
    results["cadence"] = cadence_result

    # Step 4: Check P2P compliance
    non_complied = scheduler.check_p2p_compliance()
    for record in non_complied:
        invoice = invoice_db.get_invoice(record["invoice_id"])
        if invoice:
            escalation_engine.escalate_p2p_non_compliance(record)
    results["p2p_non_complied"] = len(non_complied)

    # Step 5: Scheduled reports. The period marker is written only after a
    # report is generated successfully — a failure leaves the marker unset so
    # the next cycle retries instead of skipping the period entirely.
    for label, due, generate, marker_key, period in (
        ("daily_report",   _should_fire_daily_report(now),   daily_report.generate,
         "last_daily_report",   _daily_period(now)),
        ("weekly_report",  _should_fire_weekly_report(now),  weekly_report.generate,
         "last_weekly_report",  _weekly_period(now)),
        ("monthly_report", _should_fire_monthly_report(now), monthly_report.generate,
         "last_monthly_report", _monthly_period(now)),
    ):
        if not due:
            continue
        try:
            generate()
            invoice_db.set_setting(marker_key, period, "ar_poller")
            results[label] = "sent"
        except Exception as exc:
            logger.error("%s failed (will retry next cycle): %s", label, exc, exc_info=True)
            results[label] = f"failed: {exc}"

    logger.info("AR poller cycle complete: %s", results)
    return results


def run_loop() -> None:
    """Main polling loop. Runs indefinitely, managed by supervisord."""
    logger.info("AR poller starting — interval %ds", _POLL_INTERVAL_SECONDS)
    invoice_db.init_db()

    while True:
        try:
            run_once()
        except Exception as exc:
            logger.error("Poller cycle failed: %s", exc, exc_info=True)

        logger.info("AR poller sleeping %ds", _POLL_INTERVAL_SECONDS)
        time.sleep(_POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    run_loop()
