"""
Tests for the PostgreSQL persistence layer and analytics helpers.

These tests use SQLite in-memory as a stand-in for PostgreSQL so they run
offline with no database server. The SQL dialect differences (no PERCENTILE_CONT,
no DATE_TRUNC in SQLite) mean the analytics endpoint SQL is not tested here —
those are integration tests that need a real PostgreSQL. What IS tested here is
the transactional persistence logic, severity classification, and file path
extraction logic.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from database import Base
from db_writer import (
    _classify_severity,
    _extract_file_path,
    _get_or_create_repo,
    persist_failed_review,
    persist_review,
)
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
from reviewer import Verdict


def _make_test_session():
    """Create a fresh SQLite in-memory database and return a session factory."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


def _make_graph_result(
    security="APPROVE",
    performance="APPROVE",
    style="APPROVE",
    qa="APPROVE",
    pm="APPROVE",
):
    """Build a fake LangGraph result dict matching what pr_reviewer_graph.invoke returns."""
    def _verdict(key, v):
        findings = [] if v == "APPROVE" else [f"{key} issue found in main.py"]
        return Verdict(
            verdict=v,
            summary=f"{key} review {'passed' if v == 'APPROVE' else 'found issues'}.",
            findings=findings,
        )

    verdicts = {
        "security": _verdict("security", security),
        "performance": _verdict("performance", performance),
        "style": _verdict("style", style),
        "qa": _verdict("qa", qa),
        "pm": _verdict("pm", pm),
    }

    approvals = sum(1 for v in verdicts.values() if v.verdict == "APPROVE")
    total = 5

    return {
        **verdicts,
        "approvals": approvals,
        "total_reviewers": total,
        "consensus_passed": approvals == total,
        "final_review": "## Review Report\n...",
    }


# =====================================================================
# SEVERITY CLASSIFICATION
# =====================================================================
class TestSeverityClassification(unittest.TestCase):
    """The heuristic severity classifier on finding text."""

    def test_credential_leak_is_critical(self):
        self.assertEqual(
            _classify_severity("Hardcoded API credential found in config.py"),
            Severity.CRITICAL,
        )

    def test_sql_injection_is_critical(self):
        self.assertEqual(
            _classify_severity("Potential SQL injection in query builder"),
            Severity.CRITICAL,
        )

    def test_missing_auth_is_critical(self):
        self.assertEqual(
            _classify_severity("Missing authentication check on admin endpoint"),
            Severity.CRITICAL,
        )

    def test_n_plus_1_is_warning(self):
        self.assertEqual(
            _classify_severity("N+1 query inside the user listing loop"),
            Severity.WARNING,
        )

    def test_missing_test_is_warning(self):
        self.assertEqual(
            _classify_severity("No test coverage for the new endpoint"),
            Severity.WARNING,
        )

    def test_unhandled_exception_is_warning(self):
        self.assertEqual(
            _classify_severity("Unhandled ValueError in parse_input"),
            Severity.WARNING,
        )

    def test_style_suggestion_is_info(self):
        self.assertEqual(
            _classify_severity("Consider renaming 'x' to something more descriptive"),
            Severity.INFO,
        )

    def test_empty_string_is_info(self):
        self.assertEqual(_classify_severity(""), Severity.INFO)


# =====================================================================
# FILE PATH EXTRACTION
# =====================================================================
class TestFilePathExtraction(unittest.TestCase):
    """Regex-based file path extraction from finding text."""

    def test_extracts_simple_filename(self):
        self.assertEqual(
            _extract_file_path("Issue found in main.py at line 42"),
            "main.py",
        )

    def test_extracts_path_with_directory(self):
        self.assertEqual(
            _extract_file_path("Missing test for src/utils/helpers.py"),
            "src/utils/helpers.py",
        )

    def test_returns_none_for_no_python_file(self):
        self.assertIsNone(
            _extract_file_path("General code quality concern")
        )

    def test_extracts_first_match(self):
        result = _extract_file_path("Changed in foo.py and bar.py")
        self.assertEqual(result, "foo.py")


