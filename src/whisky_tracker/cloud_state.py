"""Integrity and snapshot helpers for ephemeral cloud runners."""

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path


class StateIntegrityError(RuntimeError):
    """A restored or newly produced SQLite state is not safe to publish."""


@dataclass(frozen=True, slots=True)
class StateValidation:
    path: Path
    exists: bool
    integrity: str
    schema_version: int | None


def validate_sqlite_state(path: str | Path, *, allow_missing: bool = False) -> StateValidation:
    """Validate durable state without creating a missing database accidentally."""
    database = Path(path)
    if not database.is_file():
        if allow_missing:
            return StateValidation(database, False, "missing", None)
        raise StateIntegrityError(f"SQLite state does not exist: {database}")
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            row = connection.execute("PRAGMA integrity_check").fetchone()
            if row is None or row[0] != "ok":
                detail = row[0] if row else "no result"
                raise StateIntegrityError(f"SQLite integrity check failed: {detail}")
            version_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            if not version_table:
                raise StateIntegrityError("SQLite state has no Whisky Tracker schema version")
            version_row = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()
            version = int(version_row[0] or 0)
            if version < 1:
                raise StateIntegrityError("SQLite state has no applied Whisky Tracker migration")
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise StateIntegrityError(f"SQLite state could not be read: {exc}") from exc
    return StateValidation(database, True, "ok", version)


def create_validated_snapshot(source: str | Path, destination: str | Path) -> StateValidation:
    """Create a transactionally consistent SQLite backup and validate the result."""
    source_path = Path(source)
    destination_path = Path(destination)
    validate_sqlite_state(source_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if destination_path.exists():
        destination_path.unlink()
    try:
        source_connection = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        destination_connection = sqlite3.connect(destination_path)
        try:
            source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
            source_connection.close()
    except sqlite3.Error as exc:
        raise StateIntegrityError(f"SQLite snapshot failed: {exc}") from exc
    return validate_sqlite_state(destination_path)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m whisky_tracker.cloud_state")
    subcommands = parser.add_subparsers(dest="command", required=True)
    validate = subcommands.add_parser("validate")
    validate.add_argument("path")
    validate.add_argument("--allow-missing", action="store_true")
    snapshot = subcommands.add_parser("snapshot")
    snapshot.add_argument("source")
    snapshot.add_argument("destination")
    args = parser.parse_args()
    if args.command == "validate":
        result = validate_sqlite_state(args.path, allow_missing=args.allow_missing)
    else:
        result = create_validated_snapshot(args.source, args.destination)
    print(
        f"state={result.path} exists={str(result.exists).lower()} "
        f"integrity={result.integrity} schema_version={result.schema_version}"
    )


if __name__ == "__main__":
    main()
