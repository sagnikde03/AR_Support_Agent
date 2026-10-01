"""
AR Knowledge Pipeline — Indexer

Incrementally embeds and upserts AR ticket Q&A docs into ChromaDB.
Tracks a manifest so only new/changed docs are re-embedded on each run.
"""

import hashlib
import json
from pathlib import Path

import chromadb
from sentence_transformers import SentenceTransformer

DB_PATH        = Path(__file__).parent.parent / "data" / "chroma_db"
MANIFEST_PATH  = Path(__file__).parent.parent / "data" / "manifest.json"
COLLECTION_NAME = "caplinked_ar_kb"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {}


def _save_manifest(manifest: dict):
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def emails_to_docs(emails: list[dict]) -> list[dict]:
    """Convert processed email dicts into indexable documents."""
    docs = []
    for i, e in enumerate(emails):
        full_text = e["full_text"]
        unique_key = f"{i}_{e['subject']}_{e['date']}_{e.get('sender', '')}"
        docs.append({
            "id":           f"email_{_sha(unique_key)}",
            "source_id":    f"email_{_sha(unique_key)}",
            "content_hash": _sha(full_text),
            "text":         full_text,
            "metadata": {
                "source":  "ar_email",
                "title":   e["subject"],
                "url":     "",
                "file":    e.get("source_file", ""),
                "date":    e.get("date", ""),
            },
        })
    return docs


def tickets_to_docs(tickets: list[dict]) -> list[dict]:
    """Convert processed AR ticket dicts into indexable documents."""
    docs = []
    for t in tickets:
        full_text = t["full_text"]
        docs.append({
            "id":           f"ticket_{t['id']}_chunk_0",
            "source_id":    f"ticket_{t['id']}",
            "content_hash": _sha(full_text),
            "text":         full_text,
            "metadata": {
                "source":    "ar_ticket",
                "title":     t["subject"],
                "url":       t.get("url", ""),
                "ticket_id": str(t["id"]),
                "tags":      ", ".join(t.get("tags") or []),
                "status":    t.get("status", ""),
            },
        })
    return docs


class Indexer:
    def __init__(self):
        print(f"Loading embedding model '{EMBEDDING_MODEL}'...")
        self.model = SentenceTransformer(EMBEDDING_MODEL)

        DB_PATH.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(DB_PATH))
        self.collection = client.get_or_create_collection(
            COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        self.manifest = _load_manifest()
        print(f"  Collection '{COLLECTION_NAME}': {self.collection.count()} chunks existing")

    def upsert(self, docs: list[dict], label: str):
        to_add = []
        skipped = 0

        for doc in docs:
            if self.manifest.get(doc["source_id"]) == doc["content_hash"]:
                skipped += 1
                continue
            to_add.append(doc)
            self.manifest[doc["source_id"]] = doc["content_hash"]

        print(f"  {label}: {skipped} unchanged, {len(to_add)} to index")
        if not to_add:
            return

        texts = [d["text"] for d in to_add]
        print(f"  Embedding {len(texts)} docs...")
        embeddings = self.model.encode(texts, show_progress_bar=True, batch_size=64).tolist()

        for i in range(0, len(to_add), 200):
            batch = to_add[i:i + 200]
            self.collection.upsert(
                ids=[d["id"] for d in batch],
                documents=[d["text"] for d in batch],
                embeddings=embeddings[i:i + len(batch)],
                metadatas=[d["metadata"] for d in batch],
            )

        _save_manifest(self.manifest)
        print(f"  Done. {len(to_add)} docs indexed.")

    def stats(self):
        print(f"\nIndex total: {self.collection.count()} docs | Manifest: {len(self.manifest)} sources")

    @staticmethod
    def reset():
        client = chromadb.PersistentClient(path=str(DB_PATH))
        try:
            client.delete_collection(COLLECTION_NAME)
            print("Collection dropped.")
        except Exception:
            pass
        if MANIFEST_PATH.exists():
            MANIFEST_PATH.unlink()
            print("Manifest cleared.")
