"""
AR Knowledge Pipeline — Ticket Processor

Reads raw Zendesk NDJSON exports and extracts only AR/billing-related Q&A.
This is the inverse of the Support-Agent: those filtered-out billing tickets
are exactly what we want here.

Filters (ticket must pass all three):
  Filter 1: Has at least one AR/billing tag
  Filter 2: At least 2 public comments  (auto-excludes all phone/call records)
  Filter 3: Content quality check       (min question + resolution length)
"""

import json
import re
from pathlib import Path

RAW_DATA_DIR = Path(__file__).parent.parent / "raw_data"

# ── Tags that identify AR/billing tickets — KEEP these ────────────────────────
# Substring match — same prefixes that Support-Agent skips
KEEP_TAG_PREFIXES = [
    "billing",
    "invoice",
    "payment",
    "receipt",
    "remittance",
    "accounts_receivable",
    "cancel_subscription",
    "reactive_subscription",
    "upgrade_request",
    "price_increase",
    "trial_extension",          # trial-to-paid transitions, suspended workspaces pre-payment
    "operations/account_management_",  # payment receipts, ACH notifications, contract emails
]

# Short tags — exact match only to avoid false positives
KEEP_TAG_EXACT = {"ar", "sales"}


def _has_ar_tag(tags: list[str]) -> bool:
    for tag in tags:
        tag = tag.lower().strip()
        if tag in KEEP_TAG_EXACT:
            return True
        for prefix in KEEP_TAG_PREFIXES:
            if tag.startswith(prefix):
                return True
    return False


def _extract_doc(ticket: dict) -> dict | None:
    """Extract Q&A from a ticket. Returns None if quality check fails."""
    comments = [c for c in ticket.get("comments", []) if c.get("public", False)]

    # Filter 2: needs at least 2 public comments (excludes phone logs)
    if len(comments) < 2:
        return None

    # Strip HTML tags
    question   = re.sub(r"<[^>]+>", "", comments[0].get("body", "")).strip()
    resolution = re.sub(r"<[^>]+>", "", comments[-1].get("body", "")).strip()

    # Filter 3: content quality
    if len(question) < 30 or len(resolution) < 80:
        return None

    return {
        "id":       ticket["id"],
        "subject":  ticket.get("subject", ""),
        "tags":     ticket.get("tags", []),
        "status":   ticket.get("status", ""),
        "full_text": f"Q: {question}\n\nA: {resolution}",
        "url":      f"https://caplinked.zendesk.com/agent/tickets/{ticket['id']}",
    }


def load_ticket_files() -> list[dict]:
    """Load all NDJSON export files from raw_data/."""
    tickets = []
    for path in sorted(RAW_DATA_DIR.glob("*.json")):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        tickets.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    print(f"  Loaded {len(tickets)} raw tickets from {RAW_DATA_DIR.name}/")
    return tickets


def process(tickets: list[dict]) -> list[dict]:
    """Filter raw tickets down to AR/billing Q&A documents."""
    docs = []
    skipped_no_tag = 0
    skipped_quality = 0

    for ticket in tickets:
        if not _has_ar_tag(ticket.get("tags", [])):
            skipped_no_tag += 1
            continue

        doc = _extract_doc(ticket)
        if doc is None:
            skipped_quality += 1
            continue

        docs.append(doc)

    print(
        f"  Kept {len(docs)} AR tickets | "
        f"Skipped {skipped_no_tag} (no AR tag) + {skipped_quality} (quality/phone)"
    )
    return docs
