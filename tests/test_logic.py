from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from qsh.programs import Result
from qsh.shell import Shell


class LogicTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "data").write_text("hello\nworld\n")
        (self.root / "fallback").write_text("fallback\n")
        (self.root / "sub").mkdir()
        self.shell = Shell(self.root)

    def test_success_failure_and_no_match(self):
        self.assertEqual(self.shell.execute("grep -q hello data && cat fallback"), "（无输出）\nfallback")
        self.assertEqual(self.shell.execute("grep -q absent data && cat fallback"), "（无匹配）")
        self.assertEqual(self.shell.execute("grep -q hello data || cat fallback"), "（无输出）")
        self.assertEqual(self.shell.execute("grep -q absent data || cat fallback"), "（无匹配）\nfallback")
        self.assertIn("fallback", self.shell.execute("cat missing || cat fallback"))

    def test_left_association_and_pipeline_status(self):
        self.assertEqual(self.shell.execute("cat fallback || cat missing && pwd"), f"fallback\n{self.root}")
        self.assertEqual(self.shell.execute("grep absent data && cat missing || cat fallback"), "（无匹配）\nfallback")
        self.assertEqual(self.shell.execute("cat data|grep hello&&cat fallback"), "hello\nfallback")
        self.assertEqual(self.shell.execute("cat data|grep absent||cat fallback"), "（无匹配）\nfallback")
        # Upstream failure is diagnostic; default pipeline status is the final
        # cat's successful status, as in a shell without pipefail.
        self.assertIn("fallback", self.shell.execute("cat missing | cat && cat fallback"))
        self.assertNotIn("fallback", self.shell.execute("cat missing | cat || cat fallback"))

    def test_cd_and_failure_recovery(self):
        self.assertEqual(self.shell.execute("cd sub && pwd"), f"{self.root / 'sub'}\n{self.root / 'sub'}")
        result = self.shell.execute("cd missing && cat missing || pwd")
        self.assertIn("错误", result)
        self.assertTrue(result.endswith(str(self.root / "sub")))
        self.assertEqual(self.shell.cwd, self.root / "sub")
        self.shell.execute("pwd || cd ..")
        self.assertEqual(self.shell.cwd, self.root / "sub")

    def test_literal_operators_and_prevalidation(self):
        (self.root / "special").write_text("a&&b\na||b\n")
        self.assertEqual(self.shell.execute('grep -F "a&&b" special'), "a&&b")
        self.assertEqual(self.shell.execute(r'grep -F a\|\|b special'), "a||b")
        for command in ("&& pwd", "pwd &&", "pwd ||| cat", "pwd && && cat", "pwd & cat"):
            self.assertTrue(self.shell.execute(command).startswith("错误"), command)
        with patch.object(self.shell.programs, "execute") as execute:
            self.assertIn("未知命令", self.shell.execute("cd sub && pwd || sh"))
            execute.assert_not_called()
        self.assertEqual(self.shell.cwd, self.root)

    def test_interruption_stops_expression_but_normal_failures_recover(self):
        with patch.object(self.shell.programs, "execute", return_value=Result("timeout", 124, True)) as execute:
            self.assertEqual(self.shell.execute("cat data || cat fallback"), "timeout")
            self.assertEqual(execute.call_count, 1)
        with patch.object(self.shell.programs, "execute", side_effect=[Result("failed", 2), Result("recovered", 0)]):
            self.assertEqual(self.shell.execute("cat data || cat fallback"), "failed\nrecovered")
        with patch.dict(self.shell.programs.paths, {"grep": None}):
            self.assertIn("fallback", self.shell.execute("grep hello data || cat fallback"))


if __name__ == "__main__":
    unittest.main()
