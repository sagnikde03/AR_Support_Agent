"""
Invoice State Engine (F-02).

SQLite-backed single source of record for all AR tracking — replacing the
manual Collections Sheet. Every feature reads from or writes to this engine.

Schema notes:
  - amounts stored as cents (INTEGER) to avoid float rounding
  - datetimes stored as ISO-8601 strings (UTC)
  - contacts table holds all email addresses per account across all tiers
  - p2p_records holds full history; invoices table holds current P2P state
  - outreach_log tracks every send (sent or shadow-queued) with status
"""

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DB_PATH = Path(os.environ.get("AR_DB_PATH", Path(__file__).parent.parent.parent / "data" / "ar_state.db"))
_db_lock = threading.Lock()


def _ensure_data_dir() -> None:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def _conn():
    _ensure_data_dir()
    with _db_lock:
        con = sqlite3.connect(str(_DB_PATH))
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()


# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    invoice_id          TEXT PRIMARY KEY,
    account_id          TEXT NOT NULL,
    account_name        TEXT NOT NULL,
    amount_cents        INTEGER NOT NULL,
    currency            TEXT NOT NULL DEFAULT 'USD',
    due_date            TEXT NOT NULL,
    issue_date          TEXT,
    status              TEXT NOT NULL DEFAULT 'open',
    cadence_stage       INTEGER NOT NULL DEFAULT 0,
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    last_outreach_at    TEXT,
    paused              INTEGER NOT NULL DEFAULT 0,
    pause_reason        TEXT,
    notes               TEXT NOT NULL DEFAULT '[]',
    freshbooks_payment_link TEXT,
    current_p2p_date    TEXT,
    current_p2p_method  TEXT,
    p2p_complied        INTEGER,
    known_non_complier  INTEGER NOT NULL DEFAULT 0,
    actual_payment_date TEXT,
    days_overdue        INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_contacts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   TEXT NOT NULL,
    email        TEXT NOT NULL,
    contact_type TEXT NOT NULL,
    tier         INTEGER NOT NULL DEFAULT 1,
    is_active    INTEGER NOT NULL DEFAULT 1,
    added_at     TEXT NOT NULL,
    UNIQUE(account_id, email)
);

CREATE INDEX IF NOT EXISTS idx_contacts_account ON account_contacts(account_id);
CREATE INDEX IF NOT EXISTS idx_contacts_email   ON account_contacts(email);

CREATE TABLE IF NOT EXISTS outreach_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id          TEXT NOT NULL REFERENCES invoices(invoice_id),
    created_at          TEXT,
    sent_at             TEXT,
    cadence_stage       INTEGER NOT NULL,
    email_to            TEXT NOT NULL,
    email_cc            TEXT,
    subject             TEXT NOT NULL,
    html_body           TEXT,
    text_body           TEXT,
    template_name       TEXT,
    status              TEXT NOT NULL DEFAULT 'queued_shadow',
    shadow_mode         INTEGER NOT NULL DEFAULT 1,
    approved_by         TEXT,
    approved_at         TEXT,
    postmark_message_id TEXT,
    error_message       TEXT
);

CREATE INDEX IF NOT EXISTS idx_outreach_invoice ON outreach_log(invoice_id);
CREATE INDEX IF NOT EXISTS idx_outreach_status  ON outreach_log(status);

CREATE TABLE IF NOT EXISTS inbound_replies (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id              TEXT REFERENCES invoices(invoice_id),
    received_at             TEXT NOT NULL,
    from_email              TEXT NOT NULL,
    subject                 TEXT,
    body_text               TEXT NOT NULL,
    classification          INTEGER,
    classification_name     TEXT,
    confidence              REAL,
    payment_signal          INTEGER NOT NULL DEFAULT 0,
    payment_signal_details  TEXT,
    p2p_date                TEXT,
    p2p_payment_method      TEXT,
    processed               INTEGER NOT NULL DEFAULT 0,
    processed_at            TEXT,
    action_taken            TEXT
);

CREATE INDEX IF NOT EXISTS idx_replies_invoice ON inbound_replies(invoice_id);

