"""Shared fixtures. Frozen after Wave 0 of Phase 1 — add per-module fixtures in your own test file."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from pact_core.db import Database


class FixedClock:
    """Deterministic, controllable clock. Call it to get the current naive-UTC time."""

    def __init__(self, start: datetime = datetime(2026, 11, 12, 9, 0, 0)):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> datetime:
        self.now = self.now + timedelta(**kwargs)
        return self.now


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def db(tmp_path) -> Database:
    """Temp-file SQLite database (a file, not :memory:, so threads share it) with all tables created."""
    database = Database(f"sqlite:///{tmp_path / 'pact_test.db'}")
    database.create_all()
    yield database
    database.engine.dispose()
