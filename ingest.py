"""
The offline pipeline: source code in, vectors out.

Two entry points:

  ingest_repository(path)          index a directory that is already on disk
  ensure_memory(clone_url, token)  index the target repo if memory is empty

`ensure_memory` is the self-healing step. Render's filesystem is ephemeral, so
every deploy and every wake from spin-down comes up with ./qdrant_data gone.
Before it existed the service booted with no collection and the first PR raised
"Collection codebase_memory not found" inside a BackgroundTask -- GitHub had
already been handed its 200, so the failure was completely invisible.
"""

import ast
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

from qdrant_client.models import PointStruct

from embeddings import embed_texts
from qdrant_store import COLLECTION_NAME, count_vectors, open_qdrant, vectors_config

# Directories that contain code we did not write. Indexing site-packages would
# bury the project's own functions under thousands of dependency vectors.
SKIP_DIRS = {
    "venv", ".venv", "env", ".git", "__pycache__", "node_modules",
    "site-packages", ".tox", "build", "dist", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".eggs",
}

# A cold boot runs on the PR review path, so indexing cost is latency the
# developer waits through. This caps the blast radius on a very large repo.
MAX_BLOCKS = 2000

# Above this, a class is split into per-method chunks instead of being stored
# whole: one vector cannot meaningfully represent 2000 lines, and a segment that
# large would be truncated by the embedder anyway.
MAX_CLASS_CHARS = 4000

CLONE_TIMEOUT_SECONDS = 300

# We clone a URL taken from the request body. Even with signature verification
# in front, the host is worth pinning -- it turns "clone anything" into "clone
# from GitHub", which is the only thing this service should ever do.
ALLOWED_CLONE_HOSTS = {"github.com", "www.github.com"}


# =====================================================================
# PHASE 1: THE AST PARSER
# Why AST? A character-count text splitter chops code mid-function, so the
# retrieved context arrives with its logic cut in half. `ast` reads the file the
# way the interpreter does, which lets us cut only on real boundaries.
# =====================================================================
def extract_code_blocks(filepath: str, source_root: str | None = None) -> list[dict]:
    """Extract complete functions and classes from one Python file."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as handle:
            source = handle.read()
    except OSError:
        return []

    try:
        tree = ast.parse(source)
    except SyntaxError:
        # A file we cannot parse is skipped rather than fatal: one bad file in a
        # cloned repo should not abort the whole index.
        return []

    if source_root:
        display_path = os.path.relpath(filepath, source_root).replace(os.sep, "/")
    else:
        display_path = filepath.replace(os.sep, "/")

    blocks: list[dict] = []

    def emit(node, qualname: str) -> bool:
        segment = ast.get_source_segment(source, node)
        if not segment:
            return False
        blocks.append({
            "name": qualname,
            "type": type(node).__name__,
            "code": segment,
            "filepath": display_path,
            "lineno": node.lineno,
        })
        return True

    # Walk only the top level of the module.
    #
    # The previous version used ast.walk(), which visits every descendant. That
    # emitted each method twice -- once inside its class's source segment and
    # once on its own -- which doubled the embedding bill and let a single large
    # class crowd every other file out of the top-k.
    #
    # It also matched only ast.FunctionDef, so every `async def` in the codebase
    # was silently skipped. This service's own webhook handler is an async def,
    # meaning the entrypoint was never in its own memory.
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            emit(node, node.name)
        elif isinstance(node, ast.ClassDef):
            segment = ast.get_source_segment(source, node) or ""
            if len(segment) <= MAX_CLASS_CHARS:
                # Small enough to keep whole, so a method keeps its class as context.
                emit(node, node.name)
                continue

            before = len(blocks)
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    emit(child, f"{node.name}.{child.name}")
            if len(blocks) == before:
                # A big class with no methods (e.g. a wall of constants): there is
                # nothing to split on, so take it as-is and let the embedder clamp.
                emit(node, node.name)

    return blocks


def iter_python_files(repo_path: str):
    """Yield every .py file under repo_path, skipping vendored directories."""
    for root, dirs, files in os.walk(repo_path):
        # Prune in place so os.walk never descends into these at all. The old
        # `"venv" not in root` substring test both missed .venv and would have
        # excluded any repo that happened to live under a path containing "venv".
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(root, name)


# =====================================================================
# PHASE 2: THE INGESTION ENGINE
# =====================================================================
def ingest_repository(repo_path: str, recreate: bool = True) -> int:
    """Chunk every Python file under repo_path into Qdrant. Returns block count."""
    print(f"Scanning repository: {repo_path}")

    blocks: list[dict] = []
    for filepath in iter_python_files(repo_path):
        blocks.extend(extract_code_blocks(filepath, source_root=repo_path))
        if len(blocks) >= MAX_BLOCKS:
            print(f"Hit MAX_BLOCKS={MAX_BLOCKS}; indexing the first {MAX_BLOCKS} blocks only.")
            blocks = blocks[:MAX_BLOCKS]
            break

    if not blocks:
        print("No Python functions or classes found - nothing to ingest.")
        return 0

    print(f"Embedding {len(blocks)} code blocks...")
    vectors = embed_texts([block["code"] for block in blocks])

    points = [
        PointStruct(id=index, vector=vector, payload=block)
        for index, (block, vector) in enumerate(zip(blocks, vectors), start=1)
    ]

    with open_qdrant() as qdrant:
        if recreate and qdrant.collection_exists(collection_name=COLLECTION_NAME):
            print(f"Replacing existing collection: {COLLECTION_NAME}")
            qdrant.delete_collection(collection_name=COLLECTION_NAME)
        if not qdrant.collection_exists(collection_name=COLLECTION_NAME):
            qdrant.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=vectors_config(),
            )
        qdrant.upsert(collection_name=COLLECTION_NAME, points=points)

    print(f"Successfully ingested {len(points)} code blocks.")
    return len(points)


# =====================================================================
# PHASE 3: SELF-HEALING BOOT
# =====================================================================
def _redact(text: str) -> str:
    """Strip any userinfo from a URL so a token never reaches a log line."""
    return re.sub(r"//[^@/\s]+@", "//***@", text)


def _validate_clone_url(clone_url: str) -> None:
    parsed = urlparse(clone_url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_CLONE_HOSTS:
        raise ValueError(f"refusing to clone untrusted URL: {_redact(clone_url)}")


def _on_rm_error(func, path, _exc):
    """git leaves .git objects read-only, which makes rmtree fail on Windows."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree(path: str) -> None:
    # onerror is deprecated from 3.12 in favour of onexc; both take three args,
    # so one callback serves either.
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_on_rm_error)
    else:
        shutil.rmtree(path, onerror=_on_rm_error)


