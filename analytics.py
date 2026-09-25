"""
Analytics API — the interview goldmine.

Five endpoints that run real PostgreSQL queries you can pull up and walk through
in a data analytics interview. Each query uses at least one non-trivial SQL
feature (CTEs, window functions, PERCENTILE_CONT, DENSE_RANK, DATE_TRUNC).

These endpoints are read-only and do not affect the review pipeline. If the
database is not configured, they all return a 503.

The queries are written in raw SQL rather than ORM because:
  1. In an interview you will be asked to write SQL, not SQLAlchemy.
  2. Window functions and PERCENTILE_CONT have no clean ORM equivalent.
  3. The interviewer can see the exact query — no abstraction layer to explain.
"""

import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import text

from database import get_db

# Analytics data can contain internal review details. Gate behind an API key
# when one is configured; allow open access otherwise (local dev).
ANALYTICS_API_KEY = os.getenv("ANALYTICS_API_KEY")
_api_key_header = APIKeyHeader(name="X-Analytics-Key", auto_error=False)


async def _verify_analytics_key(api_key: str | None = Security(_api_key_header)):
    """Reject unauthenticated requests when ANALYTICS_API_KEY is set."""
    if ANALYTICS_API_KEY and api_key != ANALYTICS_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing analytics API key.")


router = APIRouter(
    prefix="/analytics",
    tags=["analytics"],
    dependencies=[Depends(_verify_analytics_key)],
)


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
# Repository-level stats with percentile latency.
# Interview topics: GROUP BY, CASE WHEN, AVG, PERCENTILE_CONT.
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
            ROUND(AVG(pr.review_duration_ms) / 1000.0, 2) AS avg_review_seconds,
            ROUND(
                (PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY pr.review_duration_ms)
                / 1000.0)::numeric, 2
            ) AS p95_review_seconds
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
# ENDPOINT 2: REVIEWER AGREEMENT
# Which reviewers disagree most often?
# Interview topics: CTE, self-join, conditional aggregation.
# =====================================================================
@router.get("/reviewer-agreement")
def reviewer_agreement(
    days: int = Query(default=30, ge=1, le=365),
):
    """Pairwise agreement rates between reviewers."""
    db = _require_db()
    since = datetime.now(timezone.utc) - timedelta(days=days)

    query = text("""
        WITH reviewer_verdicts AS (
            SELECT
                pr.id AS pr_id,
                rv.reviewer_type,
                rv.verdict
            FROM reviews rv
            JOIN pull_requests pr ON rv.pull_request_id = pr.id
            WHERE pr.created_at >= :since
              AND pr.status = 'COMPLETED'
        )
        SELECT
            a.reviewer_type AS reviewer_a,
            b.reviewer_type AS reviewer_b,
            COUNT(*) AS total_shared_prs,
            SUM(CASE WHEN a.verdict = b.verdict THEN 1 ELSE 0 END) AS agreements,
            ROUND(
                SUM(CASE WHEN a.verdict = b.verdict THEN 1 ELSE 0 END)::decimal
                / NULLIF(COUNT(*), 0) * 100, 1
            ) AS agreement_rate_pct
        FROM reviewer_verdicts a
        JOIN reviewer_verdicts b
          ON a.pr_id = b.pr_id
         AND a.reviewer_type < b.reviewer_type
        GROUP BY a.reviewer_type, b.reviewer_type
        ORDER BY agreement_rate_pct ASC
    """)

    with db:
        rows = db.execute(query, {"since": since}).mappings().all()

    return {
        "period_days": days,
        "pairs": [dict(row) for row in rows],
    }


