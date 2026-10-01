"""
AR Knowledge Pipeline — Sync Entry Point

Processes raw Zendesk NDJSON exports and builds the AR ChromaDB index.
No web scraping — tickets only.

Usage:
    python knowledge_pipeline/run_sync.py          # incremental sync
    python knowledge_pipeline/run_sync.py --reset  # wipe + full rebuild
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from knowledge_pipeline.email_processor import load_mbox_files
from knowledge_pipeline.indexer import Indexer, emails_to_docs, tickets_to_docs
from knowledge_pipeline.ticket_processor import load_ticket_files, process


def run_sync():
    start = time.time()
    print("=" * 50)
    print("  CapLinked AR Knowledge Pipeline Sync")
    print("=" * 50)

    indexer = Indexer()

    # ── Zendesk ticket exports ─────────────────────────────────────────────────
    print("\n[1/2] Zendesk ticket exports (.json)")
    ticket_files = load_ticket_files()
    if not ticket_files:
        print("\n  No ticket export files found in raw_data/ — skipping tickets.")
    else:
        tickets = process(ticket_files)
        if tickets:
            docs = tickets_to_docs(tickets)
            indexer.upsert(docs, "AR tickets")

    # ── Email exports (.mbox) ─────────────────────────────────────────────────
    print("\n[2/2] Email exports (.mbox)")
    emails = load_mbox_files()
    if emails:
        docs = emails_to_docs(emails)
        indexer.upsert(docs, "AR emails")

    indexer.stats()
    print(f"\nSync complete in {time.time() - start:.1f}s")
    print("=" * 50)


def main():
    parser = argparse.ArgumentParser(description="AR Knowledge Pipeline Sync")
    parser.add_argument("--reset", action="store_true", help="Wipe index and rebuild from scratch")
    args = parser.parse_args()

    if args.reset:
        print("Resetting index...")
        Indexer.reset()
        print("Reset complete. Running full sync...\n")

    run_sync()


if __name__ == "__main__":
    main()
