"""
Per-repository sliding-window rate limiter backed by PostgreSQL.

Prevents a single repository (or a misconfigured webhook) from flooding the
review pipeline and burning the Gemini API quota. The window is implemented as
a COUNT query with a time filter — no Redis required.

Algorithm: sliding window with a fixed-size time bucket.
  1. Count completed + pending reviews for this repo in the last WINDOW seconds.
  2. If count >= MAX_REVIEWS_PER_WINDOW, reject with 429 and a Retry-After hint.

This is a "sliding window counter" — simpler than a token bucket but correct
enough for this use case. A true token bucket would require persistent state
that survives restarts (Redis), which is deferred.

Interview talking points:
- Why per-repo, not per-IP? Because the webhook sender is GitHub's infrastructure
  (a handful of IPs), so IP-based limiting blocks the wrong thing.
- Why a DB query and not in-memory? Because in-memory state is lost on restart,
  and a repo that pushed 50 commits while the service was down would queue 50
  reviews on cold boot.
"""

from datetime import datetime, timedelta, timezone

from database import get_db
from models import PullRequest

# Default: 10 reviews per hour per repository. Generous enough for normal
# development, tight enough that a webhook loop cannot burn the budget.
MAX_REVIEWS_PER_WINDOW = int(__import__("os").getenv("RATE_LIMIT_MAX_REVIEWS", "10"))
WINDOW_SECONDS = int(__import__("os").getenv("RATE_LIMIT_WINDOW_SECONDS", "3600"))


class RateLimitExceeded(Exception):
    """Raised when a repository has exceeded its review budget."""

    def __init__(self, repo: str, current: int, limit: int, retry_after: int):
        self.repo = repo
        self.current = current
        self.limit = limit
        self.retry_after = retry_after
        super().__init__(
            f"{repo} has {current}/{limit} reviews in the current window. "
            f"Retry after {retry_after}s."
        )


def check_rate_limit(repo_full_name: str) -> None:
    """Raise RateLimitExceeded if the repository has exceeded its budget.

    Does nothing if the database is not configured — rate limiting is a
    database-backed feature and degrades to unlimited without one.
    """
    db = get_db()
    if db is None:
        return

    window_start = datetime.now(timezone.utc) - timedelta(seconds=WINDOW_SECONDS)

    try:
        with db:
            # Count ALL reviews in the window — pending, completed, and failed all
            # consume quota. The original only counted completed, so a burst of
            # queued reviews would slip through.
            from sqlalchemy import func

            query = (
                db.query(func.count(PullRequest.id), func.min(PullRequest.created_at))
                .join(PullRequest.repository)
                .filter(
                    PullRequest.repository.has(full_name=repo_full_name),
                    PullRequest.created_at >= window_start,
                )
            )
            count, oldest_created = query.one()

        if count >= MAX_REVIEWS_PER_WINDOW:
            # Estimate when the oldest review in the window will age out.
            retry_after = WINDOW_SECONDS  # Worst case: full window.
            if oldest_created is not None:
                age = (datetime.now(timezone.utc) - oldest_created).total_seconds()
                retry_after = max(1, int(WINDOW_SECONDS - age))

            raise RateLimitExceeded(
                repo=repo_full_name,
                current=count,
                limit=MAX_REVIEWS_PER_WINDOW,
                retry_after=retry_after,
            )

    except RateLimitExceeded:
        raise
    except Exception as exc:
        # Rate limiter failure must not block reviews. Log and allow.
        print(f"[RATE LIMIT] Check failed ({type(exc).__name__}): {exc}")

