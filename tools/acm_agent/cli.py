from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any, Mapping, Sequence

# Re-exported dependencies keep existing CLI monkey-patch integrations stable.
from .platforms import CodeforcesClient, LuoguClient, sync_codeforces, sync_luogu
from .service import AcmService, FAILURE_MODES, RESULTS
from .verify import verify_problem


def _stress_prepare_timeout(value: str) -> int:
    seconds = int(value)
    if not 60 <= seconds <= 1800:
        raise argparse.ArgumentTypeError("准备耗时上限必须在 60..1800 秒之间")
    return seconds


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
    method = _service(paths).ai_recommendations if args.ai else _service(paths).recommendations
    kwargs = {
        "count": args.count,
        "mode": args.mode,
        "source_mode": args.source_mode,
        "plan_ids": args.plan_ids or None,
    }
    if args.ai:
        kwargs["model"] = args.model
    payload = method(**kwargs)
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


def command_ai_status(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).ai_status()
    detected = "已检测" if payload["api_key_detected"] else "未检测"
    _emit(payload, as_json=args.json, human=f"DeepSeek API Key：{detected}")
    return 0


def command_ai_test(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).ai_test(model=args.model)
    _emit(payload, as_json=args.json, human=f"DeepSeek 连接成功：{payload['model']}")
    return 0


def command_ai_settings(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).ai_settings(
        recommendation_model=args.recommend_model,
        coaching_model=args.coach_model,
        summary_model=args.summary_model,
        validation_model=args.validation_model,
        coaching_thinking=args.thinking,
        reasoning_effort=args.reasoning_effort,
        summary_thinking=args.summary_thinking,
        summary_reasoning_effort=args.summary_reasoning_effort,
        validation_thinking=args.validation_thinking,
        validation_reasoning_effort=args.validation_reasoning_effort,
    )
    _emit(payload, as_json=args.json, human="AI 设置已保存（API Key 未写入配置）。")
    return 0


def command_context_fetch(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).problem_context_fetch(args.problem, force=args.force)
    _emit(payload, as_json=args.json, human=f"题面已缓存：{payload['problem_id']}（{payload['source']}）")
    return 0


def command_context_show(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).problem_context(args.problem)
    _emit(payload, as_json=args.json, human=payload.get("content") or "暂无题面缓存。")
    return 0


def command_context_set(args: argparse.Namespace, paths: Any) -> int:
    content = args.file.read_text(encoding="utf-8")
    payload = _service(paths).problem_context_save(
        args.problem,
        content=content,
        expected_hash=args.expected_hash,
    )
    _emit(payload, as_json=args.json, human=f"人工题面已保存：{payload['problem_id']}")
    return 0


def command_ask(args: argparse.Namespace, paths: Any) -> int:
    message = args.message or input("问题: ").strip()
    payload = _service(paths).ai_chat(
        args.problem,
        message=message,
        mode=args.mode,
        hint_level=args.hint_level,
        model=args.model,
        conversation_id=args.conversation,
    )
    _emit(payload, as_json=args.json, human=payload.get("content") or "")
    return 0


def command_patch_preview(args: argparse.Namespace, paths: Any) -> int:
    instruction = args.instruction or input("修复要求: ").strip()
    payload = _service(paths).ai_patch_preview(
        args.problem,
        instruction=instruction,
        model=args.model,
        conversation_id=args.conversation,
    )
    _emit(payload, as_json=args.json, human=payload.get("diff") or "")
    return 0


def command_patch_apply(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).ai_patch_apply(args.proposal_id)
    _emit(payload, as_json=args.json, human=f"补丁已应用并验证：{payload['proposal_id']}")
    return 0 if payload.get("verify", {}).get("passed") else 3


def command_patch_revert(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).ai_patch_revert(args.proposal_id)
    _emit(payload, as_json=args.json, human=f"已恢复 AI 补丁：{payload['proposal_id']}")
    return 0


