import os
from pathlib import Path
import sys
import subprocess
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from qsh.output import OUTPUT_LIMIT
from qsh.programs import Programs
from qsh.shell import Shell, parse_commands


class ProgramTests(unittest.TestCase):
    def test_pipelines_quoting_and_validation(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a b.txt").write_text("hello|world\nother\nhello again\n")
            shell = Shell(root)
            self.assertEqual(shell.execute('cat "a b.txt"|grep hello|grep again'), "hello again")
            self.assertEqual(shell.execute(r'cat "a b.txt" | grep -F "hello|world"'), "hello|world")
            self.assertEqual(shell.execute(r'cat "a b.txt" | grep -F hello\|world'), "hello|world")
            self.assertEqual(shell.execute('ls | grep "a b"'), "a b.txt")
            self.assertEqual(shell.execute("pwd|cat"), str(root.resolve()))
            self.assertIn("cat=", shell.execute("cat missing | grep hello"))
            self.assertEqual(shell.execute('cat "a b.txt" | grep absent'), "（无匹配）")
            for command in ('| ls', 'ls |', 'ls ||', 'ls &&', 'ls > output', 'cd / | cat'):
                self.assertTrue(shell.execute(command).startswith("错误"), command)
            with patch("qsh.programs.subprocess.Popen") as popen:
                self.assertIn("未知命令", shell.execute("ls | sh"))
                popen.assert_not_called()
            self.assertEqual(parse_commands('grep "" "a b.txt"'), [(None, [["grep", "", "a b.txt"]])])

    def test_pipeline_streaming_limits_and_cleanup(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            programs = Programs()
            programs.paths["test"] = sys.executable
            # Large intermediate output must stream through pipes, not be
            # truncated before a downstream filter gets its match.
            result = programs.execute([
                ["test", "-c", "print('x\\n' * 100000 + 'needle')"],
                ["grep", "needle"], ["cat"],
            ], root).output
            self.assertEqual(result, "needle")
            # Stderr is displayed, never passed as input to the next stage.
            result = programs.execute([
                ["test", "-c", "import sys; print('diagnostic', file=sys.stderr)"],
                ["grep", "diagnostic"],
            ], root).output
            self.assertEqual(result, "diagnostic")
            with patch("qsh.programs.TIMEOUT", 0.1):
                result = programs.execute([
                    ["test", "-c", "import time; time.sleep(30)"], ["cat"],
                ], root).output
            self.assertIn("超时", result)
            result = programs.execute([
                ["test", "-c", "import sys\nwhile True: print('x' * 10000, flush=True)"],
                ["cat"],
            ], root).output
            self.assertIn("截断", result)
            self.assertLessEqual(len(result), OUTPUT_LIMIT)

    def test_partial_launch_failure_reaps_started_processes(self):
        with TemporaryDirectory() as temp:
            programs = Programs()
            programs.paths.update(test=sys.executable, missing=str(Path(temp) / "missing"))
            started = []
            real_popen = subprocess.Popen

            def spawn(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                started.append(process)
                return process

            with patch("qsh.programs.subprocess.Popen", side_effect=spawn):
                with self.assertRaises(FileNotFoundError):
                    programs.execute([
                        ["test", "-c", "import time; time.sleep(30)"], ["missing"],
                    ], Path(temp))
            self.assertEqual(len(started), 1)
            self.assertIsNotNone(started[0].poll())
            self.assertTrue(started[0].stdout.closed)
            self.assertTrue(started[0].stderr.closed)

    def test_commands_use_session_directory(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / "sub").mkdir()
            (root / "sub" / "a b.txt").write_text("hello world\nother\nhello again\n")
            shell = Shell(root)
            self.assertEqual(shell.execute("pwd"), str(root))
            shell.execute("cd sub")
            self.assertEqual(shell.execute("pwd"), str(root / "sub"))
            self.assertEqual(shell.execute('grep -n "hello" "a b.txt"'), "1:hello world\n3:hello again")
            self.assertEqual(shell.execute('grep absent "a b.txt"'), "（无匹配）")
            self.assertIn("退出码", shell.execute("grep hello missing.txt"))
            self.assertIn(str(os.getpid()), shell.execute(f"ps -p {os.getpid()} -o pid="))
            self.assertIn("未知命令", shell.execute("sh -c ls"))
            shell.execute('grep hello "a b.txt" ; touch PWNED')
            self.assertFalse((root / "sub" / "PWNED").exists())
            self.assertIn("未知命令", shell.execute("/bin/ps"))

    def test_limits_and_environment(self):
        programs = Programs()
        # A trusted test-only entry exercises the runner; python is not allowed
        # by the actual application's fixed program list.
        programs.paths["test"] = sys.executable
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.dict(os.environ, {"QQ_SECRET": "do-not-inherit", "QQ_APPID": "private"}):
                result = programs.execute([["test", "-c", "import os; print('QQ_SECRET' in os.environ, 'QQ_APPID' in os.environ)"]], root).output
            self.assertEqual(result, "False False")
            result = programs.execute([["test", "-c", "import sys; print(sys.stdin.read() == '')"]], root).output
            self.assertEqual(result, "True")
            result = programs.execute([["test", "-c", "print('x' * 10000000)"]], root).output
            self.assertLessEqual(len(result), OUTPUT_LIMIT)
            self.assertIn("截断", result)
            with patch("qsh.programs.TIMEOUT", 0.1):
                start = time.monotonic()
                result = programs.execute([["test", "-c", "import time; time.sleep(30)"]], root).output
                self.assertLess(time.monotonic() - start, 3)
                self.assertIn("超时", result)
            self.assertEqual(programs.execute([["test", "-c", "pass"]], root).output, "（无输出）")

    def test_system_program_resolution_ignores_cwd_and_path(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            fake = root / "grep"
            fake.write_text("#!/bin/sh\necho intercepted\n")
            fake.chmod(0o755)
            (root / "data").write_text("expected\n")
            with patch.dict(os.environ, {"PATH": temp}):
                shell = Shell(root)
                self.assertNotEqual(shell.programs.paths["grep"], str(fake))
                self.assertEqual(shell.execute("grep expected data"), "expected")


if __name__ == "__main__":
    unittest.main()
