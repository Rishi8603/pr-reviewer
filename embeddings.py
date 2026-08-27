"""
Shared embedding client for both pipelines.

The offline ingest pipeline and the online retrieval pipeline must use the same
model at the same output dimensionality, or their vectors land in different
spaces and cosine similarity between them is meaningless. Keeping the single
call site here is what enforces that invariant.
"""

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

load_dotenv()

EMBED_MODEL = "gemini-embedding-001"

# The model returns 3072 dimensions by default. We request a 768-dim Matryoshka
# truncation: 4x less storage and RAM for a small recall cost. Cosine is
# scale-invariant so the truncated vectors need no renormalising here -- they
# would if we ranked by dot product.
EMBED_DIM = 768

# The model caps a single input at 2048 tokens. Code is denser than prose
# (roughly 3-4 chars per token), so this char budget stays clear of the ceiling
# without putting a tokeniser on the hot path.
MAX_INPUT_CHARS = 6000

# Trades round-trips against request size. 32 blocks of <=6000 chars sits
# comfortably inside the per-request payload limit.
BATCH_SIZE = 32

_client = genai.Client()


def truncate(text: str) -> str:
    """Clamp one input to the model's window, marking it so the LLM can tell."""
    if len(text) <= MAX_INPUT_CHARS:
        return text
    return text[:MAX_INPUT_CHARS] + "\n# ... truncated for embedding ..."


def _is_transient(exc: BaseException) -> bool:
    """Retry rate limits and server faults; never retry a bad request or key.

    Retrying a 400 or a 401 just burns the same failure three times and delays
    the real error by the length of the backoff.
    """
    if isinstance(exc, genai_errors.ClientError):
        return getattr(exc, "code", None) == 429
    if isinstance(exc, genai_errors.ServerError):
        return True
    return isinstance(exc, (ConnectionError, TimeoutError))


@retry(
    retry=retry_if_exception(_is_transient),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _embed_batch(batch: list[str]) -> list[list[float]]:
    response = _client.models.embed_content(
        model=EMBED_MODEL,
        contents=batch,
        config=types.EmbedContentConfig(output_dimensionality=EMBED_DIM),
    )
    return [embedding.values for embedding in response.embeddings]


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed many strings in as few round-trips as possible, order preserved.

    The first version of this project made one API call per code block, so
    indexing a repo with 400 functions meant 400 sequential HTTPS round-trips.
    That was survivable in an offline script and is not survivable now that a
    cold boot runs inside the PR review path.
    """
    if not texts:
        return []
    vectors: list[list[float]] = []
    for start in range(0, len(texts), BATCH_SIZE):
        batch = [truncate(text) for text in texts[start:start + BATCH_SIZE]]
        vectors.extend(_embed_batch(batch))
    return vectors


def embed_one(text: str) -> list[float]:
    return embed_texts([text])[0]
