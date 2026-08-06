from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

from tools.acm_agent.stress import SandboxLimits, WindowsAppContainerBackend


@unittest.skipUnless(
    os.name == "nt" and os.environ.get("RUN_APPCONTAINER_SMOKE") == "1" and shutil.which("g++"),
    "set RUN_APPCONTAINER_SMOKE=1 on Windows with g++ to exercise the native sandbox",
)
class AppContainerSmokeTests(unittest.TestCase):
    def test_bundled_launcher_executes_static_cpp_with_bounded_io(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "echo.cpp"
            executable = root / "echo.exe"
            source.write_text(
                "#include <iostream>\n#include <string>\n"
                "int main(int c,char**v){std::string mode=c>1?v[1]:\"missing\";"
                "if(mode==\"loop\"){for(;;){}}"
                "if(mode==\"spam\"){for(int i=0;i<4096;++i)std::cout<<'x';return 0;}"
                "std::string s;std::getline(std::cin,s);"
                "std::cout<<(c>1?v[1]:\"missing\")<<':'<<s<<'\\n';}\n",
                encoding="utf-8",
            )
            compiled = subprocess.run(
                ["g++", "-std=c++17", "-O2", "-static", str(source), "-o", str(executable)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                shell=False,
                timeout=30,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr.decode(errors="replace"))
            work = root / "sandbox"
            work.mkdir()
            backend = WindowsAppContainerBackend(root=root)
            self.assertTrue(backend.probe().available)
            result = backend.run(
                [str(executable), "small"],
                cwd=work,
                input_data=b"42\n",
                limits=SandboxLimits(timeout_seconds=2),
            )
            self.assertTrue(result.ok, result)
            self.assertEqual(result.stdout.replace(b"\r\n", b"\n"), b"small:42\n")

            timed_out = backend.run(
                [str(executable), "loop"],
                cwd=work,
                limits=SandboxLimits(timeout_seconds=0.1),
            )
            self.assertTrue(timed_out.timed_out, timed_out)

            limited = backend.run(
                [str(executable), "spam"],
                cwd=work,
                limits=SandboxLimits(timeout_seconds=2, stdout_bytes=128),
            )
            self.assertTrue(limited.output_limited, limited)
            self.assertLessEqual(len(limited.stdout), 128)

            cancelled = []
            worker = threading.Thread(
                target=lambda: cancelled.append(
                    backend.run(
                        [str(executable), "loop"],
                        cwd=work,
                        limits=SandboxLimits(timeout_seconds=5),
                    )
                )
            )
            worker.start()
            time.sleep(0.15)
            backend.cancel()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(cancelled[0].returncode, 130)


if __name__ == "__main__":
    unittest.main()
