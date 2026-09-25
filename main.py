"""
The webhook entrypoint.

GitHub gives a webhook delivery about 10 seconds to be acknowledged, and a
five-reviewer swarm on a cold vector store takes considerably longer than that.
So the handler does the smallest possible amount of work -- verify the
signature, decide whether we care about the event, claim it -- and hands the
review to a BackgroundTask, which Starlette runs only after the 200 has already
been written to the socket.
"""

import asyncio
import hashlib
import hmac
import json
import os
import threading
import time
import traceback
from collections import OrderedDict

import requests
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from analytics import router as analytics_router
from database import check_connection as check_db_connection
from db_writer import persist_failed_review, persist_review
from ingest import ensure_memory
from qdrant_store import MemoryBusy, count_vectors
from rate_limiter import RateLimitExceeded, check_rate_limit
from retrieve import search_codebase
from reviewer import initial_state, pr_reviewer_graph

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")

GITHUB_API = "https://api.github.com"
GITHUB_TIMEOUT = 30

# The name this service claims in the PR's checks list. Marking this context as
# required in branch protection is what turns a failed tally into a disabled
# merge button.
STATUS_CONTEXT = "pr-review-swarm"

REVIEW_ACTIONS = {"opened", "synchronize", "reopened"}

app = FastAPI(title="Autonomous CI/CD PR Review Swarm")
app.include_router(analytics_router)


# =====================================================================
# WEBHOOK AUTHENTICITY
# =====================================================================
def verify_signature(raw_body: bytes, signature_header: str | None) -> None:
    """Reject any payload GitHub did not sign.

    This endpoint is public, and acting on a request makes the service clone a
    URL from the body and spend LLM credits on it. Without this check, anyone who
    learns the URL can do both. The comparison is constant-time so a valid digest
    cannot be discovered one byte at a time by timing the responses.
    """
    if not WEBHOOK_SECRET:
        # Unset is allowed so the local curl walkthrough in the README still
        # works, but it is never correct in a deployment.
        print("WARNING: GITHUB_WEBHOOK_SECRET is unset - signature verification disabled.")
        return

    if not signature_header:
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256")

    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=401, detail="Invalid signature")


# =====================================================================
# DELIVERY DE-DUPLICATION
# Webhooks are at-least-once, and `synchronize` fires on every push to the
# branch. Keying on the head SHA means one review per commit no matter how many
# times we are told about it.
#
# This is process-local, so it stops being effective the moment the service runs
# more than one worker -- the same constraint that makes embedded Qdrant
# single-process. Both want the same fix: shared state in Redis.
# =====================================================================
MAX_TRACKED_DELIVERIES = 512
_claimed: OrderedDict[tuple[str, str], bool] = OrderedDict()
_claim_lock = threading.Lock()


def claim_delivery(key: tuple[str, str]) -> bool:
    """Return True if this is the first time we have seen this commit."""
    with _claim_lock:
        if key in _claimed:
            return False
        _claimed[key] = True
        while len(_claimed) > MAX_TRACKED_DELIVERIES:
            _claimed.popitem(last=False)
        return True


def release_delivery(key: tuple[str, str]) -> None:
    """Un-claim a commit so a failed review can be retried by re-pushing."""
    with _claim_lock:
        _claimed.pop(key, None)


# =====================================================================
# GITHUB API
# =====================================================================
def _headers(accept: str = "application/vnd.github+json") -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_pr_diff(repo_full_name: str, pr_number: int) -> str:
    """Fetch the PR as a unified diff.

    The diff media type on the pulls endpoint returns the patch text directly,
    which avoids walking the files endpoint and paginating through it.
    """
    response = requests.get(
        f"{GITHUB_API}/repos/{repo_full_name}/pulls/{pr_number}",
        headers=_headers("application/vnd.github.v3.diff"),
        timeout=GITHUB_TIMEOUT,
    )
    response.raise_for_status()
    return response.text