def clone_repository(clone_url: str, dest: str, token: str | None = None) -> str:
    """Shallow-clone the default branch of clone_url into dest."""
    _validate_clone_url(clone_url)

    url = clone_url
    if token:
        # Private repos need credentials on the URL. This string must never be
        # printed or included in an exception message.
        url = url.replace("https://", f"https://x-access-token:{token}@", 1)

    print(f"Cloning {_redact(clone_url)} (depth 1)...")
    result = subprocess.run(
        # A list, never shell=True: the URL comes from a request body and must
        # not be able to become a shell command.
        ["git", "clone", "--depth", "1", "--single-branch", url, dest],
        capture_output=True,
        text=True,
        timeout=CLONE_TIMEOUT_SECONDS,
        # Without this, a private repo with a bad token makes git block forever
        # on a credential prompt that nobody is there to answer.
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git clone failed (exit {result.returncode}): {_redact(result.stderr.strip())}"
        )
    return dest


def ensure_memory(clone_url: str, token: str | None = None) -> int:
    """Rebuild vector memory from source if it is empty. Returns block count.

    Idempotent and cheap on a warm process: it is one count() when memory is
    already populated, so it is safe to call at the top of every review.
    """
    existing = count_vectors()
    if existing > 0:
        print(f"Memory warm: {existing} blocks already indexed.")
        return existing

    print("Cold boot detected (0 vectors). Rebuilding memory from source...")

    temp_root = tempfile.mkdtemp(prefix="pr-reviewer-")
    repo_dir = os.path.join(temp_root, "repo")
    try:
        clone_repository(clone_url, repo_dir, token=token)
        return ingest_repository(repo_dir)
    finally:
        # The free tier's disk is small and this process is long-lived, so a
        # clone left behind on a failed review would accumulate until the disk
        # filled. Cleanup must not mask the original exception.
        try:
            _rmtree(temp_root)
        except OSError as exc:
            print(f"Could not remove {temp_root}: {exc}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    ingest_repository(target)
