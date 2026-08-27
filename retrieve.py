"""
The online pipeline: a PR diff in, relevant existing code out.

Retrieval is per changed file rather than per PR. The previous version embedded
the entire diff as one vector, which had two problems:

  1. gemini-embedding-001 caps input at 2048 tokens, so any real multi-file PR
     was silently clamped to roughly its first file.
  2. Averaging an entire diff into a single point is weak retrieval even when it
     fits -- a PR touching auth and billing lands the query vector somewhere
     between the two, near neither.

One query per changed file gives each file its own neighbours, and the results
are merged by best score across all of them.
"""

import re

from embeddings import embed_texts
from qdrant_store import COLLECTION_NAME, open_qdrant

# Per-file query cap. A 60-file refactor would otherwise cost 60 embeddings and
# return more context than fits in a prompt.
MAX_DIFF_CHUNKS = 12

# Neighbours per changed file.
TOP_K_PER_CHUNK = 3

# Distinct blocks handed to the agents. Every extra block is paid for five times
# over, once per reviewer prompt.
MAX_CONTEXT_BLOCKS = 8


def split_diff_by_file(diff_text: str) -> list[str]:
    """Split a unified diff into one string per changed file."""
    if not diff_text or not diff_text.strip():
        return []
    # Split before each "diff --git" header without consuming it.
    parts = re.split(r"(?m)^(?=diff --git )", diff_text)
    chunks = [part.strip() for part in parts if part.strip()]
    # A diff with no git headers (a hand-pasted hunk, or a test fixture) still
    # deserves one query rather than none.
    return chunks or [diff_text.strip()]


def search_codebase(query_text: str, top_k: int = TOP_K_PER_CHUNK) -> str:
    """Return the codebase blocks most similar to the changed files in a diff."""
    chunks = split_diff_by_file(query_text)
    if not chunks:
        return ""

    dropped = max(0, len(chunks) - MAX_DIFF_CHUNKS)
    chunks = chunks[:MAX_DIFF_CHUNKS]
    if dropped:
        print(f"Diff touches {dropped} more files than MAX_DIFF_CHUNKS; querying the first {len(chunks)}.")

    print(f"Searching codebase memory with {len(chunks)} per-file queries...")
    query_vectors = embed_texts(chunks)

    # Merge hits across chunks, keeping each block's best score. Two changed
    # files often share a neighbour, and the agents should see it once.
    best_by_id: dict[int, tuple[float, dict]] = {}
    with open_qdrant() as qdrant:
        if not qdrant.collection_exists(collection_name=COLLECTION_NAME):
            # ensure_memory() should have run first. Returning empty rather than
            # raising keeps a memory failure from taking the whole review down --
            # the agents can still review the diff on its own.
            print("No collection found; proceeding without codebase context.")
            return ""

        for vector in query_vectors:
            hits = qdrant.query_points(
                collection_name=COLLECTION_NAME,
                query=vector,
                limit=top_k,
            ).points
            for hit in hits:
                current = best_by_id.get(hit.id)
                if current is None or hit.score > current[0]:
                    best_by_id[hit.id] = (hit.score, hit.payload or {})

    ranked = sorted(best_by_id.values(), key=lambda item: item[0], reverse=True)
    ranked = ranked[:MAX_CONTEXT_BLOCKS]

    context_blocks = []
    for score, payload in ranked:
        header = (
            f"--- File: {payload.get('filepath')} "
            f"| {payload.get('type')}: {payload.get('name')} "
            f"| similarity: {score:.3f} ---"
        )
        context_blocks.append(f"{header}\n{payload.get('code', '')}\n")

    print(f"Retrieved {len(context_blocks)} unique code blocks.")
    return "\n".join(context_blocks)
