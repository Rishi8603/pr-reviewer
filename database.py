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
        # Five persistent connections cover the five concurrent reviewer threads;
        # overflow handles a simultaneous webhook + health check.
        pool_size=5,
        max_overflow=3,
        pool_pre_ping=True,
        # Long reviews block the connection; the default 30s recycle is too
        # aggressive for a cold-boot review that can take minutes.
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
