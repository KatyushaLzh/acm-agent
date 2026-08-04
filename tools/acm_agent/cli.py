from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

# Re-exported dependencies keep existing CLI monkey-patch integrations stable.
from .platforms import CodeforcesClient, LuoguClient, sync_codeforces, sync_luogu
from .service import AcmService, FAILURE_MODES, RESULTS
from .verify import verify_problem


def _service(paths: Any) -> AcmService:
    return AcmService(
        paths.root,
        codeforces_client_factory=CodeforcesClient,
        luogu_client_factory=LuoguClient,
        sync_codeforces_fn=sync_codeforces,
        sync_luogu_fn=sync_luogu,
        verify_fn=verify_problem,
    )


def _emit(payload: Mapping[str, Any] | Sequence[Any], *, as_json: bool, human: str = "") -> None:
    if as_json or not human:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(human)


def _read_account_args(args: argparse.Namespace) -> tuple[str, str, int | None]:
    handle = (args.codeforces or input("Codeforces handle: ")).strip()
    uid = (args.luogu or input("洛谷数字 UID: ")).strip()
    target = args.target_rating
    if target is None and not args.non_interactive:
        raw = input("目标 CF rating（可留空）: ").strip()
        target = int(raw) if raw else None
    return handle, uid, target


def command_init(args: argparse.Namespace, paths: Any) -> int:
    handle, uid, target = _read_account_args(args)
    payload = _service(paths).setup(
        handle,
        uid,
        target_rating=target,
        skip_validate=args.skip_validate,
    )
    _emit(
        payload,
        as_json=args.json,
        human=f"初始化完成；导入 {payload['local_files_imported']} 个本地题目文件。",
    )
    return 0


def command_sync(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).sync(args.platform, force=args.force)
    lines = [
        f"{item['platform']}: {item['status']}，AC {item['accepted']}，新提交 {item['submissions']}"
        for item in payload["results"]
    ]
    _emit(payload, as_json=args.json, human="\n".join(lines))
    return 0 if payload["ok"] else 2


def command_status(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).status()
    lines = ["ACM 状态："]
    lines.extend(f"- {name}: {value}" for name, value in payload["status_counts"].items())
    lines.append(f"- 到期复做: {payload['review_due']}")
    lines.extend(
        f"- {platform} 数据: {source['freshness']}（最近成功 {source['last_success_at'] or '无'}）"
        for platform, source in payload["sources"].items()
    )
    _emit(payload, as_json=args.json, human="\n".join(lines))
    return 0


def _disposition_context(raw: str | None) -> Mapping[str, Any]:
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, Mapping):
        raise ValueError("--context-json 必须是 JSON 对象")
    return value


def command_skip(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).problem_skip(
        args.problem,
        note=args.note or "",
        source="cli",
        context=_disposition_context(args.context_json),
    )
    _emit(
        payload,
        as_json=args.json,
        human=f"已 Skip {payload['problem_id']}（想法已清楚，无需题解）。",
    )
    return 0


def command_unskip(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).problem_unskip(
        args.problem,
        note=args.note or "",
        source="cli",
        context=_disposition_context(args.context_json),
    )
    _emit(
        payload,
        as_json=args.json,
        human=(
            f"已取消 Skip：{payload['problem_id']}"
            if payload["unskipped"] else f"{payload['problem_id']} 当前未被 Skip"
        ),
    )
    return 0


def command_skipped(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).skipped_problems()
    lines = [
        f"{row['problem_id']}  {row.get('name') or ''}  {row.get('notes') or ''}".rstrip()
        for row in payload["problems"]
    ]
    _emit(payload, as_json=args.json, human="\n".join(lines) or "暂无 Skip 题目。")
    return 0


def command_next(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).recommendations(
        count=args.count,
        mode=args.mode,
        source_mode=args.source_mode,
        plan_ids=args.plan_ids or None,
    )
    lines: list[str] = []
    for item in payload["recommendations"]:
        why = "；".join(item["reasons"][:3]) or "题库补充"
        lines.append(
            f"[{item['slot']}] {item['problem_id']}  score={item['score']:.1f}  {why}"
        )
        lines.append(f"  {item['url']}")
    lines.extend(f"注意：{warning}" for warning in payload["warnings"])
    _emit(
        payload,
        as_json=args.json,
        human="\n".join(lines) or "当前没有符合条件的题目。",
    )
    return 0


def command_start(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).start(args.problem, with_stress=args.with_stress)
    human = f"已开始 {payload['problem_id']}：{payload['source']}"
    if payload["reused"]:
        human += "（复用已有文件）"
    _emit(payload, as_json=args.json, human=human)
    return 0


