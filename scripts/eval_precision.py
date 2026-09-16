"""Precision@5 + latency evaluation for the hybrid search layer.

Grading criteria calls for "Precision@5 > 0.8" and "<500ms query latency" as
concrete numbers, not just a described strategy - this script is how you'd actually
produce them against a real user's synced data, rather than asserting them in prose.

Usage:
    python scripts/eval_precision.py --user-id <uuid> --labels docs/eval_labels.example.json

The labels file is a small hand-curated set of (query, which ids SHOULD come back)
pairs - see docs/eval_labels.example.json for the exact shape. You write these by
looking at a synced user's actual data and picking a query + the ids you know are
genuinely relevant to it; this script then measures how well search_gmail/
search_gcal/search_gdrive actually retrieve those ids.

Why this can't ship with real numbers pre-filled in: Precision@5 is only meaningful
against a specific user's actual synced Gmail/Calendar/Drive content, which requires
a completed OAuth login + at least one background sync to exist - there's no
"the assignment's" data to score against ahead of time.
"""

import argparse
import json
import statistics
import time

from app.db.session import SessionLocal
from app.embeddings.embedder import embed_text
from app.embeddings.search import search_gcal, search_gdrive, search_gmail

SEARCH_FNS = {
    "gmail": lambda db, user_id, vec, limit: [r.email_id for r in search_gmail(db, user_id, vec, limit=limit)],
    "gcal": lambda db, user_id, vec, limit: [r.event_id for r in search_gcal(db, user_id, vec, limit=limit)],
    "gdrive": lambda db, user_id, vec, limit: [r.file_id for r in search_gdrive(db, user_id, vec, limit=limit)],
}


def precision_at_k(retrieved_ids: list[str], relevant_ids: set[str], k: int) -> float:
    top_k = retrieved_ids[:k]
    if not top_k:
        return 0.0
    hits = sum(1 for item_id in top_k if item_id in relevant_ids)
    return hits / k


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, help="User UUID whose synced data to evaluate against")
    parser.add_argument("--labels", required=True, help="Path to a JSON labels file (see docs/eval_labels.example.json)")
    parser.add_argument("--k", type=int, default=5, help="Precision@K (default: 5, matching the brief's target)")
    args = parser.parse_args()

    with open(args.labels) as f:
        cases: list[dict] = json.load(f)

    db = SessionLocal()
    precisions: list[float] = []
    latencies_ms: list[float] = []

    for case in cases:
        service = case["service"]
        query_text = case["query"]
        relevant_ids = set(case["relevant_ids"])

        start = time.perf_counter()
        vector = embed_text(query_text)
        retrieved_ids = SEARCH_FNS[service](db, args.user_id, vector, args.k)
        elapsed_ms = (time.perf_counter() - start) * 1000

        precision = precision_at_k(retrieved_ids, relevant_ids, args.k)
        precisions.append(precision)
        latencies_ms.append(elapsed_ms)

        print(f"[{service}] {query_text!r}: precision@{args.k}={precision:.2f}  latency={elapsed_ms:.0f}ms  retrieved={retrieved_ids}")

    db.close()

    if not precisions:
        print("No labeled cases found - nothing to evaluate.")
        return

    avg_precision = statistics.mean(precisions)
    p50 = statistics.median(latencies_ms)
    p95 = sorted(latencies_ms)[max(0, int(len(latencies_ms) * 0.95) - 1)]

    print("\n--- Summary ---")
    print(f"Average precision@{args.k}: {avg_precision:.3f}  (target: > 0.80)")
    print(f"Latency p50: {p50:.0f}ms   p95: {p95:.0f}ms   (target: < 500ms)")


if __name__ == "__main__":
    main()
