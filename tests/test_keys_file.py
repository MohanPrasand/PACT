"""`pact init` against a non-default database must never overwrite the default database's agent keys."""

from __future__ import annotations

import json
import stat

from pact_core.config import Settings
from pact_server import cli, ops


def test_keys_file_is_scoped_to_the_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert ops.keys_file(ops.DEFAULT_DATABASE_URL) == ops.KEYS_FILE
    other = ops.keys_file("sqlite:////tmp/other.db")
    assert other != ops.KEYS_FILE and other.parent == ops.KEYS_FILE.parent
    assert other == ops.keys_file("sqlite:////tmp/other.db") != ops.keys_file("sqlite:////tmp/third.db")


def test_seeding_another_database_leaves_default_keys_untouched(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ops.save_keys({"ag_shopping": "real-key"})
    before = ops.KEYS_FILE.read_text()

    temp_url = f"sqlite:///{tmp_path / 'temp.db'}"
    settings = Settings(_env_file=None, PACT_DATABASE_URL=temp_url, PACT_PAYMENT_PROVIDER="simulation")
    result = cli._seed(settings)

    assert ops.KEYS_FILE.read_text() == before
    scoped = ops.keys_file(temp_url)
    assert set(json.loads(scoped.read_text())) == set(result["new_keys"]) and result["new_keys"]
    assert stat.S_IMODE(scoped.stat().st_mode) == 0o600
    # --reset of the temp DB removes only its own keys file
    cli._seed(settings, reset=True)
    assert ops.KEYS_FILE.read_text() == before
