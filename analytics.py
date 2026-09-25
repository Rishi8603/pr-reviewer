"""
Analytics API — SQL-powered insights over the review audit trail.

Four endpoints that query the PostgreSQL database for patterns in review data.
Each query uses standard SQL features (GROUP BY, JOINs, CASE WHEN, DATE_TRUNC)
that are straightforward to explain.

These endpoints are read-only and do not affect the review pipeline. If the
database is not configured, they all return a 503.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from database import get_db

router = APIRouter(prefix="/analytics", tags=["analytics"])


def _require_db():
    db = get_db()
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="Analytics unavailable: DATABASE_URL not configured.",
        )
    return db


# =====================================================================
# ENDPOINT 1: OVERVIEW
# Repository-level stats — how many PRs reviewed, what % approved.
# =====================================================================
@router.get("/overview")
def analytics_overview(
    days: int = Query(default=30, ge=1, le=365, description="Look-back window in days"),
):
    """Repository-level review statistics for the last N days."""
    db = _require_db()
    since = datetime.now(timezone.utc) - timedelta(days=days)

    query = text("""
        SELECT
            r.full_name,
            COUNT(DISTINCT pr.id) AS total_prs_reviewed,
            ROUND(
                AVG(CASE WHEN rc.consensus_passed THEN 1 ELSE 0 END) * 100, 1
            ) AS approval_rate_pct,
            ROUND(AVG(pr.review_duration_ms) / 1000.0, 2) AS avg_review_seconds
        FROM repositories r
        JOIN pull_requests pr ON pr.repository_id = r.id
        LEFT JOIN review_consensus rc ON rc.pull_request_id = pr.id
        WHERE pr.created_at >= :since
          AND pr.status = 'COMPLETED'
        GROUP BY r.id, r.full_name
        ORDER BY total_prs_reviewed DESC
    """)

    with db:
        rows = db.execute(query, {"since": since}).mappings().all()

    return {
        "period_days": days,
        "repositories": [dict(row) for row in rows],
    }


# =====================================================================
# ENDPOINT 2: HOTSPOTS
# Files with the most review findings — shows where code quality is weakest.
# =====================================================================
@router.get("/hotspots")
def hotspots(
    limit: int = Query(default=20, ge=1, le=100),
    days: int = Query(default=30, ge=1, le=365),
):
    """Files with the most review findings, ranked."""
    db = _require_db()
    since = datetime.now(timezone.utc) - timedelta(days=days)

    query = text("""
        SELECT
            r.full_name AS repo,
            f.file_path,
            COUNT(*) AS total_findings,
            SUM(CASE WHEN f.severity = 'CRITICAL' THEN 1 ELSE 0 END) AS critical_count,
            SUM(CASE WHEN f.severity = 'WARNING'  THEN 1 ELSE 0 END) AS warning_count,
            SUM(CASE WHEN rv.reviewer_type = 'security' THEN 1 ELSE 0 END) AS security_findings
        FROM findings f
        JOIN reviews rv ON f.review_id = rv.id
        JOIN pull_requests pr ON rv.pull_request_id = pr.id
        JOIN repositories r ON pr.repository_id = r.id
        WHERE f.file_path IS NOT NULL
          AND pr.created_at >= :since
        GROUP BY r.full_name, f.file_path
        ORDER BY total_findings DESC
        LIMIT :lim
    """)

    with db:
        rows = db.execute(query, {"since": since, "lim": limit}).mappings().all()

    return {
        "period_days": days,
        "hotspots": [dict(row) for row in rows],
    }


# =====================================================================
# ENDPOINT 3: TRENDS
# Weekly review volume — are we reviewing more or fewer PRs over time?
# =====================================================================
@router.get("/trends")
def trends(
    weeks: int = Query(default=12, ge=1, le=52, description="Number of weeks to return"),
):
    """Weekly review volume and outcome trends."""
    db = _require_db()

    query = text("""
        SELECT
            DATE_TRUNC('week', pr.created_at) AS week,
            COUNT(*) AS reviews,
            SUM(CASE WHEN rc.consensus_passed THEN 1 ELSE 0 END) AS approved,
            SUM(CASE WHEN NOT rc.consensus_passed THEN 1 ELSE 0 END) AS rejected,
            ROUND(AVG(pr.review_duration_ms) / 1000.0, 2) AS avg_duration_sec
        FROM pull_requests pr
        LEFT JOIN review_consensus rc ON rc.pull_request_id = pr.id
        WHERE pr.status = 'COMPLETED'
        GROUP BY DATE_TRUNC('week', pr.created_at)
        ORDER BY week DESC
        LIMIT :weeks
    """)

    with db:
        rows = db.execute(query, {"weeks": weeks}).mappings().all()

    results = []
    for row in rows:
        row_dict = dict(row)
        if row_dict.get("week"):
            row_dict["week"] = row_dict["week"].isoformat()
        results.append(row_dict)

    return {"weeks_requested": weeks, "trends": results}


# =====================================================================
# ENDPOINT 4: SUMMARY
# Quick counts — total PRs, findings, critical findings.
# =====================================================================
@router.get("/summary")
def summary():
    """Quick counts for dashboard headers."""
    db = _require_db()

    query = text("""
        SELECT
            (SELECT COUNT(*) FROM repositories) AS total_repos,
            (SELECT COUNT(*) FROM pull_requests) AS total_prs,
            (SELECT COUNT(*) FROM pull_requests WHERE status = 'COMPLETED') AS completed_prs,
            (SELECT COUNT(*) FROM pull_requests WHERE status = 'FAILED') AS failed_prs,
            (SELECT COUNT(*) FROM reviews) AS total_reviews,
            (SELECT COUNT(*) FROM findings) AS total_findings,
            (SELECT COUNT(*) FROM findings WHERE severity = 'CRITICAL') AS critical_findings
    """)

    with db:
        row = db.execute(query).mappings().one()

    return dict(row)
