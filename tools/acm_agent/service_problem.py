"""Problem workspace, attempt lifecycle, skip, and review service methods."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
import json
from typing import Any, Mapping

from .config import load_config
from .recommend import compute_weakness
from .service_common import (
    FAILURE_MODES,
    RESULTS,
    _db_problem_id,
    _display_problem_id,
    _problem_key,
)
from .storage import Database
from .workspace import (
    ProblemRef,
    parse_problem_ref,
    save_default_template,
    start_problem,
    validate_default_template,
)


class ServiceProblemMixin:
    def start(
        self,
        problem: str,
        *,
        with_stress: bool = False,
        template: str | None = None,
    ) -> dict[str, Any]:
        load_config(self.paths)
        if template is not None:
            save_default_template(
                self.paths.root, validate_default_template(template)
            )
        ref = parse_problem_ref(problem)
        with Database(self.paths.database) as db:
            db_id = _db_problem_id(ref.platform, ref.problem_id)
            if db.problem_status(ref.platform, db_id) == "skipped":
                raise ValueError(f"{ref.problem_id} 已被 Skip；请先 unskip 再开始")
        result = start_problem(self.paths.root, problem, with_stress=with_stress)
        with Database(self.paths.database) as db:
            problem_id = _db_problem_id(result.problem.platform, result.problem.problem_id)
            db.upsert_problem({"platform": result.problem.platform, "problem_id": problem_id})
            db.upsert_local_file(result.source, result.problem.platform, problem_id)
            attempt_id = self._find_or_start_attempt(db, result.problem)
        return {**result.to_dict(), "attempt_id": attempt_id, "ok": True}

    def verify(
        self,
        problem: str | None = None,
        *,
        debug: bool = False,
        exact: bool = False,
        timeout: float = 2.0,
        stress_iterations: int = 100,
        seed: int | None = None,
    ) -> dict[str, Any]:
        load_config(self.paths)
        selected = problem
        if not selected:
            with Database(self.paths.database) as db:
                active = [row for row in db.attempts() if row["active"]]
            if not active:
                raise ValueError("未指定题号，且没有 active session")
            selected = _display_problem_id(active[0]["platform"], active[0]["problem_id"])
        result = self._verify(
            self.paths.root,
            selected,
            exact=exact,
            debug=debug,
            timeout=float(timeout),
            stress_iterations=int(stress_iterations),
            seed=seed,
        )
        payload = result.to_dict()
        payload["ok"] = result.passed
        return payload

    def problem_skip(
        self,
        problem: str,
        *,
        reason: str = "idea_clear_without_editorial",
        note: str = "",
        notes: str | None = None,
        source: str = "agent",
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        ref = parse_problem_ref(problem)
        problem_id = _db_problem_id(ref.platform, ref.problem_id)
        reason = str(reason or "idea_clear_without_editorial").strip()
        if reason != "idea_clear_without_editorial":
            raise ValueError("Skip reason 必须是 idea_clear_without_editorial")
        note = str(note if notes is None else notes).strip()
        source = str(source or "agent").strip().lower()
        if source not in {"web", "cli", "agent"}:
            raise ValueError("source 必须是 web、cli 或 agent")
        if context is not None and not isinstance(context, Mapping):
            raise ValueError("context 必须是 JSON 对象")
        with Database(self.paths.database) as db:
            db.upsert_problem({"platform": ref.platform, "problem_id": problem_id})
            if db.problem_status(ref.platform, problem_id) == "accepted":
                raise ValueError(f"{ref.problem_id} 已 AC，不能 Skip")
            active = db.connection.execute(
                """SELECT 1 FROM attempts
                   WHERE platform=? AND problem_id=? AND active=1 LIMIT 1""",
                (ref.platform, problem_id),
            ).fetchone()
            if active:
                raise ValueError(f"{ref.problem_id} 存在 active session，不能 Skip")
            db.skip_problem(
                ref.platform,
                problem_id,
                notes=note,
                source=source,
                context=context,
            )
        return {
            "ok": True,
            "problem": ref.problem_id,
            "platform": ref.platform,
            "problem_id": ref.problem_id,
            "status": "skipped",
            "disposition": "skipped_mastered",
            "reason": reason,
            "note": note,
            "notes": note,
        }

    def problem_unskip(
        self,
        problem: str,
        *,
        reason: str = "idea_clear_without_editorial",
        note: str = "",
        notes: str | None = None,
        source: str = "agent",
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        ref = parse_problem_ref(problem)
        problem_id = _db_problem_id(ref.platform, ref.problem_id)
        if str(reason or "idea_clear_without_editorial") != "idea_clear_without_editorial":
            raise ValueError("Skip reason 必须是 idea_clear_without_editorial")
        source = str(source or "agent").strip().lower()
        if source not in {"web", "cli", "agent"}:
            raise ValueError("source 必须是 web、cli 或 agent")
        if context is not None and not isinstance(context, Mapping):
            raise ValueError("context 必须是 JSON 对象")
        with Database(self.paths.database) as db:
            note = str(note if notes is None else notes).strip()
            removed = db.unskip_problem(
                ref.platform,
                problem_id,
                notes=note,
                source=source,
                context=context,
            )
            status = db.problem_status(ref.platform, problem_id)
        return {
            "ok": True,
            "problem": ref.problem_id,
            "platform": ref.platform,
            "problem_id": ref.problem_id,
            "unskipped": removed,
            "status": status,
            "disposition": None,
            "note": note,
            "notes": note,
        }

    def skipped_problems(self) -> dict[str, Any]:
        with Database(self.paths.database) as db:
            rows: list[dict[str, Any]] = []
            for disposition in db.problem_dispositions():
                platform = disposition["platform"]
                problem_id = disposition["problem_id"]
                if db.problem_status(platform, problem_id) != "skipped":
                    continue
                problem = db.connection.execute(
                    "SELECT name,url FROM problems WHERE platform=? AND problem_id=?",
                    (platform, problem_id),
                ).fetchone()
                display_id = _display_problem_id(platform, problem_id)
                rows.append(
                    {
                        "platform": platform,
                        "problem_id": display_id,
                        "problem_key": _problem_key(platform, problem_id),
                        "name": problem["name"] if problem else None,
                        "url": problem["url"] if problem else None,
                        "reason": disposition["reason"],
                        "notes": disposition["notes"],
                        "note": disposition["notes"],
                        "source": disposition["source"],
                        "context": json.loads(disposition["context_json"] or "{}"),
                        "disposition": disposition["disposition"],
                        "created_at": disposition["created_at"],
                        "updated_at": disposition["updated_at"],
                        "status": "skipped",
                    }
                )
        return {"ok": True, "problems": rows, "count": len(rows)}

    def close(
        self,
        problem: str,
        *,
        result: str,
        minutes: int | None,
        hint_level: int,
        failure: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        load_config(self.paths)
        ref = parse_problem_ref(problem)
        normalized_result = str(result).upper()
        if normalized_result not in RESULTS:
            raise ValueError(f"result 必须是 {', '.join(RESULTS)}")
        if minutes is not None and int(minutes) < 0:
            raise ValueError("minutes 不能为负数")
        hint = int(hint_level)
        if not 0 <= hint <= 4:
            raise ValueError("hint-level 必须在 0..4")
        if failure and failure not in FAILURE_MODES:
            raise ValueError(f"failure 必须是 {', '.join(FAILURE_MODES)}")

        problem_id = _db_problem_id(ref.platform, ref.problem_id)
        today = date.today()
        with Database(self.paths.database) as db:
            with db.atomic():
                attempt_id = self._find_or_start_attempt(db, ref)
                hint = max(hint, db.max_ai_hint_level(attempt_id))
                previous = [
                    row for row in db.attempts(ref.platform, problem_id)
                    if row["id"] != attempt_id
                ]
                previous_wa = sum(str(row["result"] or "").upper() == "WA" for row in previous)
                previous_abandoned = any(
                    str(row["result"] or "").upper() == "ABANDONED" for row in previous
                )
                previous_stage = max((int(row["review_stage"] or 0) for row in previous), default=0)
                previous_due = next(
                    (row["review_due"][:10] for row in previous if row["review_due"]), None
                )
                qualifies = normalized_result == "AC" and (
                    hint >= 2
                    or previous_wa >= 2
                    or previous_abandoned
                    or (failure or "") in {"selection", "modeling", "invariant", "editorial"}
                    or previous_stage > 0
                )
                if qualifies:
                    next_stage = previous_stage + 1
                    review_stage = min(next_stage, 3)
                    delay = {1: 7, 2: 30, 3: 90}.get(next_stage)
                    review_due = (today + timedelta(days=delay)).isoformat() if delay else None
                elif previous_stage > 0 and normalized_result != "AC":
                    review_stage = previous_stage
                    review_due = previous_due or today.isoformat()
                else:
                    review_stage = 0
                    review_due = None
                snapshot_tags = db.effective_problem_tags(ref.platform, problem_id)
                db.close_attempt(
                    attempt_id,
                    result=normalized_result,
                    minutes=minutes,
                    hint_level=hint,
                    failure_mode=None if failure == "none" else failure,
                    notes=notes,
                    review_stage=review_stage,
                    review_due=review_due,
                )
                conversation = db.active_ai_conversation(attempt_id)
                if conversation is not None:
                    db.close_ai_conversation(conversation["id"])
                db.save_attempt_tag_snapshot(
                    attempt_id, snapshot_tags, source="close"
                )
            state = db.problem_status(ref.platform, problem_id)

        candidate = {
            "problem_key": _problem_key(ref.platform, problem_id),
            "problem_id": ref.problem_id,
            "platform": ref.platform,
            "result": normalized_result,
            "minutes": minutes,
            "hint_level": hint,
            "failure_mode": None if failure == "none" else failure,
            "notes": notes,
            "review_stage": review_stage,
            "review_due": review_due,
            "archive_candidate": bool(
                normalized_result == "AC" and (hint >= 2 or failure not in {None, "none"})
            ),
            "tags_snapshot": snapshot_tags,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.paths.reports.mkdir(parents=True, exist_ok=True)
        report = self.paths.reports / f"archive-candidate-{attempt_id}.json"
        report.write_text(
            json.dumps(candidate, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return {
            "ok": True,
            "attempt_id": attempt_id,
            "status": state,
            "review_due": review_due,
            "archive_candidate": str(report),
            "close": candidate,
        }

    def weekly_review(self) -> dict[str, Any]:
        load_config(self.paths)
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        with Database(self.paths.database) as db:
            attempts = [
                dict(row) for row in db.attempts()
                if row["closed_at"] and datetime.fromisoformat(row["closed_at"]) >= cutoff
            ]
            due = self._review_due_rows(db)
            weakness = compute_weakness(self._attempt_rows_with_tags(db))
        results = Counter(str(row["result"] or "unknown") for row in attempts)
        failures = Counter(str(row["failure_mode"] or "none") for row in attempts)
        average_hint = (
            round(sum(int(row["hint_level"] or 0) for row in attempts) / len(attempts), 2)
            if attempts else 0
        )
        payload: dict[str, Any] = {
            "ok": True,
            "window": {"from": cutoff.date().isoformat(), "to": date.today().isoformat()},
            "sessions": len(attempts),
            "results": dict(results),
            "failure_modes": dict(failures),
            "average_hint_level": average_hint,
            "weak_topics": weakness,
            "review_due": due,
        }
        self.paths.reports.mkdir(parents=True, exist_ok=True)
        report = self.paths.reports / f"week-{date.today().isoformat()}.md"
        lines = [
            f"# ACM 周复盘（截至 {date.today().isoformat()}）",
            "",
            f"- 完成 session：{len(attempts)}",
            f"- 平均提示等级：{average_hint}",
            f"- 到期复做：{len(due)}",
            "",
            "## 结果",
            "",
            *(f"- {key}: {value}" for key, value in sorted(results.items())),
            "",
            "## 薄弱专题",
            "",
            *(
                f"- {key}: {value}"
                for key, value in sorted(weakness.items(), key=lambda item: (-item[1], item[0]))
            ),
        ]
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        payload["report"] = str(report)
        return payload

    @staticmethod
    def _review_due_by_key(db: Database) -> dict[str, str]:
        result: dict[str, str] = {}
        seen: set[str] = set()
        for row in db.attempts():
            key = _problem_key(row["platform"], row["problem_id"])
            if key in seen:
                continue
            seen.add(key)
            if row["review_due"]:
                result[key] = row["review_due"][:10]
        return result

    def _review_due_rows(self, db: Database) -> list[dict[str, Any]]:
        today = date.today().isoformat()
        return [
            {
                "problem_id": key.split(":", 1)[1],
                "platform": key.split(":", 1)[0],
                "review_due": due_date,
            }
            for key, due_date in sorted(self._review_due_by_key(db).items())
            if due_date <= today
        ]

    @staticmethod
    def _session_dict(db: Database, row: Any) -> dict[str, Any]:
        payload = dict(row)
        payload["attempt_id"] = int(row["id"])
        payload["problem_id"] = _display_problem_id(row["platform"], row["problem_id"])
        local = db.query(
            """SELECT path FROM local_files
               WHERE platform=? AND problem_id=? ORDER BY updated_at DESC LIMIT 1""",
            (row["platform"], row["problem_id"]),
        )
        payload["source"] = local[0]["path"] if local else None
        return payload

    @staticmethod
    def _find_or_start_attempt(db: Database, ref: ProblemRef) -> int:
        problem_id = _db_problem_id(ref.platform, ref.problem_id)
        active = [row for row in db.attempts(ref.platform, problem_id) if row["active"]]
        return int(active[0]["id"]) if active else db.start_attempt(ref.platform, problem_id)

__all__ = ["ServiceProblemMixin"]
