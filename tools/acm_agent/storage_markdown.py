"""Markdown knowledge target and proposal persistence."""

from __future__ import annotations
import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence
from .storage_common import (
    MarkdownSummaryProposalRevisionConflict,
    MarkdownSummaryTargetRevisionConflict,
    _UNSET,
    _json,
    _sha256_text,
    utc_now,
)

class _MarkdownStorageMixin:
    def create_markdown_summary_target(
        self,
        target_id: str,
        *,
        name: str,
        path: str | Path,
        preset: str,
        schema: Mapping[str, Any],
        schema_hash: str | None = None,
        enabled: bool = True,
        created_at: str | None = None,
    ) -> sqlite3.Row:
        """Register one exact local Markdown file and its declared schema."""

        stamp = created_at or utc_now()
        schema_json = _json(schema)
        self.connection.execute(
            """INSERT INTO markdown_summary_targets(
                   id,name,path,preset,schema_json,schema_hash,revision,enabled,
                   created_at,updated_at)
               VALUES(?,?,?,?,?,?,1,?,?,?)""",
            (
                str(target_id),
                str(name),
                str(path),
                str(preset),
                schema_json,
                schema_hash or _sha256_text(schema_json),
                int(bool(enabled)),
                stamp,
                stamp,
            ),
        )
        row = self.markdown_summary_target(str(target_id))
        assert row is not None
        return row

    def markdown_summary_target(self, target_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM markdown_summary_targets WHERE id=?", (str(target_id),)
        ).fetchone()

    def markdown_summary_target_by_path(self, path: str | Path) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM markdown_summary_targets WHERE path=?", (str(path),)
        ).fetchone()

    def markdown_summary_targets(
        self, *, enabled: bool | None = None
    ) -> list[sqlite3.Row]:
        if enabled is None:
            return self.query(
                """SELECT * FROM markdown_summary_targets
                   ORDER BY enabled DESC,updated_at DESC,id"""
            )
        return self.query(
            """SELECT * FROM markdown_summary_targets WHERE enabled=?
               ORDER BY updated_at DESC,id""",
            (int(bool(enabled)),),
        )

    def update_markdown_summary_target(
        self,
        target_id: str,
        *,
        expected_revision: int | None = None,
        name: str | object = _UNSET,
        path: str | Path | object = _UNSET,
        preset: str | object = _UNSET,
        schema: Mapping[str, Any] | object = _UNSET,
        schema_hash: str | object = _UNSET,
        enabled: bool | object = _UNSET,
    ) -> sqlite3.Row:
        row = self.markdown_summary_target(str(target_id))
        if row is None:
            raise KeyError(f"Markdown summary target {target_id!r} not found")
        actual = int(row["revision"])
        if expected_revision is not None and int(expected_revision) != actual:
            raise MarkdownSummaryTargetRevisionConflict(
                str(target_id), expected_revision, actual
            )

        assignments = ["updated_at=?", "revision=revision+1"]
        values: list[Any] = [utc_now()]
        for column, value in (("name", name), ("preset", preset)):
            if value is not _UNSET:
                assignments.append(f"{column}=?")
                values.append(str(value))
        if path is not _UNSET:
            assignments.append("path=?")
            values.append(str(path))
        if schema is not _UNSET:
            encoded = _json(schema)
            assignments.append("schema_json=?")
            values.append(encoded)
            if schema_hash is _UNSET:
                schema_hash = _sha256_text(encoded)
        if schema_hash is not _UNSET:
            assignments.append("schema_hash=?")
            values.append(str(schema_hash))
        if enabled is not _UNSET:
            assignments.append("enabled=?")
            values.append(int(bool(enabled)))
        values.extend((str(target_id), actual))
        cursor = self.connection.execute(
            f"""UPDATE markdown_summary_targets SET {','.join(assignments)}
                WHERE id=? AND revision=?""",
            values,
        )
        if cursor.rowcount != 1:
            current = self.markdown_summary_target(str(target_id))
            raise MarkdownSummaryTargetRevisionConflict(
                str(target_id), actual, int(current["revision"]) if current else None
            )
        updated = self.markdown_summary_target(str(target_id))
        assert updated is not None
        return updated

    def delete_markdown_summary_target(
        self, target_id: str, *, expected_revision: int | None = None
    ) -> None:
        row = self.markdown_summary_target(str(target_id))
        if row is None:
            raise KeyError(f"Markdown summary target {target_id!r} not found")
        actual = int(row["revision"])
        if expected_revision is not None and int(expected_revision) != actual:
            raise MarkdownSummaryTargetRevisionConflict(
                str(target_id), expected_revision, actual
            )
        cursor = self.connection.execute(
            "DELETE FROM markdown_summary_targets WHERE id=? AND revision=?",
            (str(target_id), actual),
        )
        if cursor.rowcount != 1:
            current = self.markdown_summary_target(str(target_id))
            raise MarkdownSummaryTargetRevisionConflict(
                str(target_id), actual, int(current["revision"]) if current else None
            )

    def create_markdown_summary_proposal(
        self,
        proposal_id: str,
        *,
        attempt_id: int,
        target_path: str | Path,
        target_existed: bool,
        schema: Mapping[str, Any],
        entry: Mapping[str, Any],
        candidate_bytes: bytes | bytearray | memoryview | str,
        diff_text: str,
        run_id: str | None = None,
        target_id: str | None = None,
        target_revision: int | None = None,
        baseline_hash: str | None = None,
        schema_hash: str | None = None,
        entry_markdown: str = "",
        candidate_hash: str | None = None,
        confidence: float | None = None,
        warnings: Sequence[Any] | Mapping[str, Any] | None = None,
        duplicate: Mapping[str, Any] | None = None,
        rationale: str = "",
        created_at: str | None = None,
    ) -> sqlite3.Row:
        stamp = created_at or utc_now()
        schema_json = _json(schema)
        payload = (
            candidate_bytes.encode("utf-8")
            if isinstance(candidate_bytes, str)
            else bytes(candidate_bytes)
        )
        self.connection.execute(
            """INSERT INTO markdown_summary_proposals(
                   id,attempt_id,run_id,target_id,target_revision,target_path,
                   target_existed,baseline_hash,schema_json,schema_hash,entry_json,
                   entry_markdown,candidate_bytes,candidate_hash,diff_text,confidence,
                   warnings_json,duplicate_json,rationale,revision,status,
                   created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'preview',?,?)""",
            (
                str(proposal_id),
                int(attempt_id),
                run_id,
                target_id,
                target_revision,
                str(target_path),
                int(bool(target_existed)),
                baseline_hash,
                schema_json,
                schema_hash or _sha256_text(schema_json),
                _json(entry),
                str(entry_markdown),
                sqlite3.Binary(payload),
                candidate_hash or hashlib.sha256(payload).hexdigest(),
                str(diff_text),
                confidence,
                _json(warnings or []),
                _json(duplicate or {}),
                str(rationale),
                stamp,
                stamp,
            ),
        )
        row = self.markdown_summary_proposal(str(proposal_id))
        assert row is not None
        return row

    def markdown_summary_proposal(self, proposal_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM markdown_summary_proposals WHERE id=?",
            (str(proposal_id),),
        ).fetchone()

    def markdown_summary_proposals(
        self,
        *,
        attempt_id: int | None = None,
        target_id: str | None = None,
        status: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        values: list[Any] = []
        if attempt_id is not None:
            clauses.append("attempt_id=?")
            values.append(int(attempt_id))
        if target_id is not None:
            clauses.append("target_id=?")
            values.append(str(target_id))
        if status is not None:
            clauses.append("status=?")
            values.append(str(status))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.query(
            f"""SELECT * FROM markdown_summary_proposals{where}
                ORDER BY created_at DESC,rowid DESC""",
            values,
        )

    def update_markdown_summary_proposal(
        self,
        proposal_id: str,
        *,
        expected_revision: int | None = None,
        target_id: str | None | object = _UNSET,
        target_revision: int | None | object = _UNSET,
        target_path: str | Path | object = _UNSET,
        target_existed: bool | object = _UNSET,
        baseline_hash: str | None | object = _UNSET,
        schema: Mapping[str, Any] | object = _UNSET,
        schema_hash: str | object = _UNSET,
        entry: Mapping[str, Any] | object = _UNSET,
        entry_markdown: str | object = _UNSET,
        candidate_bytes: bytes | bytearray | memoryview | str | object = _UNSET,
        candidate_hash: str | object = _UNSET,
        diff_text: str | object = _UNSET,
        confidence: float | None | object = _UNSET,
        warnings: Sequence[Any] | Mapping[str, Any] | object = _UNSET,
        duplicate: Mapping[str, Any] | object = _UNSET,
        rationale: str | object = _UNSET,
        status: str | object = _UNSET,
        backup_path: str | Path | None | object = _UNSET,
        applied_hash: str | None | object = _UNSET,
        error: Mapping[str, Any] | object = _UNSET,
        applied_at: str | None | object = _UNSET,
        reverted_at: str | None | object = _UNSET,
    ) -> sqlite3.Row:
        row = self.markdown_summary_proposal(str(proposal_id))
        if row is None:
            raise KeyError(f"Markdown summary proposal {proposal_id!r} not found")
        actual = int(row["revision"])
        if expected_revision is not None and int(expected_revision) != actual:
            raise MarkdownSummaryProposalRevisionConflict(
                str(proposal_id), expected_revision, actual
            )

        assignments = ["updated_at=?", "revision=revision+1"]
        values: list[Any] = [utc_now()]
        for column, value in (
            ("target_id", target_id),
            ("target_revision", target_revision),
            ("baseline_hash", baseline_hash),
            ("confidence", confidence),
            ("applied_hash", applied_hash),
            ("applied_at", applied_at),
            ("reverted_at", reverted_at),
        ):
            if value is not _UNSET:
                assignments.append(f"{column}=?")
                values.append(value)
        for column, value in (
            ("target_path", target_path),
            ("entry_markdown", entry_markdown),
            ("diff_text", diff_text),
            ("rationale", rationale),
            ("status", status),
        ):
            if value is not _UNSET:
                assignments.append(f"{column}=?")
                values.append(str(value))
        if target_existed is not _UNSET:
            assignments.append("target_existed=?")
            values.append(int(bool(target_existed)))
        if schema is not _UNSET:
            encoded = _json(schema)
            assignments.append("schema_json=?")
            values.append(encoded)
            if schema_hash is _UNSET:
                schema_hash = _sha256_text(encoded)
        if schema_hash is not _UNSET:
            assignments.append("schema_hash=?")
            values.append(str(schema_hash))
        if entry is not _UNSET:
            assignments.append("entry_json=?")
            values.append(_json(entry))
        if candidate_bytes is not _UNSET:
            payload = (
                candidate_bytes.encode("utf-8")
                if isinstance(candidate_bytes, str)
                else bytes(candidate_bytes)
            )
            assignments.append("candidate_bytes=?")
            values.append(sqlite3.Binary(payload))
            if candidate_hash is _UNSET:
                candidate_hash = hashlib.sha256(payload).hexdigest()
        if candidate_hash is not _UNSET:
            assignments.append("candidate_hash=?")
            values.append(str(candidate_hash))
        if warnings is not _UNSET:
            assignments.append("warnings_json=?")
            values.append(_json(warnings))
        if duplicate is not _UNSET:
            assignments.append("duplicate_json=?")
            values.append(_json(duplicate))
        if backup_path is not _UNSET:
            assignments.append("backup_path=?")
            values.append(str(backup_path) if backup_path is not None else None)
        if error is not _UNSET:
            assignments.append("error_json=?")
            values.append(_json(error))
        values.extend((str(proposal_id), actual))
        cursor = self.connection.execute(
            f"""UPDATE markdown_summary_proposals SET {','.join(assignments)}
                WHERE id=? AND revision=?""",
            values,
        )
        if cursor.rowcount != 1:
            current = self.markdown_summary_proposal(str(proposal_id))
            raise MarkdownSummaryProposalRevisionConflict(
                str(proposal_id), actual, int(current["revision"]) if current else None
            )
        updated = self.markdown_summary_proposal(str(proposal_id))
        assert updated is not None
        return updated
