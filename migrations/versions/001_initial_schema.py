"""Initial schema: repositories, pull_requests, reviews, findings, review_consensus.

Revision ID: 001_initial_schema
Revises: None
Create Date: 2026-09-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Enums ---
    # These are created implicitly by create_table when used as column types.
    # Defining them here so they can be reused across tables and in downgrade().
    review_status = sa.Enum("PENDING", "COMPLETED", "FAILED", name="reviewstatus")
    verdict_type = sa.Enum("APPROVE", "REQUEST_CHANGES", name="verdicttype")
    severity = sa.Enum("CRITICAL", "WARNING", "INFO", name="severity")


    # --- repositories ---
    op.create_table(
        "repositories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("clone_url", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("full_name"),
    )

    # --- pull_requests ---
    op.create_table(
        "pull_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(40), nullable=False),
        sa.Column("author", sa.String(255), nullable=True),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("status", review_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_duration_ms", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repository_id", "pr_number", "head_sha", name="uq_pr_sha"),
    )
    op.create_index("ix_pr_repo_created", "pull_requests", ["repository_id", "created_at"])
    op.create_index("ix_pr_head_sha", "pull_requests", ["head_sha"])

    # --- reviews ---
    op.create_table(
        "reviews",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pull_request_id", sa.Integer(), nullable=False),
        sa.Column("reviewer_type", sa.String(50), nullable=False),
        sa.Column("verdict", verdict_type, nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["pull_request_id"], ["pull_requests.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_type_verdict", "reviews", ["reviewer_type", "verdict"])

    # --- findings ---
    op.create_table(
        "findings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("review_id", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("severity", severity, nullable=False),
        sa.Column("file_path", sa.String(500), nullable=True),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_finding_file_path", "findings", ["file_path"])

    # --- review_consensus ---
    op.create_table(
        "review_consensus",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pull_request_id", sa.Integer(), nullable=False),
        sa.Column("approvals", sa.Integer(), nullable=False),
        sa.Column("total_reviewers", sa.Integer(), nullable=False),
        sa.Column("consensus_passed", sa.Boolean(), nullable=False),
        sa.Column("github_comment_url", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["pull_request_id"], ["pull_requests.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pull_request_id"),
    )


def downgrade() -> None:
    op.drop_table("review_consensus")
    op.drop_table("findings")
    op.drop_table("reviews")
    op.drop_table("pull_requests")
    op.drop_table("repositories")

    sa.Enum(name="severity").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="verdicttype").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="reviewstatus").drop(op.get_bind(), checkfirst=True)
