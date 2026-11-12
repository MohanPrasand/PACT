"""Database plumbing.

Concurrency model: every write path runs inside ``Database.transaction()``. On SQLite each
transaction starts with ``BEGIN IMMEDIATE`` so writers are serialized, and budget/grant
mutations additionally use conditional UPDATEs (``... WHERE <invariant holds>``) whose
row counts are checked. The conditional updates are what make reservations and grant
consumption atomic; they remain correct on PostgreSQL as well.

Rule: never call out to the network (payment providers) or to another service that opens
its own write transaction while a transaction is open.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Callable, Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite does not preserve tzinfo, so we store naive UTC everywhere)."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):  # pragma: no cover - driver glue
        dbapi_conn.isolation_level = None  # let SQLAlchemy emit BEGIN itself
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn):  # pragma: no cover - driver glue
        conn.exec_driver_sql("BEGIN IMMEDIATE")


class Database:
    def __init__(self, url: str):
        kwargs: dict = {}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 10}
        self.engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            _configure_sqlite(self.engine)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        """Create every table registered on ``Base.metadata``. Core registers its own models here;
        other packages (e.g. merchant_lab) register theirs simply by being imported first."""
        import pact_core.models  # noqa: F401

        Base.metadata.create_all(self.engine)

    def drop_all(self) -> None:
        import pact_core.models  # noqa: F401

        Base.metadata.drop_all(self.engine)

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            with session.begin():
                yield session
        finally:
            session.close()
