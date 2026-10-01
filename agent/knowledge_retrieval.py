"""
AR Knowledge Retrieval
Queries ChromaDB for the most relevant AR/billing ticket resolutions.
"""

from pathlib import Path

import chromadb
from sentence_transformers import SentenceTransformer

DB_PATH         = Path(__file__).parent.parent / "data" / "chroma_db"
COLLECTION_NAME = "caplinked_ar_kb"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

_model: SentenceTransformer | None = None
_collection = None


def _get_collection():
    global _model, _collection
    if _model is None:
        _model = SentenceTransformer(EMBEDDING_MODEL)
    if _collection is None:
        if not DB_PATH.exists():
            raise RuntimeError(
                "AR index not found. Run: python knowledge_pipeline/run_sync.py"
            )
        client = chromadb.PersistentClient(path=str(DB_PATH))
        _collection = client.get_collection(COLLECTION_NAME)
    return _model, _collection


def retrieve(query: str, n_results: int = 6) -> dict:
    model, collection = _get_collection()
    embedding = model.encode(query).tolist()

    results = collection.query(
        query_embeddings=[embedding],
        n_results=min(n_results, collection.count()),
    )

    tickets = []
    seen: dict[str, dict] = {}

    for i in range(len(results["documents"][0])):
        meta     = results["metadatas"][0][i]
        distance = results["distances"][0][i]
        key      = meta.get("ticket_id") or str(i)

        if key in seen and distance >= seen[key]["distance"]:
            continue

        hit = {
            "text":     results["documents"][0][i],
            "title":    meta.get("title", ""),
            "url":      meta.get("url", ""),
            "source":   meta.get("source", "ar_ticket"),
            "distance": distance,
        }
        seen[key] = hit
        tickets.append(hit)

    tickets.sort(key=lambda x: x["distance"])
    top = tickets[:5]

    context_text = "\n\n---\n\n".join(
        f"[Past Resolution {i+1}] {h['title']}\n\n{h['text']}"
        for i, h in enumerate(top)
    )

    return {
        "tickets": top,
        "context_text": context_text,
    }