def _schema_file(path: Path | None) -> Mapping[str, Any] | None:
    if path is None:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("schema 文件必须是 JSON 对象")
    return value


def command_knowledge_templates(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).knowledge_templates()
    rows = payload.get("templates", []) if isinstance(payload, Mapping) else payload
    lines = [
        f"{row.get('preset') or row.get('id')}  {row.get('name') or row.get('title') or ''}".rstrip()
        for row in rows
    ]
    _emit(payload, as_json=args.json, human="\n".join(lines) or "暂无 Markdown 模板。")
    return 0


def command_knowledge_targets_list(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).knowledge_targets()
    rows = payload.get("targets", []) if isinstance(payload, Mapping) else payload
    lines = [
        f"{row.get('target_id') or row.get('id')}  {row.get('name') or row.get('display_name') or ''}  {row.get('path') or ''}".rstrip()
        for row in rows
    ]
    _emit(payload, as_json=args.json, human="\n".join(lines) or "尚未注册 Markdown 目标。")
    return 0


def command_knowledge_target_add(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).knowledge_target_create(
        path=str(args.path),
        name=args.name,
        preset=args.preset,
        schema_mode=args.schema_mode,
        schema=_schema_file(args.schema_file),
        allow_create=args.allow_create,
    )
    _emit(
        payload,
        as_json=args.json,
        human=f"Markdown 目标已保存：{payload.get('name') or payload.get('target_id')}（未写入文件）",
    )
    return 0


def command_knowledge_target_update(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).knowledge_target_update(
        args.target_id,
        name=args.name,
        preset=args.preset,
        schema_mode=args.schema_mode,
        schema=_schema_file(args.schema_file),
        enabled=args.enabled,
        expected_revision=args.expected_revision,
    )
    _emit(payload, as_json=args.json, human=f"Markdown 目标已更新：{args.target_id}")
    return 0


def command_knowledge_target_remove(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).knowledge_target_delete(
        args.target_id,
        expected_revision=args.expected_revision,
    )
    _emit(payload, as_json=args.json, human=f"已取消注册 {args.target_id}；Markdown 文件未删除。")
    return 0


def command_knowledge_inspect(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).knowledge_target_inspect(
        path=str(args.path),
        allow_create=args.allow_create,
        preset=args.preset,
        schema_mode=args.schema_mode,
        schema=_schema_file(args.schema_file),
    )
    _emit(
        payload,
        as_json=args.json,
        human=(
            f"路径检查通过：{payload.get('normalized_path') or payload.get('path')}\n"
            f"Schema：{payload.get('schema_source') or payload.get('preset') or '已推断'}"
        ),
    )
    return 0


def command_knowledge_preview(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).knowledge_preview(
        attempt_id=args.attempt_id,
        target_id=args.target_id,
        schema_mode=args.schema_mode,
        preset=args.preset,
        schema=_schema_file(args.schema_file),
        model=args.model,
    )
    proposal = payload.get("proposal") if isinstance(payload.get("proposal"), dict) else payload
    _emit(payload, as_json=args.json, human=proposal.get("entry_markdown") or "预览已生成；目标文件尚未修改。")
    return 0


def command_knowledge_refresh(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).knowledge_refresh(
        args.proposal_id,
        entry_markdown=args.entry_file.read_text(encoding="utf-8"),
        expected_revision=args.expected_revision,
    )
    proposal = payload.get("proposal") if isinstance(payload.get("proposal"), dict) else payload
    _emit(payload, as_json=args.json, human=proposal.get("entry_markdown") or "预览已刷新。")
    return 0


def command_knowledge_apply(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).knowledge_apply(
        args.proposal_id,
        expected_revision=args.expected_revision,
    )
    _emit(payload, as_json=args.json, human=f"Markdown 总结已写入：{args.proposal_id}")
    return 0


def command_knowledge_revert(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).knowledge_revert(
        args.proposal_id,
        expected_revision=args.expected_revision,
    )
    _emit(payload, as_json=args.json, human=f"Markdown 写入已回退：{args.proposal_id}")
    return 0