def post_pr_comment(repo_full_name: str, pr_number: int, body: str) -> str | None:
    """Post the review as an issue comment. Returns the comment's URL."""
    response = requests.post(
        f"{GITHUB_API}/repos/{repo_full_name}/issues/{pr_number}/comments",
        headers=_headers(),
        json={"body": body},
        timeout=GITHUB_TIMEOUT,
    )
    response.raise_for_status()
    return response.json().get("html_url")


def post_commit_status(
    repo_full_name: str,
    sha: str,
    state: str,
    description: str,
    target_url: str | None = None,
) -> None:
    """Attach a commit status to the PR's head SHA.

    This is the actual merge gate. A comment is advisory -- a developer can read
    it and merge anyway. A commit status named as a required check in branch
    protection makes GitHub itself refuse the merge until it passes.

    state is one of pending, success, failure, error.
    """
    payload = {
        "state": state,
        "context": STATUS_CONTEXT,
        # GitHub truncates past 140 characters; do it here so the text we send is
        # the text that gets shown.
        "description": description[:140],
    }
    if target_url:
        payload["target_url"] = target_url

    try:
        response = requests.post(
            f"{GITHUB_API}/repos/{repo_full_name}/statuses/{sha}",
            headers=_headers(),
            json=payload,
            timeout=GITHUB_TIMEOUT,
        )
        response.raise_for_status()
        print(f"Commit status -> {state} ({STATUS_CONTEXT})")
    except requests.RequestException as exc:
        # Never let a status update failure abort a review that otherwise worked.
        print(f"Could not set commit status: {exc}")


# =====================================================================
# THE BACKGROUND WORKER
# =====================================================================
def process_pr(
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    clone_url: str,
    author: str | None = None,
    title: str | None = None,
) -> None:
    print(f"\n[BACKGROUND] Reviewing {repo_full_name}#{pr_number} @ {head_sha[:7]}")
    claim_key = (repo_full_name, head_sha)
    start_time = time.monotonic()

    try:
        post_commit_status(
            repo_full_name, head_sha, "pending", "Review swarm running..."
        )

        # Self-heal first. On a cold process this clones and indexes the repo; on
        # a warm one it is a single count() and returns immediately.
        blocks = ensure_memory(clone_url, token=GITHUB_TOKEN)

        diff_text = get_pr_diff(repo_full_name, pr_number)
        if not diff_text.strip():
            print("Empty diff - nothing to review.")
            post_commit_status(
                repo_full_name, head_sha, "success", "No reviewable changes in diff."
            )
            return

        context = search_codebase(diff_text)

        print("Running the 5-reviewer swarm...")
        result = pr_reviewer_graph.invoke(initial_state(diff_text, context))

        approvals = result["approvals"]
        total = result["total_reviewers"]
        passed = result["consensus_passed"]

        comment_url = post_pr_comment(repo_full_name, pr_number, result["final_review"])

        post_commit_status(
            repo_full_name,
            head_sha,
            "success" if passed else "failure",
            f"{approvals}/{total} reviewers approved"
            + ("" if passed else " - unanimous approval required"),
            target_url=comment_url,
        )

        duration_ms = int((time.monotonic() - start_time) * 1000)
        print(
            f"[DONE] {repo_full_name}#{pr_number}: {approvals}/{total} "
            f"({blocks} blocks in memory, {duration_ms}ms)"
        )

        # Persist the full review to PostgreSQL for analytics.
        persist_review(
            repo_full_name=repo_full_name,
            clone_url=clone_url,
            pr_number=pr_number,
            head_sha=head_sha,
            result=result,
            duration_ms=duration_ms,
            comment_url=comment_url,
            author=author,
            title=title,
        )

    except Exception as exc:
        # This runs after the response was sent, so an exception here has nowhere
        # to surface. Reporting it as an errored commit status is what makes a
        # broken review visible in the PR instead of vanishing into the logs.
        duration_ms = int((time.monotonic() - start_time) * 1000)
        print(f"[FAILED] {repo_full_name}#{pr_number}: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        post_commit_status(
            repo_full_name,
            head_sha,
            "error",
            f"Review failed: {type(exc).__name__}",
        )
        persist_failed_review(
            repo_full_name=repo_full_name,
            clone_url=clone_url,
            pr_number=pr_number,
            head_sha=head_sha,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=duration_ms,
            author=author,
            title=title,
        )
        release_delivery(claim_key)


