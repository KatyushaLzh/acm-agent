"""Problem reference parsing and date-based ACM workspace management."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import re
import tempfile
from urllib.parse import urlparse


_CF_ID_RE = re.compile(r"^CF(?P<contest>\d+)(?P<index>[A-Z][A-Z0-9]*)$", re.I)
_LUOGU_ID_RE = re.compile(r"^P(?P<number>\d+)$", re.I)
_SOLUTION_RE = re.compile(
    r"^((?:CF\d+[A-Z][A-Z0-9]*|P\d+|[A-Za-z0-9_][A-Za-z0-9_.-]*[A-Za-z0-9]))\.cpp$",
    re.I,
)
_HELPER_FILE_SUFFIXES = (".gen.cpp", ".bf.cpp", ".ref.cpp", ".stress.cpp")
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)
_CUSTOM_ID_MAX_LENGTH = 64
DEFAULT_TEMPLATE_MAX_BYTES = 64 * 1024


DEFAULT_TEMPLATE = """#include <bits/stdc++.h>

using ll = long long;
using pii = std::pair <int, int>;


void solve() {

}

int main() {
    std::ios::sync_with_stdio(0);
    std::cin.tie(0);
    std::cout.tie(0);

    int T = 1;
    // std::cin >> T;
    while (T--) {
        solve();
    }

    return 0;
}
"""


STRESS_TEMPLATE = """#include <bits/stdc++.h>

using ll = long long;

