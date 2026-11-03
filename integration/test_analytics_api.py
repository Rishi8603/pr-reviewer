"""
API tests for /analytics, against a real PostgreSQL with known data.

Each expected number is worked out by hand from the `seeded` fixture, so a wrong
JOIN or a wrong filter in the SQL fails here.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)
postgres = pytest.mark.needs_postgres


def get_json(path: str) -> dict:
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


def repo_row(body: dict, name: str) -> dict:
    return next(row for row in body["repositories"] if row["full_name"] == name)


# =====================================================================
# THE API CONTRACT (no database needed)
# =====================================================================
class TestContract:
    def test_openapi_spec_lists_every_analytics_endpoint(self):
        paths = get_json("/openapi.json")["paths"]
        for path in ("overview", "hotspots", "trends", "summary"):
            assert f"/analytics/{path}" in paths

    def test_openapi_spec_documents_the_query_limits(self):
        spec = get_json("/openapi.json")["paths"]["/analytics/overview"]["get"]
        days = next(param for param in spec["parameters"] if param["name"] == "days")
        assert days["schema"]["minimum"] == 1
        assert days["schema"]["maximum"] == 365

    @pytest.mark.parametrize("path", [
        "/analytics/overview?days=0",
        "/analytics/overview?days=366",
        "/analytics/overview?days=abc",
        "/analytics/hotspots?limit=0",
        "/analytics/hotspots?limit=101",
        "/analytics/trends?weeks=0",
        "/analytics/trends?weeks=53",
    ])
    def test_out_of_range_parameters_are_rejected(self, path):
        assert client.get(path).status_code == 422

    @pytest.mark.parametrize("path", [
        "/analytics/overview", "/analytics/hotspots", "/analytics/trends", "/analytics/summary",
    ])
    def test_no_database_gives_a_clear_503(self, path, monkeypatch):
        monkeypatch.setattr("analytics.get_db", lambda: None)
        response = client.get(path)
        assert response.status_code == 503
        assert "DATABASE_URL" in response.json()["detail"]

    @pytest.mark.parametrize("path", ["/analytics/summary", "/analytics/overview?days=7; DROP TABLE findings"])
    def test_post_is_not_allowed_and_sql_in_a_parameter_is_rejected(self, path):
        assert client.post(path).status_code == 405
        if "DROP" in path:
            assert client.get(path).status_code == 422


# =====================================================================
# /analytics/summary
# =====================================================================
@postgres
class TestSummary:
    def test_counts(self, seeded):
        assert get_json("/analytics/summary") == {
            "total_repos": 2,
            "total_prs": 7,
            "completed_prs": 5,
            "failed_prs": 1,
            "total_reviews": 5,
            "total_findings": 5,
            "critical_findings": 2,
        }

    def test_empty_database_gives_zeros_and_not_nulls(self, db_session):
        assert set(get_json("/analytics/summary").values()) == {0}


# =====================================================================
# /analytics/overview
# =====================================================================
@postgres
class TestOverview:
    def test_rates_count_only_completed_reviews_in_the_window(self, seeded):
        alpha = repo_row(get_json("/analytics/overview"), "acme/alpha")
        assert alpha["total_prs_reviewed"] == 3
        assert float(alpha["approval_rate_pct"]) == 66.7
        assert float(alpha["avg_review_seconds"]) == 4.0

    def test_a_wider_window_brings_in_the_old_pr(self, seeded):
        alpha = repo_row(get_json("/analytics/overview?days=60"), "acme/alpha")
        assert alpha["total_prs_reviewed"] == 4
        assert float(alpha["approval_rate_pct"]) == 50.0

    def test_most_reviewed_repository_comes_first(self, seeded):
        names = [row["full_name"] for row in get_json("/analytics/overview")["repositories"]]
        assert names == ["acme/alpha", "acme/beta"]

    def test_empty_database_gives_an_empty_list(self, db_session):
        assert get_json("/analytics/overview")["repositories"] == []


# =====================================================================
# /analytics/hotspots
# =====================================================================
@postgres
class TestHotspots:
    def test_ranked_with_severity_split_and_null_paths_excluded(self, seeded):
        spots = get_json("/analytics/hotspots")["hotspots"]
        assert [spot["file_path"] for spot in spots] == ["app/a.py", "app/b.py"]
        assert spots[0]["total_findings"] == 3
        assert spots[0]["critical_count"] == 2
        assert spots[0]["warning_count"] == 1
        assert spots[0]["security_findings"] == 3

    def test_limit_keeps_the_worst_file(self, seeded):
        spots = get_json("/analytics/hotspots?limit=1")["hotspots"]
        assert [spot["file_path"] for spot in spots] == ["app/a.py"]


# =====================================================================
# /analytics/trends
# =====================================================================
@postgres
class TestTrends:
    def test_a_week_of_reviews_is_grouped_and_split_by_outcome(self, db_session):
        from conftest import add_pr
        from models import Repository

        repo = Repository(full_name="acme/gamma", clone_url="https://github.com/acme/gamma.git")
        db_session.add(repo)
        db_session.commit()
        # A fixed Wednesday, so all three land in one week whatever day this runs.
        wednesday = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
        add_pr(db_session, repo, 1, passed=True, created_at=wednesday)
        add_pr(db_session, repo, 2, passed=True, created_at=wednesday + timedelta(hours=1))
        add_pr(db_session, repo, 3, passed=False, created_at=wednesday + timedelta(hours=2))

        trends = get_json("/analytics/trends")["trends"]
        assert len(trends) == 1
        assert trends[0]["week"].startswith("2026-09-14")
        assert (trends[0]["reviews"], trends[0]["approved"], trends[0]["rejected"]) == (3, 2, 1)

    def test_newest_week_first_and_limited(self, db_session):
        from conftest import add_pr
        from models import Repository

        repo = Repository(full_name="acme/delta", clone_url="https://github.com/acme/delta.git")
        db_session.add(repo)
        db_session.commit()
        for offset in range(3):
            add_pr(db_session, repo, offset + 1, created_at=datetime(2026, 9, 2, tzinfo=timezone.utc) + timedelta(weeks=offset))

        trends = get_json("/analytics/trends?weeks=2")["trends"]
        assert [row["week"][:10] for row in trends] == ["2026-09-14", "2026-09-07"]
