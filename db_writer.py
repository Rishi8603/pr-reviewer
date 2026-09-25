"""
Transactional persistence of a completed review.

A single function maps the LangGraph result to the five database tables in one
transaction. If any part fails, nothing is partially committed — this is
important because half-written data (a PR row with no reviews, or reviews with
missing findings) would make the analytics queries return misleading numbers.

This module deliberately does not import or call anything from the review
pipeline; it only knows about database models and plain dicts. That separation
means the review can run and succeed even if the database is down — the review
still gets posted to GitHub, it just is not persisted for analytics.
"""

import re
import traceback
from datetime import datetime, timezone

from database import get_db
from models import (
    Finding,
    PullRequest,
    Repository,
    Review,
    ReviewConsensus,
    ReviewStatus,
    Severity,
    VerdictType,
)


def _classify_severity(finding_text: str) -> Severity:
    """Heuristic severity from the finding's text.

    The LLM reviewers do not return a severity field (Verdict has verdict +
    summary + findings-as-strings), so we classify after the fact by keyword.
    This is imperfect, but it populates the severity column with something more
    useful than a blanket WARNING on every row — and the hotspot / severity
    analytics queries depend on it.
    """
    lower = finding_text.lower()

    critical_patterns = [
        "credential", "secret", "password", "token", "injection",
        "sql injection", "command injection", "path traversal",
        "authentication", "authoriz", "deseriali", "remote code",
        "hardcoded", "exposed", "leak",
    ]
    if any(pattern in lower for pattern in critical_patterns):
        return Severity.CRITICAL

    warning_patterns = [
        "n+1", "complexity", "unbounded", "blocking", "race condition",
        "missing test", "no test", "unhandled", "exception", "error",
        "edge case", "boundary", "timeout", "memory",
    ]
    if any(pattern in lower for pattern in warning_patterns):
        return Severity.WARNING

    return Severity.INFO


def _extract_file_path(finding_text: str) -> str | None:
    """Try to pull a file path from the finding text.

    Reviewers often cite the file they are talking about. A rough regex beats
    NULL in every row for the hotspot queries.
    """
    # Match patterns like "in file.py", "file.py:", "path/to/file.py"
    match = re.search(r"[\w./\\-]+\.py\b", finding_text)
    return match.group(0) if match else None


def _get_or_create_repo(db, repo_full_name: str, clone_url: str) -> Repository:
    """Return the repository row, creating it on first contact."""
    repo = db.query(Repository).filter_by(full_name=repo_full_name).first()
    if repo is None:
        repo = Repository(full_name=repo_full_name, clone_url=clone_url)
        db.add(repo)
        db.flush()  # Assigns repo.id without committing the transaction.
    return repo


def persist_review(
    repo_full_name: str,
    clone_url: str,
    pr_number: int,
    head_sha: str,
    result: dict,
    duration_ms: int,
    comment_url: str | None = None,
    author: str | None = None,
    title: str | None = None,
) -> bool:
    """Persist a completed review as one transaction. Returns True on success.

    This is called from the background worker after the review graph has run
    and the GitHub comment has been posted. It must not raise — a database
    failure should not retroactively fail a review that already succeeded on
    GitHub.
    """
    db = get_db()
    if db is None:
        # No DATABASE_URL configured — analytics is opt-in.
        return False

    try:
        with db:
            # 1. Repository (get or create)
            repo = _get_or_create_repo(db, repo_full_name, clone_url)
            repo.last_reviewed_at = datetime.now(timezone.utc)

            # 2. Pull request
            pr = PullRequest(
                repository_id=repo.id,
                pr_number=pr_number,
                head_sha=head_sha,
                author=author,
                title=title,
                status=ReviewStatus.COMPLETED,
                completed_at=datetime.now(timezone.utc),
                review_duration_ms=duration_ms,
            )
            db.add(pr)
            db.flush()  # Assigns pr.id so reviews can reference it

            # 3. Individual reviewer verdicts + findings
            from reviewer import REVIEWERS

            for spec in REVIEWERS:
                verdict = result.get(spec["key"])
                if verdict is None:
                    continue

                review = Review(
                    pull_request_id=pr.id,
                    reviewer_type=spec["key"],
                    verdict=VerdictType(verdict.verdict),
                    summary=verdict.summary,
                )
                db.add(review)
                db.flush()  # Assigns review.id

                for finding_text in verdict.findings:
                    finding = Finding(
                        review_id=review.id,
                        description=finding_text,
                        severity=_classify_severity(finding_text),
                        file_path=_extract_file_path(finding_text),
                    )
                    db.add(finding)

            # 4. Consensus
            consensus = ReviewConsensus(
                pull_request_id=pr.id,
                approvals=result.get("approvals", 0),
                total_reviewers=result.get("total_reviewers", 0),
                consensus_passed=result.get("consensus_passed", False),
                github_comment_url=comment_url,
            )
            db.add(consensus)

            db.commit()
            print(f"[DB] Persisted review for {repo_full_name}#{pr_number} @ {head_sha[:7]}")
            return True

    except Exception as exc:
        print(f"[DB] Failed to persist review: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return False


def persist_failed_review(
    repo_full_name: str,
    clone_url: str,
    pr_number: int,
    head_sha: str,
    error: str,
    duration_ms: int,
    author: str | None = None,
    title: str | None = None,
) -> bool:
    """Record a review that failed before completing. Returns True on success."""
    db = get_db()
    if db is None:
        return False

    try:
        with db:
            repo = _get_or_create_repo(db, repo_full_name, clone_url)

            pr = PullRequest(
                repository_id=repo.id,
                pr_number=pr_number,
                head_sha=head_sha,
                author=author,
                title=title,
                status=ReviewStatus.FAILED,
                completed_at=datetime.now(timezone.utc),
                review_duration_ms=duration_ms,
            )
            db.add(pr)
            db.commit()
            print(f"[DB] Persisted failed review for {repo_full_name}#{pr_number}")
            return True

    except Exception as exc:
        print(f"[DB] Failed to persist failed review: {type(exc).__name__}: {exc}")
        return False