def command_verify(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).verify(
        args.problem,
        exact=args.exact,
        debug=args.debug,
        timeout=args.timeout,
        stress_iterations=args.stress_iterations,
        seed=args.seed,
    )
    human = f"{payload['problem_id']}: " + ("验证通过" if payload["passed"] else "验证失败")
    if payload["sanitizer"] == "unsupported":
        human += "；当前 MinGW 不支持可用的 ASan/UBSan，已明确跳过"
    if payload["failure_dir"]:
        human += f"；对拍资产：{payload['failure_dir']}"
    _emit(payload, as_json=args.json, human=human)
    return 0 if payload["passed"] else 3


def _prompt_if_missing(value: Any, prompt: str, cast=lambda value: value) -> Any:
    return value if value is not None else cast(input(prompt).strip())


def command_close(args: argparse.Namespace, paths: Any) -> int:
    result = str(_prompt_if_missing(args.result, f"结果 {RESULTS}: ")).upper()
    minutes = _prompt_if_missing(args.minutes, "独立思考分钟数: ", int)
    hint = _prompt_if_missing(args.hint_level, "提示等级 0-4: ", int)
    failure = args.failure
    if failure is None and not args.non_interactive:
        failure = input(f"失败类型 {FAILURE_MODES}（可留空）: ").strip() or None
    notes = args.notes
    if notes is None and not args.non_interactive:
        notes = input("简短备注（可留空）: ").strip() or None
    payload = _service(paths).close(
        args.problem,
        result=result,
        minutes=minutes,
        hint_level=hint,
        failure=failure,
        notes=notes,
    )
    human = f"已结束 {payload['close']['problem_id']}：{result}，状态 {payload['status']}"
    if payload["review_due"]:
        human += (
            f"；第 {payload['close']['review_stage']} 阶段复做日期 {payload['review_due']}"
        )
    human += f"；归档候选已保存（未修改知识索引）：{payload['archive_candidate']}"
    _emit(payload, as_json=args.json, human=human)
    return 0


def command_review(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).weekly_review()
    _emit(payload, as_json=args.json, human=f"周复盘已生成：{payload['report']}")
    return 0


def command_plan_check(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).plan_check()
    human = "训练计划校验通过" if payload["ok"] else "训练计划校验失败：\n" + "\n".join(payload["errors"])
    if payload["warnings"]:
        human += "\n警告：\n" + "\n".join(payload["warnings"])
    _emit(payload, as_json=args.json, human=human)
    return 0 if payload["ok"] else 4


def command_plan_list(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).plans()
    lines = [
        f"{'[on]' if row.get('enabled') else '[off]'} {row['plan_id']}  "
        f"rev={row.get('revision', 0)}  {row.get('title', '')}"
        for row in payload.get("plans", [])
    ]
    _emit(payload, as_json=args.json, human="\n".join(lines) or "暂无题单。")
    return 0


def command_plan_import(args: argparse.Namespace, paths: Any) -> int:
    plan = json.loads(args.file.read_text(encoding="utf-8"))
    payload = _service(paths).plan_import(plan=plan, replace=args.replace)
    _emit(
        payload,
        as_json=args.json,
        human=f"已导入题单 {payload['plan_id']}，修订 {payload['revision']}。",
    )
    return 0