CREATE TABLE IF NOT EXISTS p2p_records (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id           TEXT NOT NULL REFERENCES invoices(invoice_id),
    committed_at         TEXT NOT NULL,
    promise_date         TEXT NOT NULL,
    payment_method_stated TEXT,
    status               TEXT NOT NULL DEFAULT 'pending',
    actual_payment_date  TEXT,
    check_number         TEXT,
    compliance_checked_at TEXT,
    reminder_sent        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_p2p_invoice ON p2p_records(invoice_id);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
"""


def init_db() -> None:
    with _conn() as con:
        con.executescript(_SCHEMA)
        _migrate(con)
    logger.info("AR state engine initialised at %s", _DB_PATH)


def _migrate(con: sqlite3.Connection) -> None:
    """Idempotent column additions for databases created before schema changes."""
    existing = {row["name"] for row in con.execute("PRAGMA table_info(outreach_log)").fetchall()}
    for column in ("html_body", "text_body", "created_at"):
        if column not in existing:
            con.execute(f"ALTER TABLE outreach_log ADD COLUMN {column} TEXT")
            logger.info("Migrated outreach_log: added column %s", column)
    if "created_at" not in existing:
        # Backfill: pre-migration rows were only written on send
        con.execute("UPDATE outreach_log SET created_at = sent_at WHERE created_at IS NULL")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dollars_to_cents(amount) -> int:
    """Exact cent conversion — avoids float penny drift (e.g. 1.005 → 100, not 99)."""
    return int(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return dict(row)


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


# ── Invoice CRUD ──────────────────────────────────────────────────────────────

def upsert_invoice(data: dict) -> None:
    """Create or update an invoice record. Merges on invoice_id."""
    now = _now()
    invoice_id = data["invoice_id"]
    amount_cents = _dollars_to_cents(data["amount"])

    with _conn() as con:
        existing = con.execute(
            "SELECT invoice_id FROM invoices WHERE invoice_id = ?", (invoice_id,)
        ).fetchone()

        if existing:
            con.execute(
                """UPDATE invoices SET
                    account_name        = ?,
                    amount_cents        = ?,
                    due_date            = ?,
                    issue_date          = ?,
                    freshbooks_payment_link = ?,
                    updated_at          = ?
                WHERE invoice_id = ?""",
                (
                    data["account_name"],
                    amount_cents,
                    data["due_date"],
                    data.get("issue_date"),
                    data.get("freshbooks_payment_link"),
                    now,
                    invoice_id,
                ),
            )
        else:
            con.execute(
                """INSERT INTO invoices
                    (invoice_id, account_id, account_name, amount_cents, currency,
                     due_date, issue_date, status, cadence_stage, attempt_count,
                     freshbooks_payment_link, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'open', 0, 0, ?, ?, ?)""",
                (
                    invoice_id,
                    data["account_id"],
                    data["account_name"],
                    amount_cents,
                    data.get("currency", "USD"),
                    data["due_date"],
                    data.get("issue_date"),
                    data.get("freshbooks_payment_link"),
                    now,
                    now,
                ),
            )


def get_invoice(invoice_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM invoices WHERE invoice_id = ?", (invoice_id,)
        ).fetchone()
    return _row_to_dict(row)


def get_invoice_by_contact_email(email: str) -> dict | None:
    """Find the most recently active invoice for an account matching the given email."""
    with _conn() as con:
        row = con.execute(
            """SELECT i.* FROM invoices i
               JOIN account_contacts c ON c.account_id = i.account_id
               WHERE c.email = ? AND i.status NOT IN ('paid', 'written_off')
               ORDER BY i.due_date DESC LIMIT 1""",
            (email,),
        ).fetchone()
    return _row_to_dict(row)


def update_status(invoice_id: str, new_status: str, reason: str | None = None) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE invoices SET status = ?, updated_at = ? WHERE invoice_id = ?",
            (new_status, _now(), invoice_id),
        )
    logger.info("Invoice %s status → %s (%s)", invoice_id, new_status, reason or "")


def update_days_overdue(invoice_id: str, days: int) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE invoices SET days_overdue = ?, updated_at = ? WHERE invoice_id = ?",
            (days, _now(), invoice_id),
        )


def increment_attempt(invoice_id: str, stage: int) -> None:
    with _conn() as con:
        con.execute(
            """UPDATE invoices SET
                attempt_count   = attempt_count + 1,
                cadence_stage   = ?,
                last_outreach_at = ?,
                updated_at      = ?
               WHERE invoice_id = ?""",
            (stage, _now(), _now(), invoice_id),
        )


def mark_payment_received(invoice_id: str, payment_date: str | None = None) -> None:
    actual = payment_date or _now()[:10]
    with _conn() as con:
        con.execute(
            """UPDATE invoices SET
                status              = 'paid',
                actual_payment_date = ?,
                p2p_complied        = 1,
                updated_at          = ?
               WHERE invoice_id = ?""",
            (actual, _now(), invoice_id),
        )


def pause_cadence(invoice_id: str, reason: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE invoices SET paused = 1, pause_reason = ?, updated_at = ? WHERE invoice_id = ?",
            (reason, _now(), invoice_id),
        )


def resume_cadence(invoice_id: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE invoices SET paused = 0, pause_reason = NULL, updated_at = ? WHERE invoice_id = ?",
            (_now(), invoice_id),
        )


def add_note(invoice_id: str, note_text: str, added_by: str = "system") -> None:
    with _conn() as con:
        row = con.execute(
            "SELECT notes FROM invoices WHERE invoice_id = ?", (invoice_id,)
        ).fetchone()
        if not row:
            return
        notes = json.loads(row["notes"])
        notes.append({"text": note_text, "by": added_by, "at": _now()})
        con.execute(
            "UPDATE invoices SET notes = ?, updated_at = ? WHERE invoice_id = ?",
            (json.dumps(notes), _now(), invoice_id),
        )


def get_invoices_for_cadence() -> list[dict]:
    """All non-paused invoices that are not paid or written off."""
    with _conn() as con:
        rows = con.execute(
            """SELECT * FROM invoices
               WHERE status NOT IN ('paid', 'written_off')
               AND paused = 0
               ORDER BY days_overdue DESC""",
        ).fetchall()
    return _rows_to_dicts(rows)


def get_all_active_invoices() -> list[dict]:
    """
    Every invoice still owing, including paused ones — for the dashboard.

    Distinct from get_invoices_for_cadence(), which excludes paused invoices
    because they must not receive outreach. A paused invoice is still very
    much active AR and has to stay visible to Brie and Leila.
    """
    with _conn() as con:
        rows = con.execute(
            """SELECT * FROM invoices
               WHERE status NOT IN ('paid', 'written_off', 'draft')
               ORDER BY days_overdue DESC""",
        ).fetchall()
    return _rows_to_dicts(rows)


def get_overdue_invoices(min_days: int = 1, max_days: int | None = None) -> list[dict]:
    with _conn() as con:
        if max_days is not None:
            rows = con.execute(
                "SELECT * FROM invoices WHERE days_overdue >= ? AND days_overdue <= ? AND status NOT IN ('paid','written_off') ORDER BY days_overdue DESC",
                (min_days, max_days),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM invoices WHERE days_overdue >= ? AND status NOT IN ('paid','written_off') ORDER BY days_overdue DESC",
                (min_days,),
            ).fetchall()
    return _rows_to_dicts(rows)


def get_at_risk_invoices() -> list[dict]:
    """Accounts 45+ days overdue — candidates for suspension flag."""
    return get_overdue_invoices(min_days=45)


def get_no_response_invoices(months: int = 6) -> list[dict]:
    """Invoices with no client reply in the last N months — monthly report flagging."""
    min_days = months * 30
    with _conn() as con:
        rows = con.execute(
            """SELECT i.* FROM invoices i
               WHERE i.status NOT IN ('paid','written_off')
               AND i.days_overdue >= ?
               AND NOT EXISTS (
                   SELECT 1 FROM inbound_replies r
                   WHERE r.invoice_id = i.invoice_id
                   AND julianday(r.received_at) > julianday('now', ?)
               )
               ORDER BY i.days_overdue DESC""",
            (min_days, f"-{min_days} days"),
        ).fetchall()
    return _rows_to_dicts(rows)


def mark_invoice_written_off(invoice_id: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE invoices SET status = 'written_off', updated_at = ? WHERE invoice_id = ?",
            (_now(), invoice_id),
        )


# ── Contacts ──────────────────────────────────────────────────────────────────

def add_contact(
    account_id: str,
    email: str,
    contact_type: str,
    tier: int = 1,
) -> None:
    """
    contact_type: 'freshbooks_primary' | 'freshbooks_additional' | 'workspace_admin' | 'tier3'
    tier: 1 (FreshBooks), 2 (workspace admin), 3 (LinkedIn/website)
    """
    with _conn() as con:
        con.execute(
            """INSERT INTO account_contacts (account_id, email, contact_type, tier, added_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(account_id, email) DO UPDATE SET
                   contact_type = excluded.contact_type,
                   tier = excluded.tier,
                   is_active = 1""",
            (account_id, email.lower().strip(), contact_type, tier, _now()),
        )


def get_contacts(account_id: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM account_contacts WHERE account_id = ? AND is_active = 1 ORDER BY tier, id",
            (account_id,),
        ).fetchall()
    return _rows_to_dicts(rows)


def get_tier1_contacts(account_id: str) -> list[str]:
    with _conn() as con:
        rows = con.execute(
            "SELECT email FROM account_contacts WHERE account_id = ? AND tier = 1 AND is_active = 1",
            (account_id,),
        ).fetchall()
    return [r["email"] for r in rows]


def get_tier2_contacts(account_id: str) -> list[str]:
    with _conn() as con:
        rows = con.execute(
            "SELECT email FROM account_contacts WHERE account_id = ? AND tier = 2 AND is_active = 1",
            (account_id,),
        ).fetchall()
    return [r["email"] for r in rows]


# ── Outreach log ──────────────────────────────────────────────────────────────

def record_outreach(
    invoice_id: str,
    stage: int,
    email_to: str,
    subject: str,
    email_cc: str | None = None,
    template_name: str | None = None,
    shadow_mode: bool = True,
    html_body: str | None = None,
    text_body: str | None = None,
) -> int:
    """Insert an outreach record. Returns the row ID (used for shadow approvals).

    html_body/text_body hold the fully rendered email content so a
    shadow-queued item can be sent verbatim on approval.
    """
    # Live sends start as 'pending' — update_outreach_sent() marks them 'sent'
    # (and stamps sent_at) only after Postmark confirms delivery.
    status = "queued_shadow" if shadow_mode else "pending"
    sent_at = None
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO outreach_log
                (invoice_id, created_at, sent_at, cadence_stage, email_to, email_cc, subject,
                 html_body, text_body, template_name, status, shadow_mode)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (invoice_id, _now(), sent_at, stage, email_to, email_cc, subject,
             html_body, text_body, template_name, status, int(shadow_mode)),
        )
        return cur.lastrowid


def update_outreach_sent(outreach_id: int, postmark_message_id: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE outreach_log SET status = 'sent', sent_at = ?, postmark_message_id = ? WHERE id = ?",
            (_now(), postmark_message_id, outreach_id),
        )


def update_outreach_failed(outreach_id: int, error: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE outreach_log SET status = 'failed', error_message = ? WHERE id = ?",
            (error, outreach_id),
        )


def approve_shadow_item(outreach_id: int, approved_by: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE outreach_log SET status = 'approved', approved_by = ?, approved_at = ? WHERE id = ?",
            (approved_by, _now(), outreach_id),
        )


def skip_shadow_item(outreach_id: int, skipped_by: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE outreach_log SET status = 'skipped', approved_by = ?, approved_at = ? WHERE id = ?",
            (skipped_by, _now(), outreach_id),
        )


def get_outreach_record(outreach_id: int) -> dict | None:
    """Fetch a single outreach log record by ID."""
    with _conn() as con:
        row = con.execute("SELECT * FROM outreach_log WHERE id = ?", (outreach_id,)).fetchone()
    return _row_to_dict(row)


def get_shadow_queue() -> list[dict]:
    """All outreach records pending shadow approval."""
    with _conn() as con:
        rows = con.execute(
            """SELECT o.*, i.account_name, i.amount_cents, i.currency
               FROM outreach_log o
               JOIN invoices i ON i.invoice_id = o.invoice_id
               WHERE o.status = 'queued_shadow'
               ORDER BY o.id""",
        ).fetchall()
    return _rows_to_dicts(rows)


def get_outreach_history(invoice_id: str) -> dict:
    """Summary stats for the outreach log of one invoice."""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM outreach_log WHERE invoice_id = ? ORDER BY sent_at",
            (invoice_id,),
        ).fetchall()
    records = _rows_to_dicts(rows)
    sent = [r for r in records if r["status"] == "sent"]
    return {
        "total_sent": len(sent),
        "total_attempts": len(records),
        "last_stage": sent[-1]["cadence_stage"] if sent else None,
        "last_sent_at": sent[-1]["sent_at"] if sent else None,
        "records": records,
    }


def get_today_outreach_count() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _conn() as con:
        row = con.execute(
            "SELECT COUNT(*) as cnt FROM outreach_log WHERE status = 'sent' AND sent_at LIKE ?",
            (f"{today}%",),
        ).fetchone()
    return row["cnt"]


# ── Inbound replies ───────────────────────────────────────────────────────────

def record_inbound_reply(
    invoice_id: str | None,
    from_email: str,
    body_text: str,
    subject: str = "",
) -> int:
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO inbound_replies
                (invoice_id, received_at, from_email, subject, body_text)
               VALUES (?, ?, ?, ?, ?)""",
            (invoice_id, _now(), from_email.lower().strip(), subject, body_text),
        )
        return cur.lastrowid


def update_reply_classification(
    reply_id: int,
    classification: int,
    category_name: str,
    confidence: float,
    payment_signal: bool = False,
    payment_signal_details: dict | None = None,
    p2p_date: str | None = None,
    p2p_payment_method: str | None = None,
    action_taken: str = "",
) -> None:
    with _conn() as con:
        con.execute(
            """UPDATE inbound_replies SET
                classification         = ?,
                classification_name    = ?,
                confidence             = ?,
                payment_signal         = ?,
                payment_signal_details = ?,
                p2p_date               = ?,
                p2p_payment_method     = ?,
                processed              = 1,
                processed_at           = ?,
                action_taken           = ?
               WHERE id = ?""",
            (
                classification,
                category_name,
                confidence,
                int(payment_signal),
                json.dumps(payment_signal_details) if payment_signal_details else None,
                p2p_date,
                p2p_payment_method,
                _now(),
                action_taken,
                reply_id,
            ),
        )


# ── P2P records ───────────────────────────────────────────────────────────────

def record_p2p_commitment(
    invoice_id: str,
    promise_date: str,
    payment_method: str | None = None,
) -> None:
    """Record a new P2P commitment and update the invoice's current P2P fields."""
    with _conn() as con:
        con.execute(
            """INSERT INTO p2p_records
                (invoice_id, committed_at, promise_date, payment_method_stated)
               VALUES (?, ?, ?, ?)""",
            (invoice_id, _now(), promise_date, payment_method),
        )
        con.execute(
            """UPDATE invoices SET
                current_p2p_date   = ?,
                current_p2p_method = ?,
                p2p_complied       = NULL,
                updated_at         = ?
               WHERE invoice_id = ?""",
            (promise_date, payment_method, _now(), invoice_id),
        )


def get_p2p_record(invoice_id: str) -> dict | None:
    """Return the most recent P2P record for an invoice."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM p2p_records WHERE invoice_id = ? ORDER BY committed_at DESC LIMIT 1",
            (invoice_id,),
        ).fetchone()
    return _row_to_dict(row)


def get_pending_p2p_records() -> list[dict]:
    """All P2P commitments whose promise date has passed and haven't been checked."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _conn() as con:
        rows = con.execute(
            """SELECT p.*, i.account_name, i.amount_cents, i.currency
               FROM p2p_records p
               JOIN invoices i ON i.invoice_id = p.invoice_id
               WHERE p.status = 'pending'
               AND p.promise_date <= ?
               AND i.status NOT IN ('paid','written_off')""",
            (today,),
        ).fetchall()
    return _rows_to_dicts(rows)


def mark_p2p_non_complied(p2p_id: int) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE p2p_records SET status = 'non_complied', compliance_checked_at = ? WHERE id = ?",
            (_now(), p2p_id),
        )


