"""
Single owner of the Qdrant connection.

Why this module exists
----------------------
Qdrant runs here in *embedded* mode (`QdrantClient(path=...)`) -- a library
reading a directory on local disk, not a server. Embedded mode takes an
exclusive file lock on that directory, so only one client may be open at a time
within the process, and only one process may use it at all.

Two PRs arriving together both execute in this same FastAPI process, because
they are BackgroundTasks dispatched to Starlette's threadpool. Without
coordination the second one dies with "storage folder is already accessed by
another instance". `open_qdrant()` serialises access behind a lock and
guarantees the handle is closed, so concurrent PRs queue instead of crashing.

The real fix for a multi-process deployment is a Qdrant *server*
(`QdrantClient(url=...)`), which is a change to this file and nowhere else.
That is the point of funnelling every caller through here.
"""

import os
import re
import threading
from contextlib import contextmanager

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

COLLECTION_PREFIX = "codebase_"
QDRANT_PATH = os.getenv("QDRANT_PATH", "./qdrant_data")

# Must match embeddings.EMBED_DIM. Qdrant rejects a vector whose length differs
# from the collection's configured size, so a mismatch fails loudly at upsert.
VECTOR_SIZE = 768

# Cosine, because we care about direction (what the code is about) and not
# magnitude (how long it is). Euclidean would rank a long function as distant
# from a short one that does the same thing.
DISTANCE = Distance.COSINE

# Non-reentrant on purpose: two live clients on one directory is exactly the
# failure this prevents, so an accidental nested open should fail rather than
# be quietly allowed through.
_LOCK = threading.Lock()


class MemoryBusy(RuntimeError):
    """Raised when the store is locked by another task and we chose not to wait."""


@contextmanager
def open_qdrant(lock_timeout: float = -1):
    """Yield an embedded Qdrant client, serialised process-wide, always closed.

    lock_timeout is passed to Lock.acquire: -1 blocks indefinitely (the default,
    correct for the review path), a positive value raises MemoryBusy instead of
    waiting (used by /health so a probe cannot hang behind a long ingest).
    """
    if not _LOCK.acquire(timeout=lock_timeout):
        raise MemoryBusy("vector store is busy")
    try:
        client = QdrantClient(path=QDRANT_PATH)
        try:
            yield client
        finally:
            client.close()
    finally:
        _LOCK.release()


def collection_for(repo_full_name: str) -> str:
    """One collection per repository, so one repo's PRs never retrieve another's code.

    With a single shared collection, the second repo to send a webhook found it
    already warm and was reviewed against the first repo's code.
    """
    slug = re.sub(r"[^a-z0-9_-]", "_", repo_full_name.lower().replace("/", "__"))
    return COLLECTION_PREFIX + slug


def vectors_config() -> VectorParams:
    return VectorParams(size=VECTOR_SIZE, distance=DISTANCE)


def count_vectors(collection: str | None = None, lock_timeout: float = -1) -> int:
    """Number of code blocks in memory. 0 means that repo has not been indexed yet.

    A missing collection and an empty collection are both 0 here on purpose:
    the caller only wants to know whether it has usable memory, and on Render
    the ephemeral-disk case shows up as the collection being absent entirely.
    With no collection given, returns the total across every repository.
    """
    with open_qdrant(lock_timeout=lock_timeout) as client:
        if collection is None:
            return sum(
                client.count(collection_name=c.name, exact=True).count
                for c in client.get_collections().collections
            )
        if not client.collection_exists(collection_name=collection):
            return 0
        return client.count(collection_name=collection, exact=True).count