# =====================================================================
# ENDPOINT 3: HOTSPOTS
# Files with the most findings.
# Interview topics: DENSE_RANK, conditional SUM, GROUP BY + HAVING.
# =====================================================================
@router.get("/hotspots")
def hotspots(
    limit: int = Query(default=20, ge=1, le=100),
    days: int = Query(default=30, ge=1, le=365),
):
    """Files with the most review findings, ranked by density."""
    db = _require_db()
    since = datetime.now(timezone.utc) - timedelta(days=days)

    query = text("""
        SELECT
            r.full_name AS repo,
            f.file_path,
            COUNT(*) AS total_findings,
            SUM(CASE WHEN f.severity = 'CRITICAL' THEN 1 ELSE 0 END) AS critical_count,
            SUM(CASE WHEN f.severity = 'WARNING'  THEN 1 ELSE 0 END) AS warning_count,
            SUM(CASE WHEN f.severity = 'INFO'     THEN 1 ELSE 0 END) AS info_count,
            SUM(CASE WHEN rv.reviewer_type = 'security' THEN 1 ELSE 0 END) AS security_findings,
            DENSE_RANK() OVER (ORDER BY COUNT(*) DESC) AS hotspot_rank
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
# ENDPOINT 4: TRENDS
# Weekly review time-series.
# Interview topics: DATE_TRUNC, time-series aggregation, conditional SUM.
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
            SUM(CASE WHEN pr.status = 'FAILED' THEN 1 ELSE 0 END) AS failed,
            ROUND(AVG(pr.review_duration_ms) / 1000.0, 2) AS avg_duration_sec
        FROM pull_requests pr
        LEFT JOIN review_consensus rc ON rc.pull_request_id = pr.id
        WHERE pr.status IN ('COMPLETED', 'FAILED')
        GROUP BY DATE_TRUNC('week', pr.created_at)
        ORDER BY week DESC
        LIMIT :weeks
    """)

    with db:
        rows = db.execute(query, {"weeks": weeks}).mappings().all()

    results = []
    for row in rows:
        row_dict = dict(row)
        # Serialize the datetime for JSON response.
        if row_dict.get("week"):
            row_dict["week"] = row_dict["week"].isoformat()
        results.append(row_dict)

    return {"weeks_requested": weeks, "trends": results}


# =====================================================================
# ENDPOINT 5: PER-REVIEWER FINDINGS
# A specific reviewer's recent findings with window functions.
# Interview topics: ROW_NUMBER, PARTITION BY, parameterised query.
# =====================================================================
VALID_REVIEWER_TYPES = {"security", "performance", "style", "qa", "pm"}


@router.get("/reviewer/{reviewer_type}/findings")
def reviewer_findings(
    reviewer_type: str,
    limit: int = Query(default=50, ge=1, le=200),
    days: int = Query(default=30, ge=1, le=365),
):
    """Recent findings from a specific reviewer, with severity breakdown."""
    if reviewer_type not in VALID_REVIEWER_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid reviewer type. Choose from: {sorted(VALID_REVIEWER_TYPES)}",
        )

    db = _require_db()
    since = datetime.now(timezone.utc) - timedelta(days=days)

    query = text("""
        SELECT
            rv.reviewer_type,
            f.severity,
            f.description,
            f.file_path,
            pr.head_sha,
            pr.pr_number,
            r.full_name AS repo,
            COUNT(*) OVER (PARTITION BY rv.reviewer_type, f.severity) AS severity_total,
            ROW_NUMBER() OVER (
                PARTITION BY rv.reviewer_type
                ORDER BY f.id DESC
            ) AS recency_rank
        FROM findings f
        JOIN reviews rv ON f.review_id = rv.id
        JOIN pull_requests pr ON rv.pull_request_id = pr.id
        JOIN repositories r ON pr.repository_id = r.id
        WHERE rv.reviewer_type = :reviewer_type
          AND pr.created_at >= :since
        ORDER BY f.severity, f.id DESC
        LIMIT :lim
    """)

    with db:
        rows = db.execute(
            query, {"reviewer_type": reviewer_type, "since": since, "lim": limit}
        ).mappings().all()

    return {
        "reviewer": reviewer_type,
        "period_days": days,
        "findings": [dict(row) for row in rows],
    }


# =====================================================================
# BONUS: SUMMARY STATS (lightweight, for /health enrichment)
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