def mark_non_complier(invoice_id: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE invoices SET known_non_complier = 1, updated_at = ? WHERE invoice_id = ?",
            (_now(), invoice_id),
        )


def delete_p2p_records(invoice_id: str) -> int:
    """
    Remove all P2P records for an invoice. Returns the number deleted.
    Used by demo seeding to stay idempotent; not part of the agent's runtime
    flow — commitments are historical record and are never deleted in
    production.
    """
    with _conn() as con:
        cur = con.execute("DELETE FROM p2p_records WHERE invoice_id = ?", (invoice_id,))
        return cur.rowcount


def mark_p2p_reminder_sent(p2p_id: int) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE p2p_records SET reminder_sent = 1 WHERE id = ?",
            (p2p_id,),
        )


# ── Settings (shared across processes — Slack overrides, report markers) ─────

# Short-TTL read cache. Settings are read on hot paths (the escalation
# threshold is consulted for every classification) but change rarely. The TTL
# is small enough that a /ar threshold change from another process is picked
# up promptly; set_setting invalidates locally for immediate effect.
_SETTINGS_TTL_SECONDS = 30.0
_settings_cache: dict[str, tuple[float, str | None]] = {}
_settings_cache_lock = threading.Lock()


def get_setting(key: str, default: str | None = None) -> str | None:
    now = time.monotonic()
    with _settings_cache_lock:
        entry = _settings_cache.get(key)
        if entry and now - entry[0] < _SETTINGS_TTL_SECONDS:
            cached = entry[1]
            return cached if cached is not None else default

    with _conn() as con:
        row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    value = row["value"] if row else None

    with _settings_cache_lock:
        _settings_cache[key] = (now, value)
    return value if value is not None else default