def command_start(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).start(args.problem, with_stress=args.with_stress)
    human = f"已开始 {payload['problem_id']}：{payload['source']}"
    if payload["reused"]:
        human += "（复用已有文件）"
    _emit(payload, as_json=args.json, human=human)
    return 0


def _wait_for_stress_cli(
    service: AcmService,
    run_id: str,
    initial: Mapping[str, Any],
    *,
    as_json: bool,
) -> int:
    terminal = {
        "stopped", "mismatch", "oracle_conflict", "fault",
        "interrupted", "completed",
    }
    try:
        while True:
            current = service.stress_run(run_id)["run"]
            if str(current.get("status") or "").casefold() in terminal:
                break
            time.sleep(0.25)
    except KeyboardInterrupt:
        service.stress_stop(run_id)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            current = service.stress_run(run_id)["run"]
            if str(current.get("status") or "").casefold() in terminal:
                break
            time.sleep(0.1)
        else:
            service.shutdown()
            current = service.stress_run(run_id)["run"]
    final_payload = {**dict(initial), "run": current}
    _emit(
        final_payload,
        as_json=as_json,
        human=f"AI 持续对拍结束：{run_id} · {current.get('status', 'unknown')}",
    )
    return 3 if current.get("status") in {"mismatch", "oracle_conflict", "fault"} else 0


def command_verify(args: argparse.Namespace, paths: Any) -> int:
    if args.ai_stress:
        service = _service(paths)
        payload = service.ai_stress_start(
            args.problem,
            generate_generator=True,
            generate_brute=True,
            prepare_reference=True,
            large_profile=not args.no_large,
            compare="exact" if args.exact else "token",
            timeout=args.timeout,
            brute_timeout=5.0,
            seed=args.seed,
            preparation_timeout_seconds=args.prepare_timeout,
            force_regenerate=args.force_regenerate,
            cache_mode=args.cache_mode,
            generation_mode=(
                args.generation_mode.replace("-", "_")
                if args.generation_mode is not None
                else None
            ),
        )
        run = payload.get("run", payload)
        run_id = run.get("id") or run.get("run_id") or payload.get("run_id") or "unknown"
        if not args.json:
            print(f"AI 持续对拍已启动：{run_id}（Ctrl+C 安全停止）")
        return _wait_for_stress_cli(service, run_id, payload, as_json=args.json)
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