def command_plan_export(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).plan_detail(args.plan_id)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload["plan"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        payload = {**payload, "output": str(args.output.resolve())}
    _emit(
        payload,
        as_json=args.json,
        human=(f"已导出到 {payload['output']}" if args.output else json.dumps(payload["plan"], ensure_ascii=False, indent=2)),
    )
    return 0


def command_plan_state(args: argparse.Namespace, paths: Any) -> int:
    service = _service(paths)
    current = service.plan_detail(args.plan_id)
    payload = service.plan_state(
        args.plan_id,
        enabled=args.enabled,
        expected_revision=current["revision"],
    )
    _emit(
        payload,
        as_json=args.json,
        human=f"题单 {args.plan_id} 已{'启用' if args.enabled else '停用'}。",
    )
    return 0


def command_plan_delete(args: argparse.Namespace, paths: Any) -> int:
    service = _service(paths)
    current = service.plan_detail(args.plan_id)
    payload = service.plan_delete(args.plan_id, expected_revision=current["revision"])
    _emit(payload, as_json=args.json, human=f"题单 {args.plan_id} 已移除；训练历史已保留。")
    return 0


def command_plan_template(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).plan_template()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload["plan"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        payload = {**payload, "output": str(args.output.resolve())}
    _emit(
        payload,
        as_json=args.json,
        human=(f"题单模板已写入 {payload['output']}" if args.output else json.dumps(payload["plan"], ensure_ascii=False, indent=2)),
    )
    return 0


def command_plan_tags_preview(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).plan_tags_preview(
        args.plan_id,
        overwrite=args.overwrite,
        refresh=not args.no_refresh,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        payload = {**payload, "output": str(args.output.resolve())}
    coverage = payload["coverage"]
    human = (
        f"题单 {args.plan_id} 标签预览：{coverage['suggested']}/"
        f"{coverage['eligible']} 项有建议"
    )
    if args.output:
        human += f"；已写入 {payload['output']}"
    _emit(payload, as_json=args.json, human=human)
    return 0


def _tag_apply_input(path: Path) -> tuple[list[Any], int | None]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(value, list):
        return value, None
    if not isinstance(value, Mapping):
        raise ValueError("标签建议文件必须是 proposals 数组或预览结果对象")
    # Accept direct CLI output and a persisted web job record.
    candidate: Any = value
    if isinstance(candidate.get("data"), Mapping):
        candidate = candidate["data"]
    if isinstance(candidate.get("result"), Mapping):
        candidate = candidate["result"]
    proposals = candidate.get("proposals")
    if not isinstance(proposals, list):
        raise ValueError("标签建议文件缺少 proposals 数组")
    base_revision = candidate.get("base_revision")
    return proposals, int(base_revision) if base_revision is not None else None


def command_plan_tags_apply(args: argparse.Namespace, paths: Any) -> int:
    proposals, file_revision = _tag_apply_input(args.file)
    service = _service(paths)
    expected = args.expected_revision if args.expected_revision is not None else file_revision
    if expected is None:
        raise ValueError(
            "标签建议文件缺少 base_revision；请使用 preview 输出或提供 --expected-revision"
        )
    payload = service.plan_tags_apply(
        args.plan_id,
        expected_revision=expected,
        proposals=proposals,
    )
    _emit(
        payload,
        as_json=args.json,
        human=(
            f"题单 {args.plan_id} 已补全 {payload['updated']} 项标签，"
            f"跳过 {payload['skipped']} 项；当前修订 {payload['revision']}。"
        ),
    )
    return 0


def command_web(args: argparse.Namespace, paths: Any) -> int:
    # Delayed import keeps the CLI usable in stripped/headless environments and
    # avoids loading the HTTP server for ordinary commands.
    from .web import serve

    serve(
        paths.root,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="acm", description="ACM Agent 自动化学习系统")
    parser.add_argument("--root", type=Path, default=None, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="配置平台账号并导入本地代码")
    init.add_argument("--codeforces")
    init.add_argument("--luogu")
    init.add_argument("--target-rating", type=int)
    init.add_argument("--skip-validate", action="store_true")
    init.add_argument("--non-interactive", action="store_true", help=argparse.SUPPRESS)
    init.add_argument("--json", action="store_true")
    init.set_defaults(handler=command_init)

    sync = sub.add_parser("sync", help="同步 Codeforces/洛谷状态")
    sync.add_argument("--platform", choices=("codeforces", "luogu", "all"), default="all")
    sync.add_argument("--force", action="store_true")
    sync.add_argument("--json", action="store_true")
    sync.set_defaults(handler=command_sync)

    status = sub.add_parser("status", help="查看统一状态")
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=command_status)

    skip = sub.add_parser("skip", help="标记想法已清楚、无需题解的题目")
    skip.add_argument("problem")
    skip.add_argument("--note", "--notes", dest="note")
    skip.add_argument("--context-json", help="附加 JSON 对象审计上下文")
    skip.add_argument("--json", action="store_true")
    skip.set_defaults(handler=command_skip)

    unskip = sub.add_parser("unskip", help="取消题目的 Skip 标记")
    unskip.add_argument("problem")
    unskip.add_argument("--note", "--notes", dest="note")
    unskip.add_argument("--context-json", help="附加 JSON 对象审计上下文")
    unskip.add_argument("--json", action="store_true")
    unskip.set_defaults(handler=command_unskip)

    skipped = sub.add_parser("skipped", help="列出全局 Skip 题目")
    skipped.add_argument("--json", action="store_true")
    skipped.set_defaults(handler=command_skipped)

    nxt = sub.add_parser("next", help="给出可解释推荐")
    nxt.add_argument("--count", type=int, default=3)
    nxt.add_argument("--mode", choices=("mixed", "new", "review"), default="mixed")
    nxt.add_argument(
        "--source-mode",
        choices=("balanced", "catalog_only", "plan_only"),
        default="balanced",
    )
    nxt.add_argument("--plan", dest="plan_ids", action="append", help="限定已启用题单，可重复")
    nxt.add_argument("--json", action="store_true")
    nxt.set_defaults(handler=command_next)

    start = sub.add_parser("start", help="创建/复用今日代码并开启 session")
    start.add_argument("problem")
    start.add_argument("--with-stress", action="store_true")
    start.add_argument("--json", action="store_true")
    start.set_defaults(handler=command_start)

    verify = sub.add_parser("verify", help="编译、样例验证和可选对拍")
    verify.add_argument("problem", nargs="?")
    verify.add_argument("--debug", action="store_true")
    verify.add_argument("--exact", action="store_true")
    verify.add_argument("--timeout", type=float, default=2.0)
    verify.add_argument("--stress-iterations", type=int, default=100)
    verify.add_argument("--seed", type=int)
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(handler=command_verify)

    close = sub.add_parser("close", help="结束 session 并记录复盘")
    close.add_argument("problem")
    close.add_argument("--result", type=str.upper, choices=RESULTS)
    close.add_argument("--minutes", type=int)
    close.add_argument("--hint-level", type=int)
    close.add_argument("--failure", choices=FAILURE_MODES)
    close.add_argument("--notes")
    close.add_argument("--non-interactive", action="store_true", help=argparse.SUPPRESS)
    close.add_argument("--json", action="store_true")
    close.set_defaults(handler=command_close)

    review = sub.add_parser("review", help="生成复盘报告")
    review_sub = review.add_subparsers(dest="period", required=True)
    week = review_sub.add_parser("week")
    week.add_argument("--json", action="store_true")
    week.set_defaults(handler=command_review)

    plan = sub.add_parser("plan", help="训练计划工具")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True)
    check = plan_sub.add_parser("check")
    check.add_argument("--json", action="store_true")
    check.set_defaults(handler=command_plan_check)

    plan_list = plan_sub.add_parser("list", help="列出题单")
    plan_list.add_argument("--json", action="store_true")
    plan_list.set_defaults(handler=command_plan_list)

    plan_import = plan_sub.add_parser("import", help="从 JSON 文件导入题单")
    plan_import.add_argument("file", type=Path)
    plan_import.add_argument("--replace", action="store_true", help="显式替换同 plan_id 题单")
    plan_import.add_argument("--json", action="store_true")
    plan_import.set_defaults(handler=command_plan_import)

    plan_export = plan_sub.add_parser("export", help="导出题单 JSON")
    plan_export.add_argument("plan_id")
    plan_export.add_argument("--output", type=Path)
    plan_export.add_argument("--json", action="store_true")
    plan_export.set_defaults(handler=command_plan_export)

    for name, enabled in (("enable", True), ("disable", False)):
        state_parser = plan_sub.add_parser(name, help=f"{'启用' if enabled else '停用'}题单")
        state_parser.add_argument("plan_id")
        state_parser.add_argument("--json", action="store_true")
        state_parser.set_defaults(handler=command_plan_state, enabled=enabled)

    plan_delete = plan_sub.add_parser("delete", help="移除题单但保留训练历史")
    plan_delete.add_argument("plan_id")
    plan_delete.add_argument("--json", action="store_true")
    plan_delete.set_defaults(handler=command_plan_delete)

    plan_template = plan_sub.add_parser("template", help="输出 plan.json v2 模板")
    plan_template.add_argument("--output", type=Path)
    plan_template.add_argument("--json", action="store_true")
    plan_template.set_defaults(handler=command_plan_template)

    plan_tags = plan_sub.add_parser("tags", help="预览或应用题单平台标签")
    tags_sub = plan_tags.add_subparsers(dest="plan_tags_command", required=True)

    tags_preview = tags_sub.add_parser("preview", help="从平台公开数据生成标签建议")
    tags_preview.add_argument("plan_id")
    tags_preview.add_argument("--overwrite", action="store_true", help="也为已有标签题生成建议")
    tags_preview.add_argument("--no-refresh", action="store_true", help="CF 本地目录缺失时不刷新官方题库")
    tags_preview.add_argument("--output", type=Path, help="保存可供 apply 使用的 JSON")
    tags_preview.add_argument("--json", action="store_true")
    tags_preview.set_defaults(handler=command_plan_tags_preview)

    tags_apply = tags_sub.add_parser("apply", help="从 JSON 建议文件原子写回标签")
    tags_apply.add_argument("plan_id")
    tags_apply.add_argument("file", type=Path)
    tags_apply.add_argument("--expected-revision", type=int)
    tags_apply.add_argument("--json", action="store_true")
    tags_apply.set_defaults(handler=command_plan_tags_apply)

    web = sub.add_parser("web", help="启动本地网页仪表盘")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--no-browser", action="store_true")
    web.set_defaults(handler=command_web)
    return parser


def main(argv: Sequence[str] | None = None, *, root: str | Path | None = None) -> int:
    from .config import Paths

    parser = build_parser()
    args = parser.parse_args(argv)
    chosen_root = Path(root) if root is not None else (args.root or Path.cwd())
    paths = Paths.for_root(chosen_root)
    try:
        return int(args.handler(args, paths))
    except (FileNotFoundError, KeyError, ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