def set_setting(key: str, value: str, updated_by: str = "system") -> None:
    with _conn() as con:
        con.execute(
            """INSERT INTO settings (key, value, updated_at, updated_by)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   updated_at = excluded.updated_at,
                   updated_by = excluded.updated_by""",
            (key, value, _now(), updated_by),
        )
    # Refresh locally so the writing process sees the change immediately
    with _settings_cache_lock:
        _settings_cache[key] = (time.monotonic(), value)


# ── Summary queries (used by reports) ─────────────────────────────────────────

def has_recent_payment_signal(invoice_id: str, days: int = 7) -> bool:
    """True if a category-1 payment confirmation reply was received within the last N days."""
    with _conn() as con:
        row = con.execute(
            """SELECT COUNT(*) as cnt FROM inbound_replies
               WHERE invoice_id = ? AND payment_signal = 1
               AND julianday(received_at) > julianday('now', ?)""",
            (invoice_id, f"-{days} days"),
        ).fetchone()
    return (row["cnt"] or 0) > 0


def get_ar_totals_by_currency() -> list[dict]:
    """
    Outstanding totals grouped by currency.

    amount_cents cannot be summed across currencies — 100 USD and 100 EUR are
    not 200 of anything. Reports use this to label or break out totals rather
    than presenting one meaningless number.
    """
    with _conn() as con:
        rows = con.execute(
            """SELECT currency,
                      COUNT(*)          AS cnt,
                      SUM(amount_cents) AS total_cents
               FROM invoices
               WHERE status NOT IN ('paid','written_off','draft')
               GROUP BY currency
               ORDER BY total_cents DESC""",
        ).fetchall()
    return [
        {"currency": r["currency"] or "USD", "count": r["cnt"], "cents": r["total_cents"] or 0}
        for r in rows
    ]