# =====================================================================
# TRANSACTIONAL PERSISTENCE
# =====================================================================
class TestPersistReview(unittest.TestCase):
    """persist_review writes all five tables in one transaction."""

    def setUp(self):
        self.TestSession, self.engine = _make_test_session()

    def tearDown(self):
        Base.metadata.drop_all(self.engine)

    def _patch_get_db(self):
        """Patch get_db to return a session from our test database."""
        return patch("db_writer.get_db", return_value=self.TestSession())

    def test_successful_review_creates_all_rows(self):
        result = _make_graph_result(security="REQUEST_CHANGES")

        with self._patch_get_db():
            success = persist_review(
                repo_full_name="test/repo",
                clone_url="https://github.com/test/repo.git",
                pr_number=1,
                head_sha="abc1234567890",
                result=result,
                duration_ms=5000,
                comment_url="https://github.com/test/repo/pull/1#comment",
                author="testuser",
                title="Fix bug",
            )

        self.assertTrue(success)

        with self.TestSession() as db:
            # Repository created
            repos = db.query(Repository).all()
            self.assertEqual(len(repos), 1)
            self.assertEqual(repos[0].full_name, "test/repo")

            # Pull request created
            prs = db.query(PullRequest).all()
            self.assertEqual(len(prs), 1)
            self.assertEqual(prs[0].pr_number, 1)
            self.assertEqual(prs[0].status, ReviewStatus.COMPLETED)
            self.assertEqual(prs[0].review_duration_ms, 5000)
            self.assertEqual(prs[0].author, "testuser")

            # 5 reviews created (one per reviewer)
            reviews = db.query(Review).all()
            self.assertEqual(len(reviews), 5)

            # Security reviewer has REQUEST_CHANGES
            sec_review = [r for r in reviews if r.reviewer_type == "security"][0]
            self.assertEqual(sec_review.verdict, VerdictType.REQUEST_CHANGES)

            # 4 approvals
            approves = [r for r in reviews if r.verdict == VerdictType.APPROVE]
            self.assertEqual(len(approves), 4)

            # Findings from the security reviewer
            findings = db.query(Finding).all()
            self.assertEqual(len(findings), 1)
            self.assertIn("security", findings[0].description)

            # Consensus
            consensus = db.query(ReviewConsensus).first()
            self.assertIsNotNone(consensus)
            self.assertEqual(consensus.approvals, 4)
            self.assertEqual(consensus.total_reviewers, 5)
            self.assertFalse(consensus.consensus_passed)

    def test_unanimous_approval_sets_consensus_passed(self):
        result = _make_graph_result()  # all APPROVE

        with self._patch_get_db():
            persist_review(
                repo_full_name="test/repo",
                clone_url="https://github.com/test/repo.git",
                pr_number=2,
                head_sha="def4567890123",
                result=result,
                duration_ms=3000,
            )

        with self.TestSession() as db:
            consensus = db.query(ReviewConsensus).first()
            self.assertTrue(consensus.consensus_passed)
            self.assertEqual(consensus.approvals, 5)

    def test_repo_is_reused_not_duplicated(self):
        result = _make_graph_result()

        with self._patch_get_db():
            persist_review(
                repo_full_name="test/repo",
                clone_url="https://github.com/test/repo.git",
                pr_number=1,
                head_sha="aaa1111111111",
                result=result,
                duration_ms=1000,
            )

        with self._patch_get_db():
            persist_review(
                repo_full_name="test/repo",
                clone_url="https://github.com/test/repo.git",
                pr_number=2,
                head_sha="bbb2222222222",
                result=result,
                duration_ms=2000,
            )

        with self.TestSession() as db:
            repos = db.query(Repository).all()
            self.assertEqual(len(repos), 1, "Second review should reuse existing repo row")
            prs = db.query(PullRequest).all()
            self.assertEqual(len(prs), 2)

    def test_no_db_returns_false_gracefully(self):
        result = _make_graph_result()

        with patch("db_writer.get_db", return_value=None):
            success = persist_review(
                repo_full_name="test/repo",
                clone_url="https://github.com/test/repo.git",
                pr_number=1,
                head_sha="abc1234567890",
                result=result,
                duration_ms=1000,
            )

        self.assertFalse(success)


class TestPersistFailedReview(unittest.TestCase):
    """persist_failed_review records a failed review attempt."""

    def setUp(self):
        self.TestSession, self.engine = _make_test_session()

    def tearDown(self):
        Base.metadata.drop_all(self.engine)

    def test_failed_review_has_failed_status(self):
        with patch("db_writer.get_db", return_value=self.TestSession()):
            success = persist_failed_review(
                repo_full_name="test/repo",
                clone_url="https://github.com/test/repo.git",
                pr_number=5,
                head_sha="fail123456789",
                error="TimeoutError: Gemini did not respond",
                duration_ms=90000,
            )

        self.assertTrue(success)

        with self.TestSession() as db:
            pr = db.query(PullRequest).first()
            self.assertEqual(pr.status, ReviewStatus.FAILED)
            self.assertEqual(pr.review_duration_ms, 90000)
            # No reviews or findings for a failed PR
            self.assertEqual(db.query(Review).count(), 0)
            self.assertEqual(db.query(Finding).count(), 0)


# =====================================================================
# GET OR CREATE REPO
# =====================================================================
class TestGetOrCreateRepo(unittest.TestCase):
    def setUp(self):
        self.TestSession, self.engine = _make_test_session()

    def tearDown(self):
        Base.metadata.drop_all(self.engine)

    def test_creates_new_repo(self):
        with self.TestSession() as db:
            repo = _get_or_create_repo(db, "new/repo", "https://github.com/new/repo.git")
            db.commit()
            self.assertIsNotNone(repo.id)
            self.assertEqual(repo.full_name, "new/repo")

    def test_returns_existing_repo(self):
        with self.TestSession() as db:
            repo1 = _get_or_create_repo(db, "existing/repo", "https://github.com/existing/repo.git")
            db.commit()

        with self.TestSession() as db:
            repo2 = _get_or_create_repo(db, "existing/repo", "https://github.com/existing/repo.git")
            db.commit()
            self.assertEqual(repo1.id, repo2.id)


if __name__ == "__main__":
    unittest.main()
