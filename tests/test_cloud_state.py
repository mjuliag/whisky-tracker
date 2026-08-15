import sqlite3
from pathlib import Path

import pytest

from whisky_tracker.cloud_state import (
    StateIntegrityError,
    create_validated_snapshot,
    validate_sqlite_state,
)
from whisky_tracker.persistence import SQLiteRepository


def test_missing_state_is_allowed_only_for_explicit_bootstrap(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"
    result = validate_sqlite_state(path, allow_missing=True)
    assert result.exists is False
    assert result.integrity == "missing"
    assert not path.exists()
    with pytest.raises(StateIntegrityError, match="does not exist"):
        validate_sqlite_state(path)


def test_valid_state_reports_integrity_and_schema(tmp_path: Path) -> None:
    path = tmp_path / "valid.db"
    with SQLiteRepository(path) as repository:
        repository.initialize()
    result = validate_sqlite_state(path)
    assert result.exists is True
    assert result.integrity == "ok"
    assert result.schema_version == 2


def test_corrupt_state_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"not a sqlite database")
    with pytest.raises(StateIntegrityError, match="could not be read"):
        validate_sqlite_state(path)


def test_empty_sqlite_file_is_not_accepted_as_restored_application_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    with pytest.raises(StateIntegrityError, match="no Whisky Tracker schema"):
        validate_sqlite_state(path)


def test_snapshot_is_independent_and_valid(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "snapshot.db"
    with SQLiteRepository(source) as repository:
        repository.initialize()
    result = create_validated_snapshot(source, destination)
    assert result.integrity == "ok"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE after_snapshot(value TEXT)")
    with sqlite3.connect(destination) as connection:
        assert (
            connection.execute("SELECT 1 FROM sqlite_master WHERE name='after_snapshot'").fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM observations "
                "WHERE longitude IS NOT NULL OR latitude IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
