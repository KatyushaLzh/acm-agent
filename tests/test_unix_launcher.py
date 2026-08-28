from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER_SOURCE = REPO_ROOT / "tools" / "ensure-python313.sh"
SECRET_SERVICE_HELPER_SOURCE = REPO_ROOT / "tools" / "ensure-linux-secret-service.sh"
LOCK_SOURCE = REPO_ROOT / "tools" / "requirements-web-unix.lock"
ACM_SOURCE = REPO_ROOT / "acm.sh"
START_SOURCE = REPO_ROOT / "start-acm-web.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def _fake_uv_script(version: str = "0.12.5") -> str:
    return (
        textwrap.dedent(
            r"""
            #!/bin/sh
            printf '%s\n' "$*" >> "$FAKE_UV_LOG"
            if [ "$1" = "--version" ]; then
                printf '%s\n' 'uv __UV_VERSION__'
                exit 0
            fi
            if [ "$1" = python ] && [ "$2" = install ]; then
                case "$*" in
                    *registry.npmmirror.com*)
                        [ "${FAKE_FAIL_PYTHON_MIRROR-0}" = 1 ] && exit 1
                        ;;
                    *github.com/astral-sh/python-build-standalone*)
                        [ "${FAKE_FAIL_PYTHON_OFFICIAL-0}" = 1 ] && exit 1
                        ;;
                esac
                python_dir=$UV_PYTHON_INSTALL_DIR/cpython-3.13.15-test/bin
                mkdir -p "$python_dir"
                cat > "$python_dir/python3.13" <<'PYEOF'
            #!/bin/sh
            case "$*" in
                *"import tkinter"*) exit "${FAKE_TKINTER_STATUS-0}" ;;
                *hashlib*) printf '%064d\n' 1; exit 0 ;;
            esac
            printf '%s\n' "$0"
            PYEOF
                chmod 755 "$python_dir/python3.13"
                exit 0
            fi
            if [ "$1" = python ] && [ "$2" = find ]; then
                printf '%s\n' "$UV_PYTHON_INSTALL_DIR/cpython-3.13.15-test/bin/python3.13"
                exit 0
            fi
            if [ "$1" = venv ]; then
                environment_dir=$2
                mkdir -p "$environment_dir/bin"
                cat > "$environment_dir/bin/python" <<'PYEOF'
            #!/bin/sh
            case "$*" in
                *hashlib*) printf '%064d\n' 1; exit 0 ;;
            esac
            exit "${FAKE_WEB_VALIDATE_STATUS-0}"
            PYEOF
                chmod 755 "$environment_dir/bin/python"
                exit 0
            fi
            if [ "$1" = pip ] && [ "$2" = sync ]; then
                exit "${FAKE_FAIL_WEB_SYNC-0}"
            fi
            exit 1
            """
        ).lstrip().replace("__UV_VERSION__", version)
    )


