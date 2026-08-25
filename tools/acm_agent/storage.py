"""SQLite persistence for the local ACM learning workflow.

The public :class:`Database` facade composes domain-specific repository mixins.
Platform clients still use one connection and :meth:`Database.atomic`, so a
broken or partial network response never erases a good snapshot.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from .storage_ai import _AiStorageMixin
from .storage_common import (
    MarkdownSummaryProposalRevisionConflict,
    MarkdownSummaryTargetRevisionConflict,
    PlanRevisionConflict,
    ProblemContextConflict,
    TagOverrideRevisionConflict,
    utc_now,
)
from .storage_markdown import _MarkdownStorageMixin
from .storage_plan import _PlanStorageMixin
from .storage_problem import _ProblemStorageMixin
from .storage_schema import MIGRATIONS, SCHEMA_VERSION


# Schema versions whose *preceding* schema is snapshotted when the database is
# opened directly at that schema (the upgrade's first step).
_BACKUP_ON_DIRECT_OPEN = frozenset((*range(5, 15), 17))
# Subset that is also snapshotted when the schema is merely passed through as an
# intermediate step of a longer upgrade.  Versions 5 and 6 keep the legacy
# direct-open-only behavior.
_BACKUP_AS_INTERMEDIATE = frozenset((*range(7, 15), 17))


class Database(
    _ProblemStorageMixin,
    _AiStorageMixin,
    _MarkdownStorageMixin,
    _PlanStorageMixin,
):
    """Small repository-style wrapper around a versioned SQLite database."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit makes single repository calls durable.  ``atomic`` still
        # provides explicit all-or-nothing batches for platform snapshots.
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        try:
            self.migrate()
        except Exception:
            self.connection.close()
            raise

    def _backup_before_migration(self, target_version: int) -> None:
        """Create one adjacent backup for the schema entering a migration."""

        source_version = int(target_version) - 1
        current = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if current != source_version or not self.path.exists():
            return
        backup_path = self.path.with_name(f"{self.path.name}.v{source_version}.bak")
        if backup_path.exists():
            self._validate_migration_backup(backup_path, source_version)
            return
        temporary = backup_path.with_name(f".{backup_path.name}.tmp")
        temporary.unlink(missing_ok=True)
        destination = sqlite3.connect(temporary)
        try:
            self.connection.backup(destination)
            destination.commit()
        finally:
            destination.close()
        try:
            self._validate_migration_backup(temporary, source_version)
            os.replace(temporary, backup_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _validate_migration_backup(path: Path, expected_version: int) -> None:
        """Fail closed unless a migration backup is readable and consistent."""

        try:
            connection = sqlite3.connect(path)
            try:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise RuntimeError(f"migration backup is not readable: {path}") from exc
        if version != expected_version:
            raise RuntimeError(
                f"migration backup schema {version} does not match {expected_version}: {path}"
            )
        if quick_check != "ok":
            raise RuntimeError(f"migration backup failed integrity check: {path}")

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def migrate(self) -> None:
        current = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {current} is newer than supported {SCHEMA_VERSION}"
            )
        initial_target = current + 1
        if initial_target in _BACKUP_ON_DIRECT_OPEN:
            self._backup_before_migration(initial_target)
        for version in range(current + 1, SCHEMA_VERSION + 1):
            if current != 0 and version in _BACKUP_AS_INTERMEDIATE:
                self._backup_before_migration(version)
            migration = MIGRATIONS.get(version)
            if migration is None:
                raise RuntimeError(f"missing database migration {version}")
            rebuilds_fk_parent = version == 17
            if rebuilds_fk_parent:
                # v17 retires persistent stress state and rebuilds ai_runs to
                # remove its stress-only preparation metadata column.  Child
                # tables keep referencing the replacement table by the same
                # name, so enforcement is disabled only for the DDL window.
                self.connection.execute("PRAGMA foreign_keys = OFF")
            try:
                # ``executescript`` commits any pending transaction before it
                # runs. Put BEGIN in the script itself so DDL and user_version
                # remain one crash-safe migration under autocommit mode.
                self.connection.executescript("BEGIN IMMEDIATE;\n" + migration)
                if version == 5:
                    self._backfill_attempt_tag_snapshots_v5()
                self.connection.execute(f"PRAGMA user_version = {version}")
                self.connection.execute("COMMIT")
            except Exception:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise
            finally:
                if rebuilds_fk_parent:
                    self.connection.execute("PRAGMA foreign_keys = ON")


    @contextmanager
    def atomic(self) -> Iterator[sqlite3.Connection]:
        """Open a transaction, nesting safely if the caller already has one."""
        if self.connection.in_transaction:
            name = f"acm_nested_{id(self):x}"
            self.connection.execute(f"SAVEPOINT {name}")
            try:
                yield self.connection
            except Exception:
                self.connection.execute(f"ROLLBACK TO {name}")
                self.connection.execute(f"RELEASE {name}")
                raise
            else:
                self.connection.execute(f"RELEASE {name}")
            return
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.connection.execute(sql, params))


def open_database(root: str | Path) -> Database:
    return Database(Path(root) / ".acm" / "state.db")


# A semantic alias for callers that prefer repository terminology.
Store = Database


__all__ = [
    "Database",
    "MarkdownSummaryProposalRevisionConflict",
    "MarkdownSummaryTargetRevisionConflict",
    "PlanRevisionConflict",
    "ProblemContextConflict",
    "Store",
    "TagOverrideRevisionConflict",
    "open_database",
    "utc_now",
]