# =====================================================================
# ROUTES
# =====================================================================
@app.get("/")
def root() -> dict:
    return {
        "service": "Autonomous CI/CD PR Review Swarm",
        "webhook": "POST /webhook",
        "health": "GET /health",
    }


@app.get("/health")
def health() -> dict:
    """Liveness plus how much codebase memory is currently loaded."""
    try:
        # Do not block behind an in-progress ingest: a probe that hangs for the
        # length of a cold boot reads as a dead service.
        vectors = count_vectors(lock_timeout=1.0)
        memory = "warm" if vectors else "cold"
    except MemoryBusy:
        vectors, memory = None, "indexing"
    except Exception as exc:
        vectors, memory = None, f"error: {type(exc).__name__}"

    return {
        "status": "ok",
        "vectors": vectors,
        "memory": memory,
        "signature_verification": bool(WEBHOOK_SECRET),
        "database": "connected" if check_db_connection() else "not configured",
    }


@app.post("/webhook")
async def github_webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    # The raw bytes are required for the HMAC: re-serialising the parsed JSON
    # would change whitespace and key order, and the digest would never match.
    raw_body = await request.body()
    verify_signature(raw_body, request.headers.get("X-Hub-Signature-256"))

    event_type = request.headers.get("X-GitHub-Event")
    if event_type == "ping":
        return {"status": "pong"}
    if event_type != "pull_request":
        return {"status": "ignored", "reason": f"event {event_type}"}

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Body is not valid JSON")

    action = payload.get("action")
    if action not in REVIEW_ACTIONS:
        return {"status": "ignored", "reason": f"action {action}"}

    try:
        pull_request = payload["pull_request"]
        pr_number = pull_request["number"]
        head_sha = pull_request["head"]["sha"]
        repository = payload["repository"]
        repo_full_name = repository["full_name"]
        clone_url = repository["clone_url"]
    except (KeyError, TypeError):
        raise HTTPException(status_code=400, detail="Malformed pull_request payload")

    if pull_request.get("draft"):
        return {"status": "ignored", "reason": "draft PR"}

    # Per-repository rate limiting (DB-backed sliding window).
    # Run in a thread to avoid blocking the async event loop — the DB query
    # can take tens of ms on a cold connection.
    try:
        await asyncio.to_thread(check_rate_limit, repo_full_name)
    except RateLimitExceeded as exc:
        print(f"Rate limited: {exc}")
        return JSONResponse(
            status_code=429,
            content={
                "status": "rate_limited",
                "detail": str(exc),
                "retry_after": exc.retry_after,
            },
            headers={"Retry-After": str(exc.retry_after)},
        )

    claim_key = (repo_full_name, head_sha)
    if not claim_delivery(claim_key):
        # A duplicate delivery, or a second push event for a commit already in
        # flight. Still a 200: GitHub should not retry this.
        print(f"Already reviewing {repo_full_name}@{head_sha[:7]} - skipping duplicate.")
        return {"status": "duplicate", "sha": head_sha}

    # Extract PR metadata for analytics persistence.
    author = pull_request.get("user", {}).get("login")
    pr_title = pull_request.get("title")

    print(f"Queued {repo_full_name}#{pr_number} @ {head_sha[:7]}")
    background_tasks.add_task(
        process_pr, repo_full_name, pr_number, head_sha, clone_url,
        author=author, title=pr_title,
    )

    return {
        "status": "queued",
        "pr": pr_number,
        "sha": head_sha,
    }
