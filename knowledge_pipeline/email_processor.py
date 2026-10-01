"""
AR Knowledge Pipeline — Email Processor

Parses .mbox files from raw_data/ and extracts useful AR email content.

Two sources:
  Accounts Receivable.mbox     — real AR email threads (small, high quality)
  Billing Zendesk Ticket.mbox  — invoices@ mailbox (large, needs filtering)

Filters out:
  - Satisfaction survey emails
  - Out-of-office / auto-replies
  - Zendesk system notifications with no real content
  - Emails too short to be useful
"""

import mailbox
import re
from email.header import decode_header
from pathlib import Path

RAW_DATA_DIR = Path(__file__).parent.parent / "raw_data"

# ── Noise filters ─────────────────────────────────────────────────────────────

SKIP_SUBJECT_KEYWORDS = [
    "how would you rate",
    "satisfaction survey",
    "automatic reply",
    "auto-reply",
    "out of office",
    "delivery failure",
    "undeliverable",
    "mail delivery",
]

SKIP_BODY_KEYWORDS = [
    "please type your reply above this line",  # pure Zendesk system notification
    "i am out of the office",
    "i'm out of the office",
    "i will be out of office",
    "thank you for contacting places for people",
    # Standard past-due reminder template — 2,700+ near-identical copies in Past Due-Collections.mbox
    "i am contacting you because you have an outstanding invoice with caplinked",
]

MIN_BODY_LENGTH = 80


# ── Helpers ───────────────────────────────────────────────────────────────────

def _decode_header(value: str) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="ignore"))
        else:
            decoded.append(part)
    return " ".join(decoded).strip()


def _extract_body(msg) -> str:
    """Extract plain text body from an email message."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(errors="ignore")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(errors="ignore")

    # Strip quoted reply chains (lines starting with >)
    lines = [l for l in body.splitlines() if not l.startswith(">")]
    body = "\n".join(lines).strip()

    # Remove Zendesk marker and everything below it
    marker = "##- Please type your reply above this line -##"
    if marker in body:
        body = body.split(marker)[0].strip()

    # Clean up extra whitespace
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body


def _is_noise(subject: str, body: str) -> bool:
    s = subject.lower()
    b = body.lower()

    for kw in SKIP_SUBJECT_KEYWORDS:
        if kw in s:
            return True

    for kw in SKIP_BODY_KEYWORDS:
        if kw in b:
            return True

    # Pure Zendesk "ticket received" notifications with no actual customer content
    if "a ticket (#" in b and "has been received" in b and len(body) < 300:
        return True

    if len(body) < MIN_BODY_LENGTH:
        return True

    return False


# ── Main processor ─────────────────────────────────────────────────────────────

def load_mbox_files() -> list[dict]:
    """Load and parse all .mbox files from raw_data/."""
    docs = []

    for mbox_path in sorted(RAW_DATA_DIR.glob("*.mbox")):
        print(f"  Reading {mbox_path.name}...")
        mbox = mailbox.mbox(str(mbox_path))
        total = 0
        kept  = 0

        for msg in mbox:
            total += 1
            subject = _decode_header(msg.get("Subject", ""))
            sender  = _decode_header(msg.get("From", ""))
            date    = msg.get("Date", "")
            body    = _extract_body(msg)

            if _is_noise(subject, body):
                continue

            docs.append({
                "source_file": mbox_path.name,
                "subject":     subject,
                "sender":      sender,
                "date":        date,
                "body":        body,
                "full_text":   f"Subject: {subject}\n\n{body}",
            })
            kept += 1

        print(f"    {mbox_path.name}: {kept} kept / {total} total")

    return docs