@unittest.skipUnless(os.name != "nt" and shutil.which("sh"), "requires a POSIX shell")
class UnixLauncherBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        (self.root / "tools").mkdir()
        shutil.copy2(HELPER_SOURCE, self.root / "tools" / HELPER_SOURCE.name)
        shutil.copy2(LOCK_SOURCE, self.root / "tools" / LOCK_SOURCE.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _env(self, **updates: str) -> dict[str, str]:
        env = os.environ.copy()
        env.update(updates)
        env["PATH"] = f"{self.fake_bin}{os.pathsep}{env['PATH']}"
        if hasattr(self, "fake_uv_payload"):
            env["FAKE_UV_PAYLOAD"] = str(self.fake_uv_payload)
        return env

    def _run_helper(self, input_text: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(self.root / "tools" / HELPER_SOURCE.name), str(self.root)],
            input=input_text,
            text=True,
            capture_output=True,
            env=self._env(**env),
            check=False,
        )

    def _reject_system_pythons(self) -> None:
        for name in ("python3.14", "python3.13", "python3.12", "python3.11", "python3.10", "python3", "python"):
            _write_executable(self.fake_bin / name, "#!/bin/sh\nexit 1\n")

    def _install_fakes(self, *, global_uv_version: str | None = "0.11.0") -> tuple[Path, Path]:
        self._reject_system_pythons()
        download_log = self.root / "downloads.log"
        uv_log = self.root / "uv.log"
        self.fake_uv_payload = self.root / "fake-uv-payload"
        _write_executable(self.fake_uv_payload, _fake_uv_script())
        if global_uv_version is not None:
            _write_executable(
                self.fake_bin / "uv",
                r"""
                #!/bin/sh
                printf 'global:%s\n' "$*" >> "$FAKE_UV_LOG"
                if [ "$1" = "--version" ]; then
                    printf 'uv %s\n' "${FAKE_GLOBAL_UV_VERSION-0.11.0}"
                    exit 0
                fi
                if [ "${FAKE_GLOBAL_UV_VERSION-0.11.0}" = 0.12.5 ]; then
                    exec "$FAKE_UV_PAYLOAD" "$@"
                fi
                exit 97
                """,
            )
        _write_executable(
            self.fake_bin / "curl",
            r"""
            #!/bin/sh
            output=
            url=
            while [ "$#" -gt 0 ]; do
                case "$1" in
                    --output) shift; output=$1 ;;
                    https://*) url=$1 ;;
                esac
                shift
            done
            printf '%s\n' "$url" >> "$FAKE_DOWNLOAD_LOG"
            case "$url" in
                *uv.agentsmirror.com*)
                    [ "${FAKE_FAIL_UV_MIRROR-0}" = 1 ] && exit 22
                    printf '%s' domestic > "$output"
                    ;;
                *github.com/astral-sh/uv*)
                    [ "${FAKE_FAIL_UV_OFFICIAL-0}" = 1 ] && exit 22
                    printf '%s' official > "$output"
                    ;;
                *) exit 22 ;;
            esac
            """,
        )
        _write_executable(
            self.fake_bin / "sha256sum",
            r"""
            #!/bin/sh
            if [ "${FAKE_BAD_DOMESTIC_HASH-0}" = 1 ] && [ "$(cat "$1")" = domestic ]; then
                printf '%064d  %s\n' 0 "$1"
            else
                printf '%s  %s\n' "$FAKE_UV_HASH" "$1"
            fi
            """,
        )
        _write_executable(
            self.fake_bin / "tar",
            r"""
            #!/bin/sh
            destination=
            while [ "$#" -gt 0 ]; do
                case "$1" in
                    -C) shift; destination=$1 ;;
                esac
                shift
            done
            uv_dir=$destination/uv-$FAKE_UV_TARGET
            mkdir -p "$uv_dir"
            cp "$FAKE_UV_PAYLOAD" "$uv_dir/uv"
            chmod 755 "$uv_dir/uv"
            """,
        )
        return download_log, uv_log

    def _linux_bootstrap_env(self, download_log: Path, uv_log: Path, **updates: str) -> dict[str, str]:
        env = {
            "ACM_BOOTSTRAP_UNAME_S": "Linux",
            "ACM_BOOTSTRAP_UNAME_M": "x86_64",
            "ACM_BOOTSTRAP_LIBC": "gnu",
            "FAKE_UV_TARGET": "x86_64-unknown-linux-gnu",
            "FAKE_UV_HASH": "68a509da24b06b4223a1c0175fb5eb5bc79342b76cbeff0cfe51ac3f5b17b6b2",
            "FAKE_DOWNLOAD_LOG": str(download_log),
            "FAKE_UV_LOG": str(uv_log),
        }
        env.update(updates)
        return env

    def test_accepts_final_python_310_or_newer_and_warns_without_tkinter(self) -> None:
        download_log, uv_log = self._install_fakes()
        probe_log = self.root / "probe.log"
        _write_executable(
            self.fake_bin / "python3.10",
            r"""
            #!/bin/sh
            printf '%s\n' "$*" >> "$FAKE_PROBE_LOG"
            case "$*" in
                *"import tkinter"*) exit 1 ;;
                *hashlib*) printf '%064d\n' 1; exit 0 ;;
            esac
            printf '%s\n' "$0"
            """,
        )
        result = self._run_helper(
            "",
            ACM_BOOTSTRAP_UNAME_S="Linux",
            ACM_BOOTSTRAP_UNAME_M="x86_64",
            ACM_BOOTSTRAP_LIBC="gnu",
            FAKE_UV_TARGET="x86_64-unknown-linux-gnu",
            FAKE_UV_HASH="68a509da24b06b4223a1c0175fb5eb5bc79342b76cbeff0cfe51ac3f5b17b6b2",
            FAKE_DOWNLOAD_LOG=str(download_log),
            FAKE_UV_LOG=str(uv_log),
            FAKE_PROBE_LOG=str(probe_log),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("/web-envs/", result.stdout.strip())
        self.assertIn("native file picker operations will be unavailable", result.stderr)
        probe = probe_log.read_text(encoding="utf-8")
        self.assertIn("releaselevel", probe)
        self.assertIn("sys.version_info[:2] < (3, 10)", probe)
        self.assertIn("sys.implementation.cache_tag", probe)
        self.assertIn("import sqlite3", probe)
        self.assertIn("import ssl", probe)
        uv_calls = uv_log.read_text(encoding="utf-8")
        self.assertIn("pip sync", uv_calls)
        self.assertIn("--require-hashes", uv_calls)
        self.assertIn("--only-binary=:all:", uv_calls)
        self.assertIn("--no-python-downloads", uv_calls)

    def test_prompt_retries_and_no_or_eof_cancels_without_writes(self) -> None:
        self._reject_system_pythons()
        declined = self._run_helper("maybe\nN\n")
        self.assertNotEqual(declined.returncode, 0)
        self.assertIn("Please enter y or n", declined.stderr)
        self.assertIn("Web startup cancelled", declined.stderr)
        self.assertFalse((self.root / ".acm").exists())

        eof = self._run_helper("")
        self.assertNotEqual(eof.returncode, 0)
        self.assertIn("No input received", eof.stderr)
        self.assertFalse((self.root / ".acm").exists())

    def test_all_supported_targets_use_pinned_verified_assets(self) -> None:
        targets = {
            ("Darwin", "arm64", "gnu"): (
                "aarch64-apple-darwin",
                "5bb0e5fe008a773c3dbcb97ff79cd89e1241464fe9d2f986d52ad8f1b037bd62",
            ),
            ("Darwin", "x86_64", "gnu"): (
                "x86_64-apple-darwin",
                "b3b2137477cf96c9686ebfb71524614cec780c673fd73e59bce099aef02e70e8",
            ),
            ("Linux", "aarch64", "gnu"): (
                "aarch64-unknown-linux-gnu",
                "9bf43b4d1a07665bf64d4c4e710930b382321a785e0eb10aac07f46471f86a31",
            ),
            ("Linux", "x86_64", "gnu"): (
                "x86_64-unknown-linux-gnu",
                "68a509da24b06b4223a1c0175fb5eb5bc79342b76cbeff0cfe51ac3f5b17b6b2",
            ),
            ("Linux", "aarch64", "musl"): (
                "aarch64-unknown-linux-musl",
                "8767a0e77f2cd45436401b1b42bf7e9ed5a4a91a74a5305d6fe93249d0f6dbc5",
            ),
            ("Linux", "x86_64", "musl"): (
                "x86_64-unknown-linux-musl",
                "a4742988791c9aeae68c78150d6cba762062ad2a47e53738c2779d2b596bfcdb",
            ),
        }
        original_root = self.root
        original_fake_bin = self.fake_bin
        for platform, (target, checksum) in targets.items():
            with self.subTest(platform=platform):
                with tempfile.TemporaryDirectory() as case_dir:
                    case_root = Path(case_dir)
                    (case_root / "fake-bin").mkdir()
                    (case_root / "tools").mkdir()
                    shutil.copy2(HELPER_SOURCE, case_root / "tools" / HELPER_SOURCE.name)
                    shutil.copy2(LOCK_SOURCE, case_root / "tools" / LOCK_SOURCE.name)
                    self.root = case_root
                    self.fake_bin = case_root / "fake-bin"
                    try:
                        download_log, uv_log = self._install_fakes()
                        result = self._run_helper(
                            "y\n",
                            ACM_BOOTSTRAP_UNAME_S=platform[0],
                            ACM_BOOTSTRAP_UNAME_M=platform[1],
                            ACM_BOOTSTRAP_LIBC=platform[2],
                            FAKE_UV_TARGET=target,
                            FAKE_UV_HASH=checksum,
                            FAKE_DOWNLOAD_LOG=str(download_log),
                            FAKE_UV_LOG=str(uv_log),
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn(f"/0.12.5/uv-{target}.tar.gz", download_log.read_text(encoding="utf-8"))
                        self.assertIn("python install 3.13.15", uv_log.read_text(encoding="utf-8"))
                        leftovers = list((case_root / ".acm/runtime/bootstrap").glob(".uv-*.????????"))
                        self.assertEqual(leftovers, [])
                    finally:
                        self.root = original_root
                        self.fake_bin = original_fake_bin

    def test_uv_hash_and_python_mirror_failures_fall_back_to_official(self) -> None:
        download_log, uv_log = self._install_fakes()
        result = self._run_helper(
            "Y\n",
            ACM_BOOTSTRAP_UNAME_S="Linux",
            ACM_BOOTSTRAP_UNAME_M="x86_64",
            ACM_BOOTSTRAP_LIBC="gnu",
            FAKE_UV_TARGET="x86_64-unknown-linux-gnu",
            FAKE_UV_HASH="68a509da24b06b4223a1c0175fb5eb5bc79342b76cbeff0cfe51ac3f5b17b6b2",
            FAKE_BAD_DOMESTIC_HASH="1",
            FAKE_FAIL_PYTHON_MIRROR="1",
            FAKE_DOWNLOAD_LOG=str(download_log),
            FAKE_UV_LOG=str(uv_log),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        downloads = download_log.read_text(encoding="utf-8")
        self.assertIn("uv.agentsmirror.com", downloads)
        self.assertIn("github.com/astral-sh/uv", downloads)
        uv_calls = uv_log.read_text(encoding="utf-8")
        self.assertIn("registry.npmmirror.com/-/binary/python-build-standalone", uv_calls)
        self.assertIn("github.com/astral-sh/python-build-standalone/releases/download", uv_calls)

    def test_matching_project_uv_is_reused_before_global_uv_without_download(self) -> None:
        download_log, uv_log = self._install_fakes()
        project_uv = self.root / ".acm/runtime/bootstrap/uv-0.12.5-x86_64-unknown-linux-gnu"
        project_uv.parent.mkdir(parents=True)
        shutil.copy2(self.fake_uv_payload, project_uv)
        project_uv.chmod(0o755)

        result = self._run_helper("y\n", **self._linux_bootstrap_env(download_log, uv_log))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(download_log.exists())
        self.assertNotIn("global:", uv_log.read_text(encoding="utf-8"))
        self.assertEqual(project_uv.read_bytes(), self.fake_uv_payload.read_bytes())

    def test_matching_global_uv_is_reused_without_project_install_or_path_changes(self) -> None:
        download_log, uv_log = self._install_fakes()
        original_path = os.environ.get("PATH", "")

        result = self._run_helper(
            "y\n",
            **self._linux_bootstrap_env(
                download_log,
                uv_log,
                FAKE_GLOBAL_UV_VERSION="0.12.5",
            ),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(download_log.exists())
        self.assertIn("Reusing global uv 0.12.5", result.stderr)
        uv_calls = uv_log.read_text(encoding="utf-8")
        self.assertIn("global:--version", uv_calls)
        self.assertIn("global:python install 3.13.15", uv_calls)
        self.assertFalse(
            (self.root / ".acm/runtime/bootstrap/uv-0.12.5-x86_64-unknown-linux-gnu").exists()
        )
        self.assertEqual(os.environ.get("PATH", ""), original_path)

    def test_mismatched_global_uv_is_only_probed_then_project_uv_is_downloaded(self) -> None:
        download_log, uv_log = self._install_fakes()
        global_uv = self.fake_bin / "uv"
        original_global_uv = global_uv.read_bytes()

        result = self._run_helper("y\n", **self._linux_bootstrap_env(download_log, uv_log))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ignoring global uv", result.stderr)
        self.assertEqual(global_uv.read_bytes(), original_global_uv)
        uv_calls = uv_log.read_text(encoding="utf-8")
        self.assertIn("global:--version", uv_calls)
        self.assertNotIn("global:python", uv_calls)
        self.assertIn("uv.agentsmirror.com", download_log.read_text(encoding="utf-8"))

    def test_damaged_or_mismatched_project_uv_is_the_only_file_replaced(self) -> None:
        download_log, uv_log = self._install_fakes()
        bootstrap_dir = self.root / ".acm/runtime/bootstrap"
        project_uv = bootstrap_dir / "uv-0.12.5-x86_64-unknown-linux-gnu"
        sentinel = bootstrap_dir / "keep-me"

        for mode in ("wrong-version", "not-executable"):
            with self.subTest(mode=mode):
                shutil.rmtree(self.root / ".acm", ignore_errors=True)
                bootstrap_dir.mkdir(parents=True)
                sentinel.write_text("unchanged", encoding="utf-8")
                if mode == "wrong-version":
                    _write_executable(project_uv, "#!/bin/sh\nprintf '%s\\n' 'uv 0.11.0'\n")
                else:
                    project_uv.write_text("damaged", encoding="utf-8")
                    project_uv.chmod(0o644)
                download_log.write_text("", encoding="utf-8")
                uv_log.write_text("", encoding="utf-8")

                result = self._run_helper(
                    "y\n",
                    **self._linux_bootstrap_env(
                        download_log,
                        uv_log,
                        FAKE_GLOBAL_UV_VERSION="0.12.5",
                    ),
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Replacing the damaged or mismatched project-local uv", result.stderr)
                self.assertEqual(project_uv.read_bytes(), self.fake_uv_payload.read_bytes())
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
                self.assertNotIn("global:", uv_log.read_text(encoding="utf-8"))

    def test_both_uv_sources_failing_aborts_without_using_mismatched_global_uv(self) -> None:
        download_log, uv_log = self._install_fakes()

        result = self._run_helper(
            "y\n",
            **self._linux_bootstrap_env(
                download_log,
                uv_log,
                FAKE_FAIL_UV_MIRROR="1",
                FAKE_FAIL_UV_OFFICIAL="1",
            ),
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Both uv sources failed", result.stderr)
        downloads = download_log.read_text(encoding="utf-8")
        self.assertIn("uv.agentsmirror.com", downloads)
        self.assertIn("github.com/astral-sh/uv", downloads)
        uv_calls = uv_log.read_text(encoding="utf-8")
        self.assertEqual(uv_calls.strip(), "global:--version")
        self.assertNotIn("python install", uv_calls)
        self.assertFalse(
            (self.root / ".acm/runtime/bootstrap/uv-0.12.5-x86_64-unknown-linux-gnu").exists()
        )

    def test_ready_environment_is_reused_without_uv_or_network(self) -> None:
        download_log, uv_log = self._install_fakes()
        common_env = {
            "ACM_BOOTSTRAP_UNAME_S": "Linux",
            "ACM_BOOTSTRAP_UNAME_M": "x86_64",
            "ACM_BOOTSTRAP_LIBC": "gnu",
            "FAKE_UV_TARGET": "x86_64-unknown-linux-gnu",
            "FAKE_UV_HASH": "68a509da24b06b4223a1c0175fb5eb5bc79342b76cbeff0cfe51ac3f5b17b6b2",
            "FAKE_DOWNLOAD_LOG": str(download_log),
            "FAKE_UV_LOG": str(uv_log),
        }
        first = self._run_helper("y\n", **common_env)
        self.assertEqual(first.returncode, 0, first.stderr)
        first_uv_calls = uv_log.read_text(encoding="utf-8")
        first_downloads = download_log.read_text(encoding="utf-8")

        second = self._run_helper("", **common_env)

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout.strip(), first.stdout.strip())
        self.assertEqual(uv_log.read_text(encoding="utf-8"), first_uv_calls)
        self.assertEqual(download_log.read_text(encoding="utf-8"), first_downloads)

    def test_dependency_sync_failure_falls_back_to_base_python(self) -> None:
        download_log, uv_log = self._install_fakes()
        base_python = self.fake_bin / "python3"
        _write_executable(
            base_python,
            r"""
            #!/bin/sh
            case "$*" in
                *"import tkinter"*) exit 0 ;;
                *hashlib*) printf '%064d\n' 1; exit 0 ;;
            esac
            printf '%s\n' "$0"
            """,
        )
        result = self._run_helper(
            "",
            ACM_BOOTSTRAP_UNAME_S="Linux",
            ACM_BOOTSTRAP_UNAME_M="x86_64",
            ACM_BOOTSTRAP_LIBC="gnu",
            FAKE_UV_TARGET="x86_64-unknown-linux-gnu",
            FAKE_UV_HASH="68a509da24b06b4223a1c0175fb5eb5bc79342b76cbeff0cfe51ac3f5b17b6b2",
            FAKE_FAIL_WEB_SYNC="1",
            FAKE_DOWNLOAD_LOG=str(download_log),
            FAKE_UV_LOG=str(uv_log),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(base_python))
        self.assertIn("starting the core Dashboard", result.stderr)
        self.assertIn("retry next time", result.stderr)
        self.assertEqual(list((self.root / ".acm/runtime/web-envs").glob("[0-9a-f]" * 64)), [])

    def test_missing_dependency_lock_falls_back_without_installing(self) -> None:
        (self.root / "tools" / LOCK_SOURCE.name).unlink()
        self._reject_system_pythons()
        base_python = self.fake_bin / "python3.13"
        _write_executable(
            base_python,
            "#!/bin/sh\ncase \"$*\" in *hashlib*) exit 1 ;; esac\nprintf '%s\\n' \"$0\"\n",
        )

        result = self._run_helper("")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(base_python))
        self.assertIn("lock is missing or unreadable", result.stderr)
        self.assertFalse((self.root / ".acm").exists())

    def test_unsupported_unix_target_fails_before_download(self) -> None:
        self._reject_system_pythons()
        result = self._run_helper(
            "y\n",
            ACM_BOOTSTRAP_UNAME_S="FreeBSD",
            ACM_BOOTSTRAP_UNAME_M="x86_64",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported Unix platform", result.stderr)
        self.assertFalse((self.root / ".acm").exists())


class UnixLauncherContractTests(unittest.TestCase):
    def test_web_detection_supports_root_forms_but_not_other_commands(self) -> None:
        source = ACM_SOURCE.read_text(encoding="utf-8")
        self.assertIn('--root) ACM_EXPECT_ROOT=1', source)
        self.assertIn('--root=*)', source)
        self.assertIn('if [ "$ACM_COMMAND" = "web" ]', source)
        self.assertIn('ensure-linux-secret-service.sh', source)
        self.assertIn('exec python3 -m tools.acm_agent "$@"', source)

    def test_double_click_script_delegates_to_canonical_unix_entrypoint(self) -> None:
        source = START_SOURCE.read_text(encoding="utf-8")
        self.assertIn('exec sh "$REPO_ROOT/acm.sh" web', source)


@unittest.skipUnless(os.name != "nt" and shutil.which("sh"), "requires a POSIX shell")
class LinuxSecretServiceLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        self.helper = self.root / SECRET_SERVICE_HELPER_SOURCE.name
        shutil.copy2(SECRET_SERVICE_HELPER_SOURCE, self.helper)
        self.os_release = self.root / "os-release"
        self.os_release.write_text('ID=ubuntu\n', encoding="utf-8")
        self.probe_count = self.root / "probe-count"
        self.sudo_log = self.root / "sudo.log"
        _write_executable(
            self.fake_bin / "python",
            r"""
            #!/bin/sh
            count=$(cat "$FAKE_PROBE_COUNT" 2>/dev/null || printf 0)
            count=$((count + 1))
            printf '%s\n' "$count" > "$FAKE_PROBE_COUNT"
            if [ "$count" -eq 1 ]; then
                exit "${FAKE_FIRST_PROBE_STATUS-1}"
            fi
            exit "${FAKE_SECOND_PROBE_STATUS-1}"
            """,
        )
        _write_executable(self.fake_bin / "apt-get", "#!/bin/sh\nexit 0\n")
        _write_executable(
            self.fake_bin / "sudo",
            r"""
            #!/bin/sh
            printf '%s\n' "$*" >> "$FAKE_SUDO_LOG"
            exit "${FAKE_SUDO_STATUS-0}"
            """,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _run(self, input_text: str, **updates: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update({
            "PATH": f"{self.fake_bin}{os.pathsep}{env['PATH']}",
            "ACM_SECRET_SERVICE_OS_RELEASE_FILE": str(self.os_release),
            "FAKE_PROBE_COUNT": str(self.probe_count),
            "FAKE_SUDO_LOG": str(self.sudo_log),
        })
        env.update(updates)
        return subprocess.run(
            ["sh", str(self.helper), str(self.fake_bin / "python"), str(self.root)],
            input=input_text,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def test_available_service_never_prompts_or_invokes_sudo(self) -> None:
        result = self._run("", FAKE_FIRST_PROBE_STATUS="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.sudo_log.exists())
        self.assertNotIn("执行安装", result.stderr)

    def test_confirmed_install_is_exact_and_success_requires_reprobe(self) -> None:
        result = self._run(
            "y\n", FAKE_FIRST_PROBE_STATUS="1", FAKE_SECOND_PROBE_STATUS="0"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.sudo_log.read_text(encoding="utf-8").strip(),
            "apt-get install --no-install-recommends gnome-keyring seahorse",
        )
        self.assertEqual(self.probe_count.read_text(encoding="utf-8").strip(), "2")
        self.assertIn("已通过 D-Bus 与无敏感数据 keyring 探测", result.stderr)
        self.assertNotIn("apt-get update", result.stderr)

    def test_failed_reprobe_requires_unlock_and_keeps_dashboard_path_successful(self) -> None:
        result = self._run(
            "y\n", FAKE_FIRST_PROBE_STATUS="1", FAKE_SECOND_PROBE_STATUS="1"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("需启动/解锁用户钥匙环", result.stderr)
        self.assertIn("libsecret-tools", result.stderr)
        command = self.sudo_log.read_text(encoding="utf-8")
        self.assertNotIn("libsecret-tools", command)
        self.assertIn("seahorse", command)

    def test_failed_install_does_not_update_or_block_dashboard(self) -> None:
        result = self._run(
            "y\n", FAKE_FIRST_PROBE_STATUS="1", FAKE_SUDO_STATUS="1"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("安装命令失败", result.stderr)
        command = self.sudo_log.read_text(encoding="utf-8")
        self.assertEqual(
            command.strip(),
            "apt-get install --no-install-recommends gnome-keyring seahorse",
        )
        self.assertNotIn("update", command)

    def test_decline_eof_and_non_debian_never_invoke_sudo(self) -> None:
        for label, input_text in (("decline", "n\n"), ("eof", ""), ("non-debian", "y\n")):
            with self.subTest(label=label):
                self.probe_count.unlink(missing_ok=True)
                self.sudo_log.unlink(missing_ok=True)
                self.os_release.write_text(
                    'ID=fedora\n' if label == "non-debian" else 'ID=debian\n',
                    encoding="utf-8",
                )
                result = self._run(input_text)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(self.sudo_log.exists())
                if label == "non-debian":
                    self.assertFalse(self.probe_count.exists())
                    self.assertNotIn("Secret Service 不可用", result.stderr)


class UnixWebDependencyLockTests(unittest.TestCase):
    def test_lock_is_fully_pinned_hashed_and_contains_secure_store_dependencies(self) -> None:
        source = LOCK_SOURCE.read_text(encoding="utf-8")
        expected = {
            "cffi": "2.1.1",
            "cryptography": "49.0.0",
            "jaraco-classes": "3.4.0",
            "jaraco-context": "6.1.2",
            "jaraco-functools": "4.6.0",
            "jeepney": "0.9.0",
            "keyring": "25.7.0",
            "more-itertools": "11.1.0",
            "pycparser": "3.0",
            "secretstorage": "3.5.0",
        }
        for package, version in expected.items():
            with self.subTest(package=package):
                self.assertIn(f"{package}=={version}", source)
        requirement_lines = [
            line for line in source.splitlines() if line and not line.startswith(("#", " "))
        ]
        self.assertEqual(len(requirement_lines), len(expected))
        self.assertNotIn("~=", source)
        self.assertNotIn(">=", source)
        self.assertEqual(source.count("--hash=sha256:"), 16)


if __name__ == "__main__":
    unittest.main()
