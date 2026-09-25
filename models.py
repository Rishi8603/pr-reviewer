"""
SQLAlchemy models for the review audit trail.

Five tables, normalised to 3NF:

    repositories ──< pull_requests ──< reviews ──< findings
                                   └──< review_consensus (1:1)

Design decisions worth knowing for an interview:

- **repositories** is separated from pull_requests so repo-level aggregations
  (approval rate per repo, most-reviewed repo) are a GROUP BY on a foreign key
  rather than a GROUP BY on a string. It also avoids storing clone_url once per
  PR row.

- **findings** is its own table instead of a JSON array on reviews because
  relational queries on findings (hotspots by file, severity distribution) need
  individual rows to aggregate on. A JSON column would push that work into the
  application layer.

- **review_consensus** is 1:1 with pull_requests. It could be columns on
  pull_requests, but separating it keeps the PR row immutable after creation
  (the PR metadata is written at webhook time, consensus is written minutes later
  after the swarm finishes).

- **severity** on findings is an enum because the set of values is closed and
  small. PostgreSQL enums are stored as 4 bytes, compared to a variable-length
  VARCHAR.
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


# =====================================================================
# ENUMS
# =====================================================================
class ReviewStatus(enum.Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class VerdictType(enum.Enum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"


class Severity(enum.Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


# =====================================================================
# MODELS
# =====================================================================
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    full_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    clone_url: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    pull_requests: Mapped[list["PullRequest"]] = relationship(
        back_populates="repository", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Repository {self.full_name}>"


class PullRequest(Base):
    __tablename__ = "pull_requests"
    __table_args__ = (
        # Mirrors the in-memory (repo, head_sha) dedup but durable across restarts.
        UniqueConstraint("repository_id", "pr_number", "head_sha", name="uq_pr_sha"),
        # The two indexes most analytics queries filter or join on.
        Index("ix_pr_repo_created", "repository_id", "created_at"),
        Index("ix_pr_head_sha", "head_sha"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repository_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("repositories.id"), nullable=False
    )
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus), default=ReviewStatus.PENDING, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    review_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Relationships
    repository: Mapped["Repository"] = relationship(back_populates="pull_requests")
    reviews: Mapped[list["Review"]] = relationship(
        back_populates="pull_request", cascade="all, delete-orphan"
    )
    consensus: Mapped["ReviewConsensus | None"] = relationship(
        back_populates="pull_request", uselist=False, cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<PullRequest #{self.pr_number} @ {self.head_sha[:7]}>"


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        # Fast aggregation: "how often does the security reviewer reject?"
        Index("ix_review_type_verdict", "reviewer_type", "verdict"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pull_request_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pull_requests.id"), nullable=False
    )
    reviewer_type: Mapped[str] = mapped_column(String(50), nullable=False)
    verdict: Mapped[VerdictType] = mapped_column(Enum(VerdictType), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    # Relationships
    pull_request: Mapped["PullRequest"] = relationship(back_populates="reviews")
    findings: Mapped[list["Finding"]] = relationship(
        back_populates="review", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Review {self.reviewer_type}: {self.verdict.value}>"


class Finding(Base):
    __tablename__ = "findings"
    __table_args__ = (
        # Hotspot queries filter on file_path.
        Index("ix_finding_file_path", "file_path"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    review_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reviews.id"), nullable=False
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[Severity] = mapped_column(
        Enum(Severity), default=Severity.WARNING, nullable=False
    )
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Relationships
    review: Mapped["Review"] = relationship(back_populates="findings")

    def __repr__(self) -> str:
        return f"<Finding [{self.severity.value}] {self.description[:40]}>"


class ReviewConsensus(Base):
    __tablename__ = "review_consensus"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pull_request_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pull_requests.id"), unique=True, nullable=False
    )
    approvals: Mapped[int] = mapped_column(Integer, nullable=False)
    total_reviewers: Mapped[int] = mapped_column(Integer, nullable=False)
    consensus_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    github_comment_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    # Relationships
    pull_request: Mapped["PullRequest"] = relationship(back_populates="consensus")

    def __repr__(self) -> str:
        return f"<Consensus {self.approvals}/{self.total_reviewers} {'✓' if self.consensus_passed else '✗'}>"