int main(int argc, char **argv) {
    std::ios::sync_with_stdio(0);
    std::cin.tie(0);

    // argv[1] and ACM_STRESS_SEED contain the reproducible random seed.
    return 0;
}
"""


@dataclass(frozen=True, slots=True)
class ProblemRef:
    platform: str
    problem_id: str

    @property
    def key(self) -> str:
        """Filesystem-safe canonical problem key."""
        return self.problem_id

    @property
    def contest_id(self) -> int | None:
        match = _CF_ID_RE.fullmatch(self.problem_id)
        return int(match.group("contest")) if match else None

    @property
    def index(self) -> str | None:
        match = _CF_ID_RE.fullmatch(self.problem_id)
        return match.group("index") if match else None


@dataclass(frozen=True, slots=True)
class LocalSolution:
    problem: ProblemRef
    path: Path
    solved_on: date

    def to_dict(self) -> dict[str, object]:
        return {
            "platform": self.problem.platform,
            "problem_id": self.problem.problem_id,
            "path": str(self.path),
            "solved_on": self.solved_on.isoformat(),
            "status": "local_only",
        }


@dataclass(frozen=True, slots=True)
class StartResult:
    problem: ProblemRef
    source: Path
    reused: bool
    template_source: Path | None
    brute_force: Path | None
    generator: Path | None

    def to_dict(self) -> dict[str, object]:
        return {
            "platform": self.problem.platform,
            "problem_id": self.problem.problem_id,
            "source": str(self.source),
            "reused": self.reused,
            "template_source": (
                str(self.template_source) if self.template_source else "builtin"
            ),
            "brute_force": str(self.brute_force) if self.brute_force else None,
            "generator": str(self.generator) if self.generator else None,
        }


def parse_problem_ref(value: str | ProblemRef) -> ProblemRef:
    """Parse a Luogu/Codeforces id or public problem URL.

    Accepted examples include ``CF1791C``, ``P3373``, Codeforces problemset
    and contest URLs, and Luogu ``/problem/P3373`` URLs.
    """
    if isinstance(value, ProblemRef):
        return value
    raw = value.strip()
    if not raw:
        raise ValueError("problem reference must not be empty")

    direct = _parse_problem_id(raw)
    if direct:
        return direct

    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]

    if host == "codeforces.com" or host.endswith(".codeforces.com"):
        contest: str | None = None
        index: str | None = None
        # /problemset/problem/<contest>/<index>
        if len(parts) >= 4 and parts[0].lower() == "problemset" and parts[1].lower() == "problem":
            contest, index = parts[2], parts[3]
        # /contest/<contest>/problem/<index> and /gym/<contest>/problem/<index>
        elif len(parts) >= 4 and parts[0].lower() in {"contest", "gym"} and parts[2].lower() == "problem":
            contest, index = parts[1], parts[3]
        if contest and index:
            return _require_problem_id(f"CF{contest}{index}")
        raise ValueError(f"unsupported Codeforces problem URL: {value}")

    if host in {"luogu.com.cn", "www.luogu.com.cn", "luogu.org"} or host.endswith(".luogu.com.cn"):
        if len(parts) >= 2 and parts[0].lower() == "problem":
            return _require_problem_id(parts[1], expected_platform="luogu")
        raise ValueError(f"unsupported Luogu problem URL: {value}")

    if "://" in raw or raw.startswith("www."):
        raise ValueError(f"unsupported problem URL: {value}")

    return _custom_problem_ref(raw)


def _custom_problem_ref(value: str) -> ProblemRef:
    """Accept an arbitrary problem id as a pure discriminator.

    The id only names files, attempt records and Markdown summaries, so any
    value is accepted as long as it stays filesystem-safe on Windows.
    """
    normalized = value.upper()
    sanitized = re.sub(r"[^\w.-]", "_", normalized, flags=re.UNICODE).strip("._-")
    sanitized = sanitized[:_CUSTOM_ID_MAX_LENGTH]
    if not sanitized:
        raise ValueError(f"unsupported problem id or URL: {value}")
    if sanitized.split(".", 1)[0] in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"problem id {value!r} conflicts with a Windows reserved name")
    return ProblemRef("custom", sanitized)


def _parse_problem_id(value: str) -> ProblemRef | None:
    normalized = value.upper()
    if _CF_ID_RE.fullmatch(normalized):
        return ProblemRef("codeforces", normalized)
    if _LUOGU_ID_RE.fullmatch(normalized):
        return ProblemRef("luogu", normalized)
    return None


def _require_problem_id(value: str, expected_platform: str | None = None) -> ProblemRef:
    result = _parse_problem_id(value)
    if result is None or (expected_platform and result.platform != expected_platform):
        raise ValueError(f"invalid problem id: {value}")
    return result


def scan_local_solutions(root: str | Path) -> list[LocalSolution]:
    """Scan only ``YYYY/M/D/<problem>.cpp`` solution files.

    Stress helpers and arbitrary nested C++ files are intentionally ignored.
    A local file proves only ``local_only`` state, never acceptance.
    """
    root_path = Path(root).resolve()
    solutions: list[LocalSolution] = []
    if not root_path.exists():
        return solutions

    for year_dir in root_path.iterdir():
        if not year_dir.is_dir() or not re.fullmatch(r"\d{4}", year_dir.name):
            continue
        for month_dir in year_dir.iterdir():
            if not month_dir.is_dir() or not month_dir.name.isdigit():
                continue
            for day_dir in month_dir.iterdir():
                if not day_dir.is_dir() or not day_dir.name.isdigit():
                    continue
                try:
                    solved_on = date(
                        int(year_dir.name), int(month_dir.name), int(day_dir.name)
                    )
                except ValueError:
                    continue
                for path in day_dir.iterdir():
                    if not path.is_file():
                        continue
                    name_lower = path.name.lower()
                    if (
                        name_lower == "template.cpp"
                        or name_lower.endswith(_HELPER_FILE_SUFFIXES)
                    ):
                        continue
                    match = _SOLUTION_RE.fullmatch(path.name)
                    if not match:
                        continue
                    problem = parse_problem_ref(match.group(1))
                    solutions.append(LocalSolution(problem, path.resolve(), solved_on))

    return sorted(
        solutions,
        key=lambda item: (item.solved_on, item.problem.problem_id, str(item.path)),
    )


def find_solution(root: str | Path, problem: str | ProblemRef) -> Path:
    """Return the newest dated source for a problem."""
    ref = parse_problem_ref(problem)
    matches = [
        item for item in scan_local_solutions(root) if item.problem.problem_id == ref.problem_id
    ]
    if not matches:
        raise FileNotFoundError(f"no local solution found for {ref.problem_id}")
    return max(matches, key=lambda item: (item.solved_on, str(item.path))).path


def global_template_path(root: str | Path) -> Path:
    """The user-editable default source template under ``.acm``."""
    return Path(root).resolve() / ".acm" / "template.cpp"


def load_default_template(root: str | Path) -> str:
    path = global_template_path(root)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return DEFAULT_TEMPLATE


def validate_default_template(source: object) -> str:
    if not isinstance(source, str):
        raise ValueError("缺省源必须是文本")
    if "\x00" in source:
        raise ValueError("缺省源不能包含 NUL 字符")
    if len(source.encode("utf-8")) > DEFAULT_TEMPLATE_MAX_BYTES:
        raise ValueError(f"缺省源不能超过 {DEFAULT_TEMPLATE_MAX_BYTES // 1024} KiB")
    return source


def save_default_template(root: str | Path, source: str) -> Path:
    path = global_template_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix="template-", suffix=".cpp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(source.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def start_problem(
    root: str | Path,
    problem: str | ProblemRef,
    *,
    with_stress: bool = False,
    today: date | None = None,
) -> StartResult:
    """Create or reuse today's solution without overwriting user files."""
    root_path = Path(root).resolve()
    ref = parse_problem_ref(problem)
    current_date = today or date.today()
    day_dir = root_path / str(current_date.year) / str(current_date.month) / str(current_date.day)
    day_dir.mkdir(parents=True, exist_ok=True)

    source = day_dir / f"{ref.problem_id}.cpp"
    reused = source.exists()
    template_source = day_dir / "template.cpp"
    chosen_template: Path | None = template_source if template_source.is_file() else None
    if chosen_template is None:
        global_template = global_template_path(root_path)
        if global_template.is_file():
            chosen_template = global_template
    if not reused:
        if chosen_template:
            source.write_bytes(chosen_template.read_bytes())
        else:
            source.write_text(DEFAULT_TEMPLATE, encoding="utf-8", newline="")

    brute_force: Path | None = None
    generator: Path | None = None
    if with_stress:
        brute_force = day_dir / f"{ref.problem_id}.bf.cpp"
        generator = day_dir / f"{ref.problem_id}.gen.cpp"
        if not brute_force.exists():
            brute_force.write_text(DEFAULT_TEMPLATE, encoding="utf-8", newline="")
        if not generator.exists():
            generator.write_text(STRESS_TEMPLATE, encoding="utf-8", newline="")

    return StartResult(
        problem=ref,
        source=source.resolve(),
        reused=reused,
        template_source=chosen_template.resolve() if chosen_template else None,
        brute_force=brute_force.resolve() if brute_force else None,
        generator=generator.resolve() if generator else None,
    )


__all__ = [
    "DEFAULT_TEMPLATE",
    "LocalSolution",
    "ProblemRef",
    "StartResult",
    "find_solution",
    "global_template_path",
    "load_default_template",
    "parse_problem_ref",
    "save_default_template",
    "scan_local_solutions",
    "start_problem",
    "validate_default_template",
]
