from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tools" / "acm-python313-windows.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")


def run_powershell(script: str) -> subprocess.CompletedProcess[str]:
    if not POWERSHELL:
        raise unittest.SkipTest("PowerShell is not available")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def helper_script(body: str) -> str:
    quoted = str(HELPER).replace("'", "''")
    return f". '{quoted}'\n$ErrorActionPreference = 'Stop'\n{body}"


def parse_last_json(completed: subprocess.CompletedProcess[str]) -> object:
    if completed.returncode != 0:
        raise AssertionError(completed.stdout + completed.stderr)
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError(completed.stdout + completed.stderr)
    return json.loads(lines[-1])


class WindowsLauncherTests(unittest.TestCase):
    def test_installer_metadata_is_pinned_for_all_windows_architectures(self) -> None:
        completed = run_powershell(
            helper_script(
                "@('X64', 'Arm64', 'X86') | ForEach-Object { "
                "Get-AcmPython313InstallerSpec -Architecture $_ "
                "} | ConvertTo-Json -Compress"
            )
        )
        specs = parse_last_json(completed)
        expected = {
            "python-3.13.15-amd64.exe": "edec09c4853aeae9ac36efb8c9f95b6b8e2fee65eee56d9767a8b7c69c574403",
            "python-3.13.15-arm64.exe": "c252c676087c49e6b94e95a273536b78921c28a5fc9f86d15d25392328247249",
            "python-3.13.15.exe": "741c07276eb2d57e7ee012d643f021c58cb38d11c5389be46c15d41d1a10b447",
        }
        self.assertEqual({item["FileName"]: item["Sha256"] for item in specs}, expected)
        for item in specs:
            self.assertIn("mirrors.huaweicloud.com", item["Urls"][0])
            self.assertIn("www.python.org", item["Urls"][1])

    def test_probe_accepts_final_python_310_or_newer_and_core_libraries(self) -> None:
        if sys.version_info[:2] < (3, 10) or sys.version_info.releaselevel != "final":
            self.skipTest("This assertion requires a final Python 3.10+ test runtime")
        executable = str(Path(sys.executable)).replace("'", "''")
        completed = run_powershell(
            helper_script(
                f"$candidate = [pscustomobject]@{{ FilePath = '{executable}'; PrefixArgs = @() }}\n"
                "$result = Test-AcmPythonCandidate $candidate\n"
                "$result | ConvertTo-Json -Compress"
            )
        )
        result = parse_last_json(completed)
        self.assertEqual(Path(result["Path"]).resolve(), Path(sys.executable).resolve())
        self.assertIsInstance(result["Tkinter"], bool)

    def test_compatible_python_skips_the_313_install_fallback(self) -> None:
        if sys.version_info[:2] < (3, 10) or sys.version_info.releaselevel != "final":
            self.skipTest("This assertion requires a final Python 3.10+ test runtime")
        executable = str(Path(sys.executable)).replace("'", "''")
        completed = run_powershell(
            helper_script(
                f"$candidate = [pscustomobject]@{{ FilePath = '{executable}'; PrefixArgs = @() }}\n"
                "$script:installs = 0\n"
                "$result = Ensure-AcmPython -Candidates @($candidate) "
                "-InstallAction { $script:installs++; return $true }\n"
                "@{ result = $result; installs = $script:installs } | ConvertTo-Json -Compress"
            )
        )
        result = parse_last_json(completed)
        self.assertEqual(Path(result["result"]).resolve(), Path(sys.executable).resolve())
        self.assertEqual(result["installs"], 0)

    def test_store_alias_is_rejected_and_system_drive_location_is_probed(self) -> None:
        completed = run_powershell(
            helper_script(
                "$env:SystemDrive = [System.IO.Path]::GetTempPath().TrimEnd([System.IO.Path]::DirectorySeparatorChar)\n"
                "$script:expectedSystemPython = Join-Path $env:SystemDrive 'Python310\\python.exe'\n"
                "function Test-Path { param([string] $LiteralPath, [string] $PathType) "
                "return $LiteralPath -ieq $script:expectedSystemPython }\n"
                "$alias = Test-AcmStoreAlias 'C:\\Users\\me\\AppData\\Local\\Microsoft\\WindowsApps\\python.exe'\n"
                "$hasSystemDrive = [bool]((Get-AcmPythonCandidates) | "
                "Where-Object { $_.FilePath -ieq $script:expectedSystemPython })\n"
                "@{ alias = $alias; systemDrive = $hasSystemDrive } | ConvertTo-Json -Compress"
            )
        )
        self.assertEqual(parse_last_json(completed), {"alias": True, "systemDrive": True})

    def test_prompt_retries_invalid_input_then_declines_without_installing(self) -> None:
        completed = run_powershell(
            helper_script(
                "$script:answers = [System.Collections.Generic.Queue[string]]::new()\n"
                "$script:answers.Enqueue('maybe'); $script:answers.Enqueue('N')\n"
                "$script:installs = 0\n"
                "$result = Ensure-AcmPython -Candidates @() "
                "-ReadAnswer { param($prompt) $script:answers.Dequeue() } "
                "-InstallAction { $script:installs++; return $true }\n"
                "@{ result = $result; installs = $script:installs; remaining = $script:answers.Count } "
                "| ConvertTo-Json -Compress"
            )
        )
        self.assertEqual(
            parse_last_json(completed),
            {"result": None, "installs": 0, "remaining": 0},
        )
        self.assertIn("Please enter y", completed.stdout)

    def test_prompt_treats_eof_as_safe_exit(self) -> None:
        completed = run_powershell(
            helper_script(
                "$result = Ensure-AcmPython -Candidates @() -ReadAnswer { param($prompt) return $null }\n"
                "@{ result = $result } | ConvertTo-Json -Compress"
            )
        )
        self.assertEqual(parse_last_json(completed), {"result": None})
        self.assertIn("No interactive input is available", completed.stdout)

    def test_download_falls_back_and_requires_expected_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            destination_path = Path(temp_directory) / "python.exe"
            destination = str(destination_path).replace("'", "''")
            payload_hash = hashlib.sha256(b"verified-payload").hexdigest()
            completed = run_powershell(
                helper_script(
                    f"$destination = '{destination}'\n"
                    f"$spec = [pscustomobject]@{{ Sha256 = '{payload_hash}'; Urls = @('mirror', 'official') }}\n"
                    "$script:calls = 0\n"
                    "$result = Save-AcmPython313Installer -Spec $spec -Destination $destination -Downloader { "
                    "param($uri, $outFile); $script:calls++; "
                    "if ($uri -eq 'mirror') { throw 'mirror unavailable' }; "
                    "[IO.File]::WriteAllBytes($outFile, [Text.Encoding]::UTF8.GetBytes('verified-payload')) }\n"
                    "@{ result = $result; calls = $script:calls } | ConvertTo-Json -Compress"
                )
            )
            result = parse_last_json(completed)
            self.assertEqual(result["calls"], 2)
            self.assertEqual(Path(result["result"]), destination_path)

    def test_hash_mismatch_rejects_every_source_and_removes_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            destination = str(Path(temp_directory) / "python.exe").replace("'", "''")
            completed = run_powershell(
                helper_script(
                    f"$destination = '{destination}'\n"
                    "$spec = [pscustomobject]@{ Sha256 = ('0' * 64); Urls = @('mirror', 'official') }\n"
                    "$result = Save-AcmPython313Installer -Spec $spec -Destination $destination -Downloader { "
                    "param($uri, $outFile); [IO.File]::WriteAllText($outFile, 'bad') }\n"
                    "@{ result = $result; exists = (Test-Path -LiteralPath $destination) } | ConvertTo-Json -Compress"
                )
            )
            self.assertEqual(parse_last_json(completed), {"result": None, "exists": False})

    def test_install_is_per_user_and_cleans_unique_temp_directory(self) -> None:
        payload_hash = hashlib.sha256(b"installer").hexdigest()
        completed = run_powershell(
            helper_script(
                f"$spec = [pscustomobject]@{{ FileName = 'python.exe'; Sha256 = '{payload_hash}'; Urls = @('mirror') }}\n"
                "$script:installerDirectory = $null; $script:arguments = @()\n"
                "$ok = Install-AcmPython313 -Spec $spec "
                "-Downloader { param($uri, $outFile); [IO.File]::WriteAllBytes($outFile, [Text.Encoding]::UTF8.GetBytes('installer')) } "
                "-InstallerRunner { param($path, $arguments); $script:installerDirectory = Split-Path -Parent $path; "
                "$script:arguments = @($arguments); return 0 }\n"
                "@{ ok = $ok; tempRemoved = -not (Test-Path -LiteralPath $script:installerDirectory); "
                "arguments = $script:arguments } | ConvertTo-Json -Compress"
            )
        )
        result = parse_last_json(completed)
        self.assertIs(result["ok"], True)
        self.assertIs(result["tempRemoved"], True)
        self.assertIn("InstallAllUsers=0", result["arguments"])
        self.assertIn("PrependPath=1", result["arguments"])
        self.assertIn("Include_launcher=1", result["arguments"])
        self.assertIn("Include_test=0", result["arguments"])

    def test_acm_wrapper_gates_web_even_after_hidden_root_option(self) -> None:
        wrapper = (ROOT / "acm.ps1").read_text(encoding="utf-8")
        self.assertIn("$AcmArgs[$index] -ieq '--root'", wrapper)
        self.assertIn("$AcmArgs[$index] -ilike '--root=*'", wrapper)
        self.assertIn("if ($commandName -ieq 'web')", wrapper)
        self.assertLess(
            wrapper.index("if ($commandName -ieq 'web')"),
            wrapper.index("Ensure-AcmPython"),
        )

    def test_candidate_discovery_covers_supported_minor_versions(self) -> None:
        helper = HELPER.read_text(encoding="utf-8")
        for command in ("python3.14", "python3.13", "python3.12", "python3.11", "python3.10"):
            self.assertIn(command, helper)
        self.assertIn("@('-3', '-3.14', '-3.13', '-3.12', '-3.11', '-3.10')", helper)

    def test_cmd_launcher_delegates_to_the_canonical_web_command(self) -> None:
        launcher = (ROOT / "start-acm-web.cmd").read_text(encoding="utf-8")
        self.assertIn('acm.ps1" web', launcher)


if __name__ == "__main__":
    unittest.main()
