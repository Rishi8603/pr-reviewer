"""
Database engine, session factory, and declarative base.

This module owns the connection pool and is the sole place where DATABASE_URL
is read. Every other module that touches PostgreSQL imports `get_db` or
`SessionLocal` from here, so changing the database is a change to this file and
nowhere else — the same pattern qdrant_store.py uses for the vector store.

Connection pooling is configured for a single-process FastAPI service behind
Starlette's BackgroundTasks threadpool: pool_size is the number of threads that
can hold a connection simultaneously, and max_overflow is how many extra
connections SQLAlchemy will open during a burst before raising.
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    engine = create_engine(
        DATABASE_URL,
        # Reviewers never touch the database. These connections serve the worker's
        # writes plus the analytics and health requests; overflow covers a burst.
        pool_size=5,
        max_overflow=3,
        # Replace a connection the server dropped while it sat idle, rather than
        # failing the next query on it.
        pool_pre_ping=True,
        pool_recycle=1800,
    )
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
else:
    engine = None
    SessionLocal = None


class Base(DeclarativeBase):
    """Shared declarative base for all models."""
    pass


def get_db() -> Session | None:
    """Yield a transactional session, or None if no database is configured.

    Usage:
        db = get_db()
        if db:
            with db:
                db.add(...)
                db.commit()

    The session is intentionally not auto-committed: callers choose when to
    commit so a multi-table write is one transaction, not five.
    """
    if SessionLocal is None:
        return None
    return SessionLocal()


def check_connection() -> bool:
    """Return True if the database is reachable. Used by /health."""
    if engine is None:
        return False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