def command_stress_status(args: argparse.Namespace, paths: Any) -> int:
    service = _service(paths)
    payload = service.stress_run(args.run_id) if args.run_id else service.stress_runs()
    _emit(payload, as_json=args.json, human=json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def command_stress_stop(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).stress_stop(args.run_id)
    _emit(payload, as_json=args.json, human=f"已请求停止持续对拍：{args.run_id}")
    return 0


def command_stress_resume(args: argparse.Namespace, paths: Any) -> int:
    service = _service(paths)
    payload = service.stress_resume(args.run_id)
    if not args.json:
        print(f"已从保存的 seed 继续持续对拍：{args.run_id}（Ctrl+C 安全停止）")
    return _wait_for_stress_cli(service, args.run_id, payload, as_json=args.json)


def command_stress_artifacts(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).stress_bundle(args.bundle_id)
    _emit(payload, as_json=args.json, human=json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def command_stress_revert(args: argparse.Namespace, paths: Any) -> int:
    payload = _service(paths).stress_bundle_revert(args.bundle_id)
    _emit(payload, as_json=args.json, human=f"AI 对拍 helper 已回退：{args.bundle_id}")
    return 0


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
    mode = getattr(args, "mode", "fill_missing")
    payload = _service(paths).plan_tags_preview(
        args.plan_id,
        mode=mode,
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
    action = "清理" if mode == "cleanup" else "补全"
    human = (
        f"题单 {args.plan_id} 标签{action}预览：{coverage['suggested']}/"
        f"{coverage['eligible']} 项有建议"
    )
    if args.output:
        human += f"；已写入 {payload['output']}"
    _emit(payload, as_json=args.json, human=human)
    return 0


def _tag_apply_input(path: Path) -> tuple[list[Any], int | None, int | None]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(value, list):
        return value, None, None
    if not isinstance(value, Mapping):
        raise ValueError("标签建议文件必须是 proposals 数组或预览结果对象")
    # Accept direct CLI output and a persisted web job record.
    candidate: Any = value
    revisions: list[Mapping[str, Any]] = [candidate]
    for key in ("data", "result", "preview"):
        nested = candidate.get(key)
        if isinstance(nested, Mapping):
            candidate = nested
            revisions.append(candidate)
    proposals = candidate.get("proposals")
    if not isinstance(proposals, list):
        raise ValueError("标签建议文件缺少 proposals 数组")
    base_revision = next(
        (item.get("base_revision") for item in reversed(revisions) if item.get("base_revision") is not None),
        None,
    )
    override_revision = next(
        (item.get("override_revision") for item in reversed(revisions) if item.get("override_revision") is not None),
        None,
    )
    return (
        proposals,
        int(base_revision) if base_revision is not None else None,
        int(override_revision) if override_revision is not None else None,
    )


def command_plan_tags_apply(args: argparse.Namespace, paths: Any) -> int:
    proposals, file_revision, file_override_revision = _tag_apply_input(args.file)
    service = _service(paths)
    expected = args.expected_revision if args.expected_revision is not None else file_revision
    expected_override = (
        getattr(args, "expected_override_revision", None)
        if getattr(args, "expected_override_revision", None) is not None
        else file_override_revision
    )
    if expected is None:
        raise ValueError(
            "标签建议文件缺少 base_revision；请使用 preview 输出或提供 --expected-revision"
        )
    payload = service.plan_tags_apply(
        args.plan_id,
        expected_revision=expected,
        proposals=proposals,
        expected_override_revision=expected_override,
    )
    _emit(
        payload,
        as_json=args.json,
        human=(
            f"题单 {args.plan_id} 已更新 {payload['updated']} 项标签，"
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
    nxt.add_argument("--ai", action="store_true", help="显式使用 DeepSeek 对确定性候选重排")
    nxt.add_argument("--model", choices=("deepseek-v4-flash", "deepseek-v4-pro"))
    nxt.add_argument("--json", action="store_true")
    nxt.set_defaults(handler=command_next)

    ai = sub.add_parser("ai", help="DeepSeek BYOK 设置与连通性")
    ai_sub = ai.add_subparsers(dest="ai_command", required=True)
    ai_status = ai_sub.add_parser("status")
    ai_status.add_argument("--json", action="store_true")
    ai_status.set_defaults(handler=command_ai_status)
    ai_test = ai_sub.add_parser("test")
    ai_test.add_argument("--model", choices=("deepseek-v4-flash", "deepseek-v4-pro"))
    ai_test.add_argument("--json", action="store_true")
    ai_test.set_defaults(handler=command_ai_test)
    ai_settings = ai_sub.add_parser("settings")
    ai_settings.add_argument("--recommend-model", choices=("deepseek-v4-flash", "deepseek-v4-pro"))
    ai_settings.add_argument("--coach-model", choices=("deepseek-v4-flash", "deepseek-v4-pro"))
    ai_settings.add_argument("--summary-model", choices=("deepseek-v4-flash", "deepseek-v4-pro"))
    ai_settings.add_argument("--validation-model", choices=("deepseek-v4-flash", "deepseek-v4-pro"))
    ai_settings.add_argument("--thinking", action=argparse.BooleanOptionalAction, default=None)
    ai_settings.add_argument("--reasoning-effort", choices=("high", "max"))
    ai_settings.add_argument("--summary-thinking", action=argparse.BooleanOptionalAction, default=None)
    ai_settings.add_argument("--summary-reasoning-effort", choices=("high", "max"))
    ai_settings.add_argument("--validation-thinking", action=argparse.BooleanOptionalAction, default=None)
    ai_settings.add_argument("--validation-reasoning-effort", choices=("high", "max"))
    ai_settings.add_argument("--json", action="store_true")
    ai_settings.set_defaults(handler=command_ai_settings)

    context = sub.add_parser("context", help="管理 AI 使用的公开题面上下文")
    context_sub = context.add_subparsers(dest="context_command", required=True)
    context_fetch = context_sub.add_parser("fetch")
    context_fetch.add_argument("problem")
    context_fetch.add_argument("--force", action="store_true")
    context_fetch.add_argument("--json", action="store_true")
    context_fetch.set_defaults(handler=command_context_fetch)
    context_show = context_sub.add_parser("show")
    context_show.add_argument("problem")
    context_show.add_argument("--json", action="store_true")
    context_show.set_defaults(handler=command_context_show)
    context_set = context_sub.add_parser("set")
    context_set.add_argument("problem")
    context_set.add_argument("--file", type=Path, required=True)
    context_set.add_argument("--expected-hash")
    context_set.add_argument("--json", action="store_true")
    context_set.set_defaults(handler=command_context_set)

    ask = sub.add_parser("ask", help="在当前做题 session 中询问 DeepSeek")
    ask.add_argument("problem")
    ask.add_argument("message", nargs="?")
    ask.add_argument("--mode", choices=("hint", "explain", "review"), default="hint")
    ask.add_argument("--hint-level", type=int, default=1)
    ask.add_argument("--model", choices=("deepseek-v4-flash", "deepseek-v4-pro"))
    ask.add_argument("--conversation")
    ask.add_argument("--json", action="store_true")
    ask.set_defaults(handler=command_ask)

    patch = sub.add_parser("patch", help="预览、应用或回退 AI 代码补丁")
    patch_sub = patch.add_subparsers(dest="patch_command", required=True)
    patch_preview = patch_sub.add_parser("preview")
    patch_preview.add_argument("problem")
    patch_preview.add_argument("instruction", nargs="?")
    patch_preview.add_argument("--model", choices=("deepseek-v4-flash", "deepseek-v4-pro"))
    patch_preview.add_argument("--conversation")
    patch_preview.add_argument("--json", action="store_true")
    patch_preview.set_defaults(handler=command_patch_preview)
    patch_apply = patch_sub.add_parser("apply")
    patch_apply.add_argument("proposal_id")
    patch_apply.add_argument("--json", action="store_true")
    patch_apply.set_defaults(handler=command_patch_apply)
    patch_revert = patch_sub.add_parser("revert")
    patch_revert.add_argument("proposal_id")
    patch_revert.add_argument("--json", action="store_true")
    patch_revert.set_defaults(handler=command_patch_revert)

    knowledge = sub.add_parser("knowledge", help="预览并确认写入 Markdown 知识总结")
    knowledge_sub = knowledge.add_subparsers(dest="knowledge_command", required=True)
    knowledge_templates = knowledge_sub.add_parser("templates", help="列出内置脱敏模板")
    knowledge_templates.add_argument("--json", action="store_true")
    knowledge_templates.set_defaults(handler=command_knowledge_templates)

    knowledge_targets = knowledge_sub.add_parser("targets", help="管理本机 Markdown 目标")
    knowledge_targets.add_argument("--json", action="store_true")
    knowledge_targets.set_defaults(handler=command_knowledge_targets_list)
    knowledge_targets_sub = knowledge_targets.add_subparsers(dest="knowledge_targets_command")
    knowledge_targets_list = knowledge_targets_sub.add_parser("list")
    knowledge_targets_list.add_argument("--json", action="store_true")
    knowledge_targets_list.set_defaults(handler=command_knowledge_targets_list)
    knowledge_target_add = knowledge_targets_sub.add_parser("add")
    knowledge_target_add.add_argument("path", type=Path)
    knowledge_target_add.add_argument("--name")
    knowledge_target_add.add_argument("--preset", choices=("algorithms-v1", "tricks-v1", "custom"))
    knowledge_target_add.add_argument("--schema-mode", choices=("auto", "stored", "ai"), default="auto")
    knowledge_target_add.add_argument("--schema-file", type=Path)
    knowledge_target_add.add_argument("--allow-create", action="store_true", help="允许注册尚不存在的 .md；此命令不会创建文件")
    knowledge_target_add.add_argument("--json", action="store_true")
    knowledge_target_add.set_defaults(handler=command_knowledge_target_add)
    knowledge_target_update = knowledge_targets_sub.add_parser("update")
    knowledge_target_update.add_argument("target_id")
    knowledge_target_update.add_argument("--name")
    knowledge_target_update.add_argument("--preset", choices=("algorithms-v1", "tricks-v1", "custom"))
    knowledge_target_update.add_argument("--schema-mode", choices=("auto", "stored", "ai"))
    knowledge_target_update.add_argument("--schema-file", type=Path)
    knowledge_target_update.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=None)
    knowledge_target_update.add_argument("--expected-revision", type=int, required=True)
    knowledge_target_update.add_argument("--json", action="store_true")
    knowledge_target_update.set_defaults(handler=command_knowledge_target_update)
    knowledge_target_remove = knowledge_targets_sub.add_parser("remove")
    knowledge_target_remove.add_argument("target_id")
    knowledge_target_remove.add_argument("--expected-revision", type=int, required=True)
    knowledge_target_remove.add_argument("--json", action="store_true")
    knowledge_target_remove.set_defaults(handler=command_knowledge_target_remove)

    knowledge_inspect = knowledge_sub.add_parser("inspect", help="只读检查 Markdown 路径与 schema")
    knowledge_inspect.add_argument("path", type=Path)
    knowledge_inspect.add_argument("--allow-create", action="store_true")
    knowledge_inspect.add_argument("--preset", choices=("algorithms-v1", "tricks-v1", "custom"))
    knowledge_inspect.add_argument("--schema-mode", choices=("auto", "stored", "ai"), default="auto")
    knowledge_inspect.add_argument("--schema-file", type=Path)
    knowledge_inspect.add_argument("--json", action="store_true")
    knowledge_inspect.set_defaults(handler=command_knowledge_inspect)

    knowledge_preview = knowledge_sub.add_parser("preview", help="调用 DeepSeek 生成持久化预览，不写文件")
    knowledge_preview.add_argument("attempt_id", type=int)
    knowledge_preview.add_argument("target_id")
    knowledge_preview.add_argument("--schema-mode", choices=("auto", "stored", "ai"), default="stored")
    knowledge_preview.add_argument("--preset", choices=("algorithms-v1", "tricks-v1", "custom"))
    knowledge_preview.add_argument("--schema-file", type=Path)
    knowledge_preview.add_argument("--model", choices=("deepseek-v4-flash", "deepseek-v4-pro"))
    knowledge_preview.add_argument("--json", action="store_true")
    knowledge_preview.set_defaults(handler=command_knowledge_preview)
    knowledge_refresh = knowledge_sub.add_parser("refresh", help="用编辑后的条目刷新安全预览，不调用模型")
    knowledge_refresh.add_argument("proposal_id")
    knowledge_refresh.add_argument("--entry-file", type=Path, required=True)
    knowledge_refresh.add_argument("--expected-revision", type=int, required=True)
    knowledge_refresh.add_argument("--json", action="store_true")
    knowledge_refresh.set_defaults(handler=command_knowledge_refresh)
    knowledge_apply = knowledge_sub.add_parser("apply", help="确认写入最新预览")
    knowledge_apply.add_argument("proposal_id")
    knowledge_apply.add_argument("--expected-revision", type=int, required=True)
    knowledge_apply.add_argument("--json", action="store_true")
    knowledge_apply.set_defaults(handler=command_knowledge_apply)
    knowledge_revert = knowledge_sub.add_parser("revert", help="在 hash 校验后回退写入")
    knowledge_revert.add_argument("proposal_id")
    knowledge_revert.add_argument("--expected-revision", type=int, required=True)
    knowledge_revert.add_argument("--json", action="store_true")
    knowledge_revert.set_defaults(handler=command_knowledge_revert)

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
    verify.add_argument(
        "--ai-stress",
        action="store_true",
        help="显式调用 DeepSeek 准备 helper，并启动持续分层对拍",
    )
    verify.add_argument(
        "--no-large",
        action="store_true",
        help="AI 持续对拍只运行可人工验证的小数据，不运行极限大数据",
    )
    verify.add_argument(
        "--prepare-timeout",
        type=_stress_prepare_timeout,
        metavar="SECONDS",
        help="AI helper 准备总耗时上限（60..1800 秒，默认读取配置）",
    )
    verify.add_argument(
        "--force-regenerate",
        action="store_true",
        help="兼容选项：等价于 --cache-mode cold",
    )
    verify.add_argument(
        "--cache-mode",
        choices=("reuse", "refresh_helpers", "cold"),
        help="准备缓存策略（默认 reuse；cold 绕过所有本地读取）",
    )
    verify.add_argument(
        "--generation-mode",
        choices=("fast", "hybrid", "full-thinking"),
        help="helper 生成策略（默认读取配置；full-thinking 映射为 full_thinking）",
    )
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(handler=command_verify)

    stress = sub.add_parser("stress", help="查看和控制 AI 持续对拍")
    stress_sub = stress.add_subparsers(dest="stress_command", required=True)
    stress_status = stress_sub.add_parser("status", help="查看全部或指定持续对拍")
    stress_status.add_argument("run_id", nargs="?")
    stress_status.add_argument("--json", action="store_true")
    stress_status.set_defaults(handler=command_stress_status)
    stress_stop = stress_sub.add_parser("stop", help="停止持续对拍")
    stress_stop.add_argument("run_id")
    stress_stop.add_argument("--json", action="store_true")
    stress_stop.set_defaults(handler=command_stress_stop)
    stress_resume = stress_sub.add_parser("resume", help="从保存的 seed 继续对拍")
    stress_resume.add_argument("run_id")
    stress_resume.add_argument("--json", action="store_true")
    stress_resume.set_defaults(handler=command_stress_resume)
    stress_artifacts = stress_sub.add_parser("artifacts", help="查看 helper 与来源")
    stress_artifacts.add_argument("bundle_id")
    stress_artifacts.add_argument("--json", action="store_true")
    stress_artifacts.set_defaults(handler=command_stress_artifacts)
    stress_revert = stress_sub.add_parser("revert", help="按 hash 校验回退 helper bundle")
    stress_revert.add_argument("bundle_id")
    stress_revert.add_argument("--json", action="store_true")
    stress_revert.set_defaults(handler=command_stress_revert)

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
    tags_preview.add_argument(
        "--mode",
        choices=("fill_missing", "cleanup"),
        default="fill_missing",
        help="补全缺失标签（默认）或清理无关元标签",
    )
    tags_preview.add_argument("--overwrite", action="store_true", help="也为已有标签题生成建议")
    tags_preview.add_argument("--no-refresh", action="store_true", help="CF 本地目录缺失时不刷新官方题库")
    tags_preview.add_argument("--output", type=Path, help="保存可供 apply 使用的 JSON")
    tags_preview.add_argument("--json", action="store_true")
    tags_preview.set_defaults(handler=command_plan_tags_preview)

    tags_apply = tags_sub.add_parser("apply", help="从 JSON 建议文件原子写回标签")
    tags_apply.add_argument("plan_id")
    tags_apply.add_argument("file", type=Path)
    tags_apply.add_argument("--expected-revision", type=int)
    tags_apply.add_argument("--expected-override-revision", type=int)
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