def get_ar_summary() -> dict:
    """High-level AR stats for reports and dashboard."""
    with _conn() as con:
        total = con.execute(
            "SELECT COUNT(*) as cnt, SUM(amount_cents) as total FROM invoices WHERE status NOT IN ('paid','written_off')"
        ).fetchone()
        paid_today = con.execute(
            "SELECT COUNT(*) as cnt FROM invoices WHERE actual_payment_date LIKE ?",
            (datetime.now(timezone.utc).strftime("%Y-%m-%d") + "%",),
        ).fetchone()
        at_risk = con.execute(
            "SELECT COUNT(*) as cnt, SUM(amount_cents) as total FROM invoices WHERE days_overdue >= 45 AND status NOT IN ('paid','written_off')"
        ).fetchone()
        shadow_pending = con.execute(
            "SELECT COUNT(*) as cnt FROM outreach_log WHERE status = 'queued_shadow'"
        ).fetchone()

    return {
        "open_invoice_count": total["cnt"] or 0,
        "total_ar_cents": total["total"] or 0,
        "paid_today_count": paid_today["cnt"] or 0,
        "at_risk_count": at_risk["cnt"] or 0,
        "at_risk_cents": at_risk["total"] or 0,
        "shadow_pending_count": shadow_pending["cnt"] or 0,
    }
