from contextlib import redirect_stderr
from io import StringIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from qsh.__main__ import main


class ConfigTests(unittest.TestCase):
    def test_explicit_env_file_paths(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config").mkdir()
            env_file = root / "config" / "credentials.env"
            env_file.write_text("QQ_APPID=custom-id\nQQ_SECRET=custom-secret\n", encoding="utf-8")
            for path in ("config/credentials.env", str(env_file)):
                with self.subTest(path=path), patch.dict(os.environ, {}, clear=True), \
                     patch("sys.argv", ["qsh", "--env-file", path, "--cwd", temp]), \
                     patch("qsh.__main__.Path.cwd", return_value=root), \
                     patch("qsh.__main__.create_app") as create, patch("qsh.__main__.web.run_app"):
                    main()
                    self.assertEqual(create.call_args.args[:2], ("custom-id", "custom-secret"))

    def test_explicit_missing_file_is_an_error(self):
        with TemporaryDirectory() as temp, \
             patch("sys.argv", ["qsh", "--env-file", str(Path(temp) / "missing.env"),
                                "--appid", "id", "--secret", "secret"]), \
             patch("qsh.__main__.web.run_app") as run, redirect_stderr(StringIO()) as stderr:
            with self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, 2)
            self.assertIn("--env-file", stderr.getvalue())
            run.assert_not_called()

    def test_dotenv_and_override_priority(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text(
                'QQ_APPID=file-id\nQQ_SECRET="file secret"\n', encoding="utf-8"
            )
            cases = [
                ({}, [], ("file-id", "file secret")),
                ({"QQ_APPID": "env-id", "QQ_SECRET": "env-secret"}, [], ("env-id", "env-secret")),
                ({"QQ_SECRET": "env-secret"}, ["--secret", "cli-secret"], ("file-id", "cli-secret")),
            ]
            for env, argv, expected in cases:
                with self.subTest(env=env, argv=argv), patch.dict(os.environ, env, clear=True), \
                     patch("sys.argv", ["qsh", *argv]), patch("qsh.__main__.Path.cwd", return_value=root), \
                     patch("qsh.__main__.create_app") as create, patch("qsh.__main__.web.run_app"):
                    main()
                    self.assertEqual(create.call_args.args[:2], expected)
                    self.assertEqual(dict(os.environ), env)

    def test_missing_credentials_do_not_start_service(self):
        with TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True), \
             patch("qsh.__main__.Path.cwd", return_value=Path(temp)), \
             patch("sys.argv", ["qsh"]), patch("qsh.__main__.web.run_app") as run, \
             redirect_stderr(StringIO()) as stderr:
            with self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, 2)
            self.assertIn("QQ_APPID", stderr.getvalue())
            run.assert_not_called()
