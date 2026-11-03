"""
Shared setup for the tests that need a real PostgreSQL.

These cover what test_db_layer.py cannot: the analytics SQL uses DATE_TRUNC and
other PostgreSQL features that SQLite does not have.

    TEST_DATABASE_URL=postgresql://postgres@localhost:5432/pr_reviewer_test python -m pytest integration

The suite wipes the tables it uses, so it refuses to run against any database
whose name does not contain "test". That guard is what stands between a mistyped
variable and an empty production audit trail.
"""

import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

ROOT = Path(__file__).parent.parent
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

if TEST_DATABASE_URL:
    if "test" not in (make_url(TEST_DATABASE_URL).database or ""):
        raise RuntimeError("TEST_DATABASE_URL must name a database containing 'test'.")
    # Set before database.py is imported, because it reads this once at import
    # time. load_dotenv does not overwrite a variable that is already set, so a
    # real DATABASE_URL in a local .env cannot take over.
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
else:
    # An empty value keeps a local .env from supplying a real database.
    os.environ["DATABASE_URL"] = ""

os.environ.setdefault("GOOGLE_API_KEY", "test-key-unused")


def pytest_collection_modifyitems(items):
    if TEST_DATABASE_URL:
        return
    skip = pytest.mark.skip(reason="TEST_DATABASE_URL is not set")
    for item in items:
        if "needs_postgres" in item.keywords:
            item.add_marker(skip)


def pytest_configure(config):
    config.addinivalue_line("markers", "needs_postgres: runs against a real PostgreSQL")


@pytest.fixture(scope="session")
def migrated_database():
    """Build the schema with Alembic, so the migration gets tested as well."""
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        check=True,
    )


@pytest.fixture
def db_session(migrated_database):
    from sqlalchemy import text

    from database import SessionLocal

    session = SessionLocal()
    session.execute(text(
        "TRUNCATE findings, reviews, review_consensus, pull_requests, repositories RESTART IDENTITY CASCADE"
    ))
    session.commit()
    yield session
    session.close()


def add_pr(session, repo, number, *, status="COMPLETED", passed=True, duration_ms=2000, created_at=None, findings=()):
    """Insert one reviewed PR, with its consensus row and a Security review."""
    from models import Finding, PullRequest, Review, ReviewConsensus, ReviewStatus, Severity, VerdictType

    pr = PullRequest(
        repository=repo,
        pr_number=number,
        head_sha=f"{number:040d}",
        status=ReviewStatus[status],
        review_duration_ms=duration_ms,
        created_at=created_at or datetime.now(timezone.utc),
    )
    session.add(pr)
    if status == "COMPLETED":
        session.add(ReviewConsensus(
            pull_request=pr, approvals=5 if passed else 3, total_reviewers=5, consensus_passed=passed,
        ))
        review = Review(
            pull_request=pr,
            reviewer_type="security",
            verdict=VerdictType.APPROVE if passed else VerdictType.REQUEST_CHANGES,
            summary="A summary.",
        )
        session.add(review)
        for path, severity in findings:
            session.add(Finding(review=review, description="A finding.", file_path=path, severity=Severity[severity]))
    session.commit()
    return pr


@pytest.fixture
def seeded(db_session):
    """Two repositories with known numbers, plus rows the queries must leave out."""
    from models import Repository

    now = datetime.now(timezone.utc)
    alpha = Repository(full_name="acme/alpha", clone_url="https://github.com/acme/alpha.git")
    beta = Repository(full_name="acme/beta", clone_url="https://github.com/acme/beta.git")
    db_session.add_all([alpha, beta])
    db_session.commit()

    # alpha: 3 completed (2 passed, 1 blocked) at 2, 4 and 6 s, so 66.7% and 4 s.
    add_pr(db_session, alpha, 1, passed=True, duration_ms=2000, findings=[("app/a.py", "WARNING")])
    add_pr(db_session, alpha, 2, passed=True, duration_ms=4000, findings=[("app/a.py", "CRITICAL")])
    add_pr(db_session, alpha, 3, passed=False, duration_ms=6000,
           findings=[("app/a.py", "CRITICAL"), ("app/b.py", "INFO"), (None, "INFO")])
    # alpha: failed and pending reviews, which the rates must not count.
    add_pr(db_session, alpha, 4, status="FAILED", duration_ms=None)
    add_pr(db_session, alpha, 5, status="PENDING", duration_ms=None)
    # alpha: older than the default 30-day window.
    add_pr(db_session, alpha, 6, passed=False, created_at=now - timedelta(days=45))
    # beta: one recent, approved.
    add_pr(db_session, beta, 1, passed=True, duration_ms=1000)
    return db_session


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def live_server():
    """The real app on a real port, because a browser needs something to talk to."""
    import uvicorn

    from main import app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("The test server did not start in time.")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch()
        yield chromium
        chromium.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    new_page = context.new_page()
    yield new_page
    context.close()
