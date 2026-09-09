"""Fixed, isolated six-profile reliability evaluation (paid only with --live).

The fixtures are synthetic and deliberately public. No live attempts, statements,
conversations or source files are copied. Reports contain hashes and facts only.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, Mapping

from .cache_workload import (
    CappedProviderClient, MODEL, Stage4WorkloadRunner, _Workspace,
    _load_live_provider, _outcome_summary, _safe_error_code, _safe_usage,
    _source_evidence, _percentile,
)
from .config import load_config, save_config
from .storage import Database


PROFILES = ("recommendation", "plan_organize", "plan_generate", "coaching", "patch", "summary")
VERSION = "business-reliability-v1"
CASES_PER_PROFILE = 10
REQUIRED_COMPLETE = 9
USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens", "cache_read_tokens")
VALIDATOR_MESSAGES = frozenset({
    "AI 推荐结果必须是对象", "AI 推荐缺少 ranked 数组", "AI 推荐缺少 focus_topics 数组",
    "AI 推荐包含不属于当前模式区间的 focus topic", "AI 推荐的 focus topic 数量不满足 2 至 3 个板块约束",
    "AI 推荐项必须是对象", "AI 推荐包含越权、重复或未知候选", "AI 推荐题与声明的知识板块不一致",
    "AI 推荐数量不足", "AI 推荐题难度超出本槽目标正负 100 的允许范围",
    "AI 推荐未满足知识板块多样性", "AI 推荐的单板块题量超过上限",
    "AI 声明的 focus topic 与实际推荐不一致", "AI 教练返回了空内容",
    "AI 教练返回内容超过安全上限", "AI 教练内容超过当前提示披露等级",
    "AI 教练内容超过 level=1 提示披露等级", "entry must be an object",
    "entry.aliases must be an array with at most 20 values", "entry.confidence must be a number",
    "entry.confidence must be between 0 and 1", "entry.fields must be an object", "entry.rationale is invalid",
    "rendered_markdown must be a string", "rendered entry exceeds 64 KiB",
    "rendered entry contains NUL or raw HTML", "rendered entry must contain exactly one entry-level heading",
    "rendered fields do not follow schema order", "summary inferred fields must contain objects",
    "summary inferred fields contain duplicate keys", "summary duplicate choice is unresolved",
    "Markdown 总结缓存产物必须是对象", "同一题号对应多个旧条目，无法安全自动合并",
} | {template.format(key=key)
     for key in ("source", "model", "correctness", "implementation", "complexity", "pitfalls", "trigger", "conclusion")
     for template in ("field {key} must be a string", "field {key} exceeds 64 KiB", "field {key} contains raw HTML",
                      "required field is empty: {key}", "rendered field uses wrong layout: {key}", "duplicate rendered field: {key}")})
CRITICAL_FILES = {
    "runner": "tools/acm_agent/business_reliability_workload.py",
    "fixture_seed": "tools/acm_agent/cache_workload.py",
    "ai_service": "tools/acm_agent/service_ai.py",
    "knowledge_service": "tools/acm_agent/service_knowledge.py",
    "storage": "tools/acm_agent/storage_ai.py",
    "provider": "tools/acm_agent/deepseek.py",
    "governance": "tools/acm_agent/provider_governance.py",
    "configuration": "tools/acm_agent/provider_config.py",
}


@dataclass(frozen=True)
class Fixture:
    key: str
    statement: str
    correct_body: str
    broken_body: str
    samples: tuple[tuple[str, str], ...]

    def source(self, *, correct: bool) -> str:
        body = self.correct_body if correct else self.broken_body
        return "#include <bits/stdc++.h>\nusing namespace std;\nint main(){" + body + "}\n"


FIXTURES = (
    Fixture("sum", "Read two integers a,b (-1000000 <= a,b <= 1000000); output a+b.",
            "long long a,b;cin>>a>>b;cout<<a+b;", "long long a,b;cin>>a>>b;cout<<a-b;",
            (("2 7\n", "9"), ("-8 3\n", "-5"), ("0 0\n", "0"))),
    Fixture("maximum", "Read two integers a,b; output the larger integer.",
            "long long a,b;cin>>a>>b;cout<<max(a,b);", "long long a,b;cin>>a>>b;cout<<min(a,b);",
            (("3 9\n", "9"), ("-2 -7\n", "-2"), ("4 4\n", "4"))),
    Fixture("minimum", "Read two integers a,b; output the smaller integer.",
            "long long a,b;cin>>a>>b;cout<<min(a,b);", "long long a,b;cin>>a>>b;cout<<max(a,b);",
            (("7 2\n", "2"), ("-3 -8\n", "-8"), ("0 0\n", "0"))),
    Fixture("absolute_difference", "Read two integers a,b; output the absolute value of a-b.",
            "long long a,b;cin>>a>>b;cout<<abs(a-b);", "long long a,b;cin>>a>>b;cout<<a-b;",
            (("2 9\n", "7"), ("8 3\n", "5"), ("-4 -4\n", "0"))),
    Fixture("array_sum", "Read n (1<=n<=100) followed by n integers; output their sum.",
            "int n;cin>>n;long long s=0,x;while(n--){cin>>x;s+=x;}cout<<s;",
            "int n;cin>>n;long long s=0,x;while(--n){cin>>x;s+=x;}cout<<s;",
            (("3\n2 3 4\n", "9"), ("1\n-5\n", "-5"), ("4\n0 -1 1 7\n", "7"))),
    Fixture("even_count", "Read n followed by n integers; output how many are even (zero is even).",
            "int n,x,s=0;cin>>n;while(n--){cin>>x;s+=(x%2==0);}cout<<s;",
            "int n,x,s=0;cin>>n;while(n--){cin>>x;s+=(x%2!=0);}cout<<s;",
            (("4\n0 2 4 7\n", "3"), ("1\n3\n", "0"), ("3\n-2 -4 -5\n", "2"))),
    Fixture("prefix_sum", "Read n followed by n integers; output all n prefix sums separated by spaces.",
            "int n;cin>>n;long long s=0,x;while(n--){cin>>x;s+=x;cout<<s<<' ';}",
            "int n;cin>>n;long long s=0,x;while(n--){cin>>x;cout<<s<<' ';s+=x;}",
            (("3\n1 2 3\n", "1 3 6"), ("1\n-7\n", "-7"), ("3\n3 -3 2\n", "3 0 2"))),
    Fixture("ascending_sort", "Read n followed by n integers; output the integers sorted in nondecreasing order.",
            "int n;cin>>n;vector<int>a(n);for(int&x:a)cin>>x;sort(a.begin(),a.end());for(int x:a)cout<<x<<' ';",
            "int n;cin>>n;vector<int>a(n);for(int&x:a)cin>>x;sort(a.rbegin(),a.rend());for(int x:a)cout<<x<<' ';",
            (("4\n3 1 2 1\n", "1 1 2 3"), ("1\n8\n", "8"), ("3\n0 -4 2\n", "-4 0 2"))),
    Fixture("inclusive_range", "Read integers l,r (0<=l<=r<=1000); output the sum of all integers in [l,r].",
            "long long l,r;cin>>l>>r;cout<<(l+r)*(r-l+1)/2;",
            "long long l,r;cin>>l>>r;cout<<(l+r)*(r-l)/2;",
            (("2 5\n", "14"), ("7 7\n", "7"), ("0 3\n", "6"))),
    Fixture("gcd", "Read positive integers a,b (1<=a,b<=1000000); output their greatest common divisor.",
            "long long a,b;cin>>a>>b;cout<<gcd(a,b);", "long long a,b;cin>>a>>b;cout<<min(a,b);",
            (("12 18\n", "6"), ("17 13\n", "1"), ("9 9\n", "9"))),
)

# Summary inputs contain reusable algorithmic invariants and correct code;
# elementary arithmetic examples alone are a poor fit for algorithms-v1.
SUMMARY_FIXTURES = (
    FIXTURES[6], FIXTURES[7], FIXTURES[8], FIXTURES[9],
    Fixture("lower_bound", "Read n,x and a sorted array of n integers. Output the zero-based first index with a[i]>=x, or n if absent. Use binary search in O(log n).",
        "int n,x;cin>>n>>x;vector<int>a(n);for(int&v:a)cin>>v;int l=0,r=n;while(l<r){int m=l+(r-l)/2;if(a[m]<x)l=m+1;else r=m;}cout<<l;", "",
        (("4 4\n1 3 4 8\n", "2"), ("1 9\n2\n", "1"), ("3 2\n2 2 2\n", "0"))),
    Fixture("two_pointers", "Read n,S and n positive integers. Output the shortest nonempty contiguous subarray length whose sum is at least S; output 0 if none. Use a linear sliding window.",
        "int n;long long s;cin>>n>>s;vector<int>a(n);for(int&v:a)cin>>v;long long sum=0;int l=0,ans=n+1;for(int r=0;r<n;r++){sum+=a[r];while(sum>=s){ans=min(ans,r-l+1);sum-=a[l++];}}cout<<(ans==n+1?0:ans);", "",
        (("5 7\n2 3 1 4 3\n", "2"), ("1 8\n3\n", "0"), ("3 3\n3 1 2\n", "1"))),
    Fixture("zero_one_knapsack", "Read n,W then n weight,value pairs; each item may be chosen at most once. Output maximum total value with total weight <=W. Use 0/1 knapsack and descending capacity iteration.",
        "int n,W;cin>>n>>W;vector<int>dp(W+1);while(n--){int w,v;cin>>w>>v;for(int j=W;j>=w;j--)dp[j]=max(dp[j],dp[j-w]+v);}cout<<dp[W];", "",
        (("2 5\n2 3\n3 4\n", "7"), ("1 4\n2 3\n", "3"), ("1 1\n2 9\n", "0"))),
    Fixture("disjoint_set", "Read n,m and m undirected edges on vertices 1..n. Output the number of connected components using disjoint-set union with path compression.",
        "int n,m;cin>>n>>m;vector<int>p(n+1);iota(p.begin(),p.end(),0);function<int(int)>f=[&](int x){return p[x]==x?x:p[x]=f(p[x]);};int ans=n;while(m--){int u,v;cin>>u>>v;u=f(u);v=f(v);if(u!=v){p[u]=v;ans--;}}cout<<ans;", "",
        (("4 2\n1 2\n2 3\n", "2"), ("1 0\n", "1"), ("3 3\n1 2\n2 3\n1 3\n", "1"))),
    Fixture("bfs_distance", "Read n,m then m undirected unweighted edges on 1..n. Output shortest distance from 1 to n, or -1 when unreachable. Use BFS with discovery marking.",
        "int n,m;cin>>n>>m;vector<vector<int>>g(n+1);while(m--){int u,v;cin>>u>>v;g[u].push_back(v);g[v].push_back(u);}vector<int>d(n+1,-1);queue<int>q;d[1]=0;q.push(1);while(!q.empty()){int u=q.front();q.pop();for(int v:g[u])if(d[v]<0){d[v]=d[u]+1;q.push(v);}}cout<<d[n];", "",
        (("3 2\n1 2\n2 3\n", "2"), ("1 0\n", "0"), ("3 1\n1 2\n", "-1"))),
    Fixture("maximum_subarray", "Read n>=1 and n signed integers. Output the largest sum of a nonempty contiguous subarray. Use Kadane's algorithm and handle all-negative input.",
        "int n;cin>>n;long long x,cur,best;cin>>x;cur=best=x;while(--n){cin>>x;cur=max(x,cur+x);best=max(best,cur);}cout<<best;", "",
        (("5\n-2 3 -1 4 -5\n", "6"), ("3\n-4 -2 -7\n", "-2"), ("1\n9\n", "9"))),
)


def workload_manifest() -> dict[str, Any]:
    """Freeze all sixty cases before the first paid call, without source in reports."""
    cases = []
    for profile in PROFILES:
        for index, fixture in enumerate(FIXTURES):
            if profile == "summary":
                fixture = SUMMARY_FIXTURES[index]
            definition = {"profile": profile, "index": index, "fixture": asdict(fixture),
                          "plan_ids": _plan_ids(index), "ai_mode": "gap_fill" if index % 2 == 0 else "specialization"}
            cases.append({"case_id": f"{profile}-{index + 1:02d}", "profile": profile,
                          "fixture": fixture.key, "definition_sha256": _digest(definition)})
    return {"version": VERSION, "cases": cases, "cases_per_profile": CASES_PER_PROFILE,
            "required_complete_per_profile": REQUIRED_COMPLETE, "synthetic": True}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _source_files(root: Path) -> dict[str, str]:
    selected = dict(CRITICAL_FILES)
    for path in sorted((root / "tools/acm_agent").rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        selected["module:" + relative] = relative
    return selected


def _flag(value: Any, expected: bool) -> bool:
    """SQLite flags may be ints; missing, floats and arbitrary truthy values fail."""
    return value is expected or (type(value) is int and value == int(expected))


def _validation_messages(value: Any) -> list[str]:
    """Only exact, source-defined validator sentences cross the diagnostic boundary."""
    found: set[str] = set()
    def visit(item: Any) -> None:
        if isinstance(item, str):
            if item in VALIDATOR_MESSAGES:
                found.add(item)
        elif isinstance(item, (list, tuple)):
            for nested in item[:64]:
                visit(nested)
        elif isinstance(item, Mapping):
            for key in ("message", "validation_error", "validation_message", "validator_messages", "validation_issues",
                        "validation_errors", "violations", "field_errors", "protocol_details", "error", "errors", "ai", "fallback"):
                if key in item:
                    visit(item[key])
    visit(value)
    return sorted(found)


def _usage_completeness(legs: list[Mapping[str, Any]]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    complete_core = 0
    for leg in legs:
        usage = leg.get("usage")
        if isinstance(usage, Mapping) and all(type(usage.get(k)) is int and usage[k] >= 0 for k in USAGE_KEYS[:3]):
            complete_core += 1
    for key in USAGE_KEYS:
        known, invalid = [], 0
        for leg in legs:
            usage = leg.get("usage")
            if not isinstance(usage, Mapping) or key not in usage:
                continue
            value = usage[key]
            if type(value) is int and value >= 0:
                known.append(value)
            else:
                invalid += 1
        unknown = len(legs) - len(known)
        fields[key] = {"known_legs": len(known), "unknown_legs": unknown,
                       "invalid_legs": invalid, "known_sum": sum(known) if known else None,
                       "complete_sum": sum(known) if legs and unknown == 0 else None}
    return {"provider_legs": len(legs), "complete_core_usage_legs": complete_core,
            "unknown_or_partial_core_usage_legs": len(legs) - complete_core,
            "fields": fields,
            "basis": "Recorded leg telemetry; completeness inside a multi-HTTP leg is not independently inferred."}


def _plan_ids(index: int) -> list[str]:
    return [f"{101 + (index + offset) % 7}A" for offset in range(1 + index % 3)]


def full_outcome(outcome: Mapping[str, Any]) -> bool:
    return (outcome.get("provider_outcome") == "succeeded"
            and outcome.get("artifact_outcome") in {"valid", "repaired"}
            and outcome.get("business_outcome") == "complete"
            and _flag(outcome.get("usable"), True)
            and _flag(outcome.get("degraded"), False))


def _tasks(value: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [task for stage in value.get("plan", {}).get("stages", []) for task in stage.get("tasks", [])]


def _task_key(task: Mapping[str, Any]) -> str:
    key = str(task.get("problem_key") or "")
    if key:
        platform, _, problem_id = key.partition(":")
    else:
        platform, problem_id = str(task.get("platform", "")), str(task.get("problem_id", ""))
    if platform == "codeforces" and problem_id.startswith("CF"):
        problem_id = problem_id[2:]
    return f"{platform}:{problem_id}"


class BusinessReliabilityRunner(Stage4WorkloadRunner):
    def __init__(self, root: str | Path, provider: CappedProviderClient, *, progress=None,
                 report_directory: Path | None = None, resume: bool = False) -> None:
        super().__init__(root, provider)
        self.progress = progress
        self.configuration: dict[str, Any] = {}
        self.report_directory = report_directory
        self.resume = resume
        self._journal_ready = False
        self._wire_records: list[dict[str, Any]] = []
        self._wire_transport = None

    def _attach_wire_recorder(self) -> None:
        client = self.provider._client
        transport = vars(client).get("_transport")
        if not callable(transport):
            raise RuntimeError("workload_wire_transport_unavailable")
        self._wire_transport = transport

        def observed(request, timeout):
            # Observe the serialized payload immediately before actual HTTP.
            # Never retain headers, credentials, URL, messages, input or tools.
            payload = json.loads(request.data.decode("utf-8"))
            responses = "/responses" in str(request.full_url)
            thinking = payload.get("thinking", {})
            reasoning = payload.get("reasoning", {})
            fact = {
                "api": "responses" if responses else "chat_completions",
                "model": payload.get("model") if payload.get("model") in {MODEL, "deepseek-v4-pro"} else "unexpected",
                "thinking_type": thinking.get("type") if isinstance(thinking, Mapping) else None,
                "reasoning_effort": payload.get("reasoning_effort"),
                "responses_reasoning_effort": reasoning.get("effort") if isinstance(reasoning, Mapping) else None,
                "stream": payload.get("stream") is True,
            }
            fact["medium_wire_valid"] = fact["model"] == MODEL and (
                (responses and fact["responses_reasoning_effort"] == "high") or
                (not responses and fact["thinking_type"] == "enabled" and fact["reasoning_effort"] == "high"))
            self._wire_records.append(fact)
            return transport(request, timeout)

        client._transport = observed

    def _detach_wire_recorder(self) -> None:
        if self._wire_transport is not None:
            self.provider._client._transport = self._wire_transport
            self._wire_transport = None

    def _journal(self, value: Mapping[str, Any]) -> None:
        if not self._journal_ready or self.report_directory is None:
            return
        with (self.report_directory / "cases.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _open_journal(self, manifest: Mapping[str, Any], source: Mapping[str, Any]) -> None:
        if self.report_directory is None:
            self.report_directory = self.root / ".acm/reports/business-reliability" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = self.report_directory / "manifest.json"
        metadata = {"workload": manifest, "workload_sha256": _digest(manifest),
                    "source_evidence": source, "provider": "deepseek", "model": MODEL,
                    "requested_reasoning_strength": "medium", "wire_reasoning_effort": "high",
                    "no_local_cache": True, "fallbacks": False}
        if self.resume:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing["workload_sha256"] != metadata["workload_sha256"] or existing["source_evidence"]["critical_files"] != source["critical_files"]:
                raise RuntimeError("workload_resume_definition_or_source_changed")
            started, completed = set(), {}
            journal = self.report_directory / "cases.jsonl"
            if journal.exists():
                for line in journal.read_text(encoding="utf-8").splitlines():
                    event = json.loads(line)
                    if event["event"] == "started":
                        started.add(event["case_id"])
                    elif event["event"] == "completed":
                        record = event["record"]
                        if record["case_id"] in completed:
                            raise RuntimeError("workload_duplicate_completed_case")
                        completed[record["case_id"]] = record
            if started - completed.keys():
                # A crash after HTTP dispatch but before the terminal journal
                # creates an unknown paid outcome. Never silently pay again.
                raise RuntimeError("workload_resume_has_uncertain_paid_case")
            allowed = {case["case_id"] for case in manifest["cases"]}
            if not set(completed) <= allowed:
                raise RuntimeError("workload_resume_unknown_case")
            self.records = list(completed.values())
            config_path = self.report_directory / "configuration.json"
            if config_path.exists():
                self.configuration = json.loads(config_path.read_text(encoding="utf-8"))
        else:
            self.report_directory.mkdir(parents=True, exist_ok=False)
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        self._journal_ready = True

    def _configure(self, service, *, exact_cache=False, validation_repairs=1) -> None:
        super()._configure(service, exact_cache=False, validation_repairs=1)
        config = load_config(service.paths)
        config["ai"]["coaching_delivery_mode"] = "low_latency"
        for profile in PROFILES:
            config["ai"]["profiles"][profile].update(
                provider_id="deepseek", model=MODEL, thinking=True,
                reasoning_strength="medium", reasoning_effort="high")
        save_config(service.paths, config)
        self.configuration = {
            "provider": "deepseek", "model": MODEL, "reasoning_strength": "medium",
            "wire_reasoning_effort": "high", "wire_thinking": "enabled",
            "profiles": {p: dict(config["ai"]["profiles"][p]) for p in PROFILES},
            "budgets": {p: dict(config["ai"]["policy"]["budgets"][p]) for p in PROFILES},
            "fallbacks": {p: [] for p in PROFILES}, "exact_cache_profiles": [],
            "semantic_cache": False, "coaching_delivery_mode": "low_latency",
        }

    def _prepare_case(self, profile: str, index: int) -> _Workspace:
        workspace = self._workspace(phase=f"{profile}-{index + 1:02d}", exact_cache=False)
        service = workspace.service
        if service.paths.root.resolve() == self.root or service.paths.database.resolve() == self.root / ".acm/state.db":
            raise RuntimeError("workload_isolation_failed")
        fixture = SUMMARY_FIXTURES[index] if profile == "summary" else FIXTURES[index]
        with Database(service.paths.database) as db:
            if profile == "recommendation":
                # Distinct catalog coverage and score ties in each fixed case.
                # Every topic has enough candidates at every relevant rating.
                db.upsert_problems([
                    {"platform": "codeforces", "problem_id": f"{number}A", "name": f"Fixture {number}A",
                     "rating": 800 + 100 * ((number + index) % 5),
                     "tags": [["dp", "math"], ["greedy", "math"], ["dp", "greedy"]][(number + index) % 3]}
                    for number in range(101, 131)
                ])
            row = db.connection.execute("SELECT path FROM local_files WHERE platform='codeforces' AND problem_id='100A'").fetchone()
        if row is None:
            raise RuntimeError("workload_fixture_source_missing")
        source = Path(row["path"])
        if not source.is_absolute():
            source = service.paths.root / source
        if not source.resolve().is_relative_to(service.paths.root.resolve()):
            raise RuntimeError("workload_fixture_path_escape")
        source.write_text(fixture.source(correct=profile == "summary"), encoding="utf-8")
        sample_in, sample_out = fixture.samples[0]
        with Database(service.paths.database) as db:
            context_hash = db.connection.execute("SELECT content_hash FROM problem_contexts WHERE platform='codeforces' AND problem_id='100A' AND source='manual'").fetchone()[0]
        service.problem_context_save("CF100A", content=fixture.statement + f"\n\nInput\n{sample_in}\nOutput\n{sample_out}\n", expected_hash=context_hash)
        if profile == "summary":
            service.close("CF100A", result="AC", minutes=10 + index, hint_level=index % 3, failure="none")
        return workspace

    def _coaching(self, workspace: _Workspace, index: int) -> dict[str, Any]:
        service = workspace.service
        conversation = service.ai_conversation_start("CF100A", reasoning_strength="medium")
        fixture = FIXTURES[index]
        data, expected = fixture.samples[-1]
        message = ("请检查这个边界输入，简短解释正确结果，不要输出源码。最后一行严格写 CHECK= 后接结果（空格分隔整数）：\n" + data)
        content, done, errors, delta_count = "", False, [], 0
        usage, outcome = {}, {}
        for event in service.ai_chat_stream(str(conversation["conversation_id"]), message=message,
                                            mode="explain", hint_level=3, reasoning_strength="medium",
                                            delivery_mode="low_latency"):
            payload = event.get("data", {})
            if event.get("event") == "delta":
                content += str(payload.get("content") or "")
                delta_count += 1
            elif event.get("event") == "usage":
                usage = dict(payload.get("usage") or {})
            elif event.get("event") == "done":
                done = True
                outcome = dict(payload.get("outcome") or {})
            elif event.get("event") == "error":
                errors.append(_safe_error_code(payload.get("code")) or "workload_stream_error")
        matches = re.findall(r"CHECK\s*=\s*([-+0-9 \t]+)", content)
        checked = bool(matches) and matches[-1].split() == expected.split()
        return {"ok": done and delta_count > 0 and bool(content.strip()) and not errors,
                "local_correct": checked and "```" not in content,
                "usage": usage, "outcome": outcome, "stream_done": done,
                "stream_delta_events": delta_count, "errors": [{"code": e} for e in errors],
                "_private_output": {"assistant_text": content}}

    def _call_case(self, workspace: _Workspace, profile: str, index: int) -> Any:
        service = workspace.service
        ids = _plan_ids(index)
        if profile == "recommendation":
            return service.ai_recommendations(count=1 + index % 3, mode="mixed", source_mode="catalog_only",
                ai_mode="gap_fill" if index % 2 == 0 else "specialization", reasoning_strength="medium")
        if profile in {"plan_organize", "plan_generate"}:
            formats = (" ", "\n", "、", ", ", "\n- ")
            text = formats[index % len(formats)].join("CF" + key for key in ids)
            return service.ai_plan_preview(mode="organize" if profile == "plan_organize" else "generate",
                text="仅选择以下题目，按给出的顺序生成训练计划，不得新增其他题：\n" + text,
                task_count=len(ids), include_completed=False, reasoning_strength="medium")
        if profile == "coaching":
            return self._coaching(workspace, index)
        if profile == "patch":
            return service.ai_patch_preview("CF100A", instruction="按已保存题意修复当前程序，保留完整可编译 C++17 源码并说明修改原因。",
                                            reasoning_strength="medium")
        return service.knowledge_preview(workspace.attempt_id, workspace.target_id,
                                         schema_mode="stored", reasoning_strength="medium")

    @staticmethod
    def _compile_fixture(workspace: _Workspace, value: Mapping[str, Any], index: int) -> bool:
        source = value.get("candidate_code")
        if not isinstance(source, str) or not source.strip():
            return False
        # Same compile/run approach as Stage4WorkloadRunner._compile_patch, with
        # three independently fixed expected outputs for each distinct fixture.
        scratch = Path(workspace.temporary.name) / ".acm/reliability-patch.cpp"
        binary = scratch.with_suffix(".exe")
        scratch.write_text(source, encoding="utf-8")
        compiled = subprocess.run(["g++", "-std=c++17", "-O2", str(scratch), "-o", str(binary)],
                                  capture_output=True, timeout=30, check=False)
        if compiled.returncode:
            return False
        for data, expected in FIXTURES[index].samples:
            result = subprocess.run([str(binary)], input=data.encode(), capture_output=True,
                                    timeout=5, check=False)
            if result.returncode or result.stdout.split() != expected.encode().split():
                return False
        return True

    def _local_gate(self, workspace: _Workspace, profile: str, index: int, value: Any) -> bool:
        if not isinstance(value, Mapping) or not value.get("ok", False):
            return False
        if profile == "patch":
            return self._compile_fixture(workspace, value, index)
        if profile == "coaching":
            return bool(value.get("local_correct"))
        if profile in {"plan_organize", "plan_generate"}:
            expected = ["codeforces:" + key for key in _plan_ids(index)]
            return [_task_key(t) for t in _tasks(value)] == expected
        if profile == "recommendation":
            recommendations = value.get("recommendations") or []
            keys = [_task_key(item) for item in recommendations]
            valid = {f"codeforces:{i}A" for i in range(101, 131)}
            return (len(keys) == 1 + index % 3 and len(set(keys)) == len(keys)
                    and set(keys) <= valid and not value.get("ai", {}).get("fallback"))
        proposal = value.get("proposal") or {}
        if not proposal.get("can_apply"):
            return False
        with Database(workspace.service.paths.database) as db:
            row = db.connection.execute("SELECT candidate_bytes,candidate_hash,entry_json,status FROM markdown_summary_proposals ORDER BY created_at DESC LIMIT 1").fetchone()
        if row is None or row["status"] != "preview":
            return False
        entry = json.loads(row["entry_json"])
        return bool(entry) and hashlib.sha256(bytes(row["candidate_bytes"])).hexdigest() == row["candidate_hash"]

    def _case_ledger(self, workspace: _Workspace) -> list[dict[str, Any]]:
        return [leg for leg in self._provider_leg_evidence() if leg["phase"] == workspace.phase]

    @staticmethod
    def _owned_file(workspace: _Workspace, value: Any) -> Path:
        root = workspace.service.paths.root.resolve()
        path = Path(str(value))
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        if not path.is_relative_to(root):
            raise RuntimeError("workload_postprocess_path_escape")
        return path

    def _apply_gate(self, workspace: _Workspace, profile: str, index: int, value: Mapping[str, Any]) -> bool:
        """Exercise real mutation endpoints only inside the disposable workspace."""
        service = workspace.service
        if profile in {"plan_organize", "plan_generate"}:
            imported = service.plan_import(plan=value["plan"])
            plan_id = str(imported["plan_id"])
            detail = service.plan_detail(plan_id)
            expected = ["codeforces:" + key for key in _plan_ids(index)]
            with Database(service.paths.database) as db:
                row = db.connection.execute("SELECT managed_path FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
                tasks = db.query("SELECT platform,problem_id FROM plan_tasks WHERE plan_id=? ORDER BY stage_key,position", (plan_id,))
            if not imported.get("ok") or not detail.get("ok") or row is None:
                return False
            persisted = json.loads(self._owned_file(workspace, row["managed_path"]).read_text(encoding="utf-8-sig"))
            return ([_task_key(t) for t in _tasks(detail)] == expected
                    and [_task_key(t) for t in _tasks({"plan": persisted})] == expected
                    and sorted(_task_key(dict(t)) for t in tasks) == sorted(expected))
        if profile == "summary":
            proposal = value["proposal"]
            proposal_id = str(proposal.get("proposal_id") or proposal["id"])
            applied = service.knowledge_apply(proposal_id, expected_revision=int(proposal["revision"]))
            with Database(service.paths.database) as db:
                row = db.connection.execute("SELECT status,target_path,candidate_hash,applied_hash FROM markdown_summary_proposals WHERE id=?", (proposal_id,)).fetchone()
            return bool(applied.get("ok") and row is not None and row["status"] == "applied"
                        and hashlib.sha256(self._owned_file(workspace, row["target_path"]).read_bytes()).hexdigest() == row["candidate_hash"] == row["applied_hash"])
        if profile == "patch":
            proposal_id = str(value["proposal_id"])
            applied = service.ai_patch_apply(proposal_id)
            with Database(service.paths.database) as db:
                row = db.connection.execute("SELECT status,source_path,applied_hash FROM ai_patch_proposals WHERE id=?", (proposal_id,)).fetchone()
            if not applied.get("ok") or row is None or row["status"] != "applied":
                return False
            source = self._owned_file(workspace, row["source_path"]).read_bytes()
            return (hashlib.sha256(source).hexdigest() == row["applied_hash"]
                    and self._compile_fixture(workspace, {"candidate_code": source.decode("utf-8-sig")}, index))
        return True

    def _save_failure_artifact(self, workspace: _Workspace, value: Any, record: Mapping[str, Any]) -> str | None:
        """Keep only generated outputs from our synthetic fixture, never request metadata."""
        if self.report_directory is None:
            return None
        artifact: dict[str, Any] = {"synthetic": True, "case_id": workspace.phase,
                                  "outcome": record["outcome"], "error_codes": record["error_codes"]}
        if isinstance(value, Mapping):
            for key in ("candidate_code", "diagnosis", "plan"):
                if value.get(key) is not None:
                    artifact[key] = value[key]
            proposal = value.get("proposal")
            if isinstance(proposal, Mapping):
                artifact["summary"] = {key: proposal[key] for key in ("entry", "entry_markdown", "warnings", "rationale", "confidence") if key in proposal}
            private = value.get("_private_output")
            if isinstance(private, Mapping):
                artifact["assistant_text"] = str(private.get("assistant_text") or "")
        artifact["validator_messages"] = sorted(set(_validation_messages(value)) | set(_validation_messages(record.get("error"))))
        with Database(workspace.service.paths.database) as db:
            artifact["assistant_messages"] = [dict(row) for row in db.query("SELECT status,content FROM ai_messages WHERE role='assistant' ORDER BY created_at")]
            artifact["patch_outputs"] = [dict(row) for row in db.query("SELECT candidate_code,diagnosis,status FROM ai_patch_proposals ORDER BY created_at")]
            artifact["summary_outputs"] = [{"entry": json.loads(row["entry_json"]), "warnings": json.loads(row["warnings_json"]), "status": row["status"]} for row in db.query("SELECT entry_json,warnings_json,status FROM markdown_summary_proposals ORDER BY created_at")]
            # Retain only validation facts; API error messages, request bodies,
            # credential metadata and provider headers are excluded.
            artifact["validation_facts"] = []
            for row in db.query("SELECT error_json FROM ai_runs ORDER BY created_at"):
                error = json.loads(row["error_json"] or "{}")
                artifact["validator_messages"] = sorted(set(artifact["validator_messages"]) | set(_validation_messages(error)))
                details = error.get("protocol_details") or {}
                if isinstance(details, Mapping):
                    safe = {"validator_messages": _validation_messages(details)}
                    for key, allowed in (("incomplete_reason", {"max_output_tokens", "max_tokens", "content_filter", "unknown"}),
                                         ("response_status", {"completed", "incomplete", "failed", "cancelled", "in_progress"})):
                        if isinstance(details.get(key), str) and details[key] in allowed:
                            safe[key] = details[key]
                    artifact["validation_facts"].append(safe)
        directory = self.report_directory / "private"
        directory.mkdir(exist_ok=True)
        path = directory / (workspace.phase + ".json")
        path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return "private/" + path.name

    def _run_case(self, profile: str, index: int) -> dict[str, Any]:
        workspace = self._prepare_case(profile, index)
        if self._journal_ready:
            configuration_path = self.report_directory / "configuration.json"
            if configuration_path.exists():
                if json.loads(configuration_path.read_text(encoding="utf-8")) != self.configuration:
                    raise RuntimeError("workload_configuration_changed")
            else:
                with configuration_path.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(json.dumps(self.configuration, ensure_ascii=False, indent=2) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
        self._journal({"event": "started", "case_id": workspace.phase,
                       "started_at": datetime.now(timezone.utc).isoformat()})
        before = self.provider.provider_request_count
        wire_before = len(self._wire_records)
        started = time.perf_counter()
        value, error, local_correct, applied_correct = None, None, False, False
        try:
            value = self._call_case(workspace, profile, index)
            local_correct = self._local_gate(workspace, profile, index, value)
            if local_correct and full_outcome(_outcome_summary(value)):
                applied_correct = self._apply_gate(workspace, profile, index, value)
        except Exception as exc:
            error = {"code": _safe_error_code(getattr(exc, "code", "workload_case_failed")),
                     "type": type(exc).__name__, "validator_messages": _validation_messages(str(exc))}
        http = self.provider.provider_request_count - before
        with Database(workspace.service.paths.database) as db:
            runs = db.query("SELECT status,provider_outcome,artifact_outcome,business_outcome,usable,apply_ready,degraded,repair_attempts,local_cache_status,requested_model,resolved_model,requested_reasoning_strength,error_json FROM ai_runs ORDER BY created_at")
        outcome = _outcome_summary(value)
        if runs:
            outcome = {k: runs[-1][k] for k in ("provider_outcome", "artifact_outcome", "business_outcome", "usable", "apply_ready", "degraded", "repair_attempts")}
        legs = self._case_ledger(workspace)
        wire = self._wire_records[wire_before:]
        wire_valid = bool(wire) and len(wire) == http and all(fact["medium_wire_valid"] for fact in wire)
        accounted = sum(leg["provider_requests"] for leg in legs)
        routing_valid = bool(runs) and all(r["requested_model"] == MODEL and r["requested_reasoning_strength"] == "medium" for r in runs)
        # A failed transport may never return a model. Completed legs must name
        # the actual Flash model; no unverified aliases or cross-model routes.
        routing_valid = routing_valid and bool(legs) and all(
            leg["provider"] == "deepseek" and leg["requested_model"] == MODEL
            and leg["reasoning_strength"] == "medium" and leg["route_kind"] != "fallback"
            and (leg["resolved_model"] == MODEL or (leg["status"] == "failed" and leg["resolved_model"] == "unknown"))
            for leg in legs)
        routing_valid = routing_valid and all(r["resolved_model"] == MODEL or (r["provider_outcome"] == "failed" and r["resolved_model"] is None) for r in runs)
        cache_disabled = all(r["local_cache_status"] not in {"hit", "coalesced"} for r in runs)
        errors = sorted({_safe_error_code(json.loads(r["error_json"] or "{}").get("code")) for r in runs} - {None})
        record = {"case_id": workspace.phase, "profile": profile, "logical_index": index + 1,
            "local_correct": bool(local_correct), "applied_and_readback_correct": bool(applied_correct),
            "complete": bool(local_correct and applied_correct and full_outcome(outcome) and routing_valid and cache_disabled and wire_valid),
            "wire_requests": wire, "wire_medium_verified": wire_valid,
            "outcome": outcome, "routing_valid": routing_valid, "no_local_cache_hit": cache_disabled,
            "http_requests": http, "leg_http_requests": accounted, "http_accounted": http == accounted,
            "usage_completeness": _usage_completeness(legs),
            "provider_legs": legs, "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "error_codes": errors, **({"error": error} if error else {})}
        if not record["complete"]:
            record["diagnostic_artifact"] = self._save_failure_artifact(workspace, value, record)
        self.records.append(record)
        self._journal({"event": "completed", "record": record})
        if self.progress:
            self.progress({k: record[k] for k in ("case_id", "complete", "http_requests", "latency_ms")})
        return record

    def run(self) -> dict[str, Any]:
        if not shutil.which("g++"):
            raise RuntimeError("workload_compiler_missing")
        manifest = workload_manifest()
        initial = _source_evidence(self.root, critical_files=_source_files(self.root))
        self._open_journal(manifest, initial)
        started = datetime.now(timezone.utc).isoformat()
        before = self.provider.provider_request_count
        historical_http = sum(r["http_requests"] for r in self.records)
        completed_ids = {r["case_id"] for r in self.records}
        try:
            if len(self.records) < 60:
                self._attach_wire_recorder()
            for profile in PROFILES:
                for index in range(CASES_PER_PROFILE):
                    if f"{profile}-{index + 1:02d}" not in completed_ids:
                        self._run_case(profile, index)
            final = _source_evidence(self.root, critical_files=_source_files(self.root))
            profiles = {}
            for profile in PROFILES:
                records = [r for r in self.records if r["profile"] == profile]
                complete = sum(r["complete"] for r in records)
                profiles[profile] = {"logical_requests": len(records), "complete": complete,
                    "complete_rate": complete / CASES_PER_PROFILE, "passed": len(records) == CASES_PER_PROFILE and complete >= REQUIRED_COMPLETE,
                    "http_requests": sum(r["http_requests"] for r in records),
                    "usage_completeness": _usage_completeness([leg for r in records for leg in r["provider_legs"]]),
                    "p50_ms": _percentile((r["latency_ms"] for r in records), .5),
                    "p95_ms": _percentile((r["latency_ms"] for r in records), .95)}
            http = historical_http + self.provider.provider_request_count - before
            facts = [leg for r in self.records for leg in r["provider_legs"]]
            usage_completeness = _usage_completeness(facts)
            usage = {key: usage_completeness["fields"][key]["complete_sum"] for key in USAGE_KEYS}
            gates = {"all_sixty_executed": len(self.records) == 60,
                     "all_profiles_at_least_nine_complete": all(p["passed"] for p in profiles.values()),
                     "every_http_accounted": all(r["http_accounted"] for r in self.records) and http == sum(r["http_requests"] for r in self.records) == sum(leg["provider_requests"] for leg in facts),
                     "fixed_route_medium": all(r["routing_valid"] for r in self.records),
                     "actual_wire_medium": all(r["wire_medium_verified"] for r in self.records),
                     "no_local_cache_hits": all(r["no_local_cache_hit"] for r in self.records),
                     "executed_source_unchanged": initial["critical_files"] == final["critical_files"]}
            report = {"report_version": VERSION, "synthetic": True, "passed": all(gates.values()),
                "started_at": started, "completed_at": datetime.now(timezone.utc).isoformat(),
                "workload": manifest, "workload_sha256": _digest(manifest), "source_evidence": initial,
                "source_evidence_after": final, "configuration": self.configuration,
                "configuration_sha256": _digest(self.configuration), "provider": "deepseek", "model": MODEL,
                "reasoning_strength": "medium", "logical_requests": len(self.records),
                "http_requests": http, "extra_http_requests": max(0, http - len(self.records)),
                "provider_request_limit": self.provider.limit, "usage_from_legs": usage,
                "known_usage_from_legs": {key: usage_completeness["fields"][key]["known_sum"] for key in USAGE_KEYS},
                "usage_completeness": usage_completeness,
                "profiles": profiles, "gates": gates, "cases": self.records,
                "scope_note": "Fixed synthetic business fixtures; local gates check outputs and contracts, not general algorithm tutoring quality."}
            (self.report_directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return report
        finally:
            self._detach_wire_recorder()
            for workspace in self._workspaces:
                workspace.close()


def write_report(root: Path, report: Mapping[str, Any]) -> Path:
    directory = root / ".acm/reports/business-reliability" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory.mkdir(parents=True, exist_ok=False)
    path = directory / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--live", action="store_true", help="Authorize sixty paid logical business requests plus bounded retries/repairs")
    parser.add_argument("--resume", type=Path, help="Resume an unchanged journal; uncertain paid cases are never repeated automatically")
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("--live is required; no provider request was sent")
    # The existing loader binds the real credential in memory only. The service
    # instances below always belong to newly seeded temporary workspaces.
    provider = _load_live_provider(args.root.resolve())
    provider.limit = 210  # 10 * (five profiles with 3 calls + plan generation with 6).
    runner = BusinessReliabilityRunner(args.root, provider, report_directory=args.resume, resume=bool(args.resume),
        progress=lambda value: print(json.dumps(value), flush=True))
    report = runner.run()
    path = runner.report_directory / "report.json"
    print(json.dumps({"passed": report["passed"], "report_path": str(path)}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
