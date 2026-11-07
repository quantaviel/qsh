import importlib.util
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("deploy", Path(__file__).resolve().parents[1] / "scripts/deploy.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeployTests(unittest.TestCase):
    def test_sigterm_stops_bot_cleanly(self):
        code = '''
import asyncio, os, signal
from pathlib import Path
from unittest.mock import patch
from qsh.gateway import Gateway, run_bot
async def wait(self):
    asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)
    await asyncio.Event().wait()
with patch.object(Gateway, "run", wait):
    asyncio.run(run_bot("fake", "fake", Path.cwd()))
print("stopped")
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=deploy.PROJECT,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'stopped')

    def test_unit_escaping_and_service_settings(self):
        unit = deploy.render_unit(Path('/opt/qsh project'), Path('/tmp/a $b%.env'), Path('/srv/a "b"'), "bot")
        self.assertIn('"/opt/qsh project/.venv/bin/python" "-u" "-m" "qsh"', unit)
        self.assertIn('WorkingDirectory=/opt/qsh project\n', unit)
        self.assertIn('"/tmp/a $$b%%.env"', unit)
        self.assertIn('"/srv/a \\"b\\""', unit)
        self.assertIn("User=bot\n", unit)
        self.assertIn("Restart=always", unit)
        self.assertIn("KillMode=control-group", unit)
        self.assertNotIn("QQ_SECRET", unit)
        for user in ("root", "bot\nUser=root", ""):
            with self.assertRaises(ValueError):
                deploy.render_unit(Path('/opt/qsh'), Path('/tmp/.env'), Path('/tmp'), user)
        with self.assertRaises(ValueError):
            deploy.unit_quote("/tmp/a\nb")

    def test_install_runs_python_as_service_user_and_restarts(self):
        with TemporaryDirectory() as temp:
            project = Path(temp)
            (project / '.venv/bin').mkdir(parents=True)
            python = project / '.venv/bin/python'
            python.touch()
            env_file = project / '.env'
            env_file.write_text('QQ_APPID=fake\nQQ_SECRET=private-placeholder\n')
            unit_path = project / 'systemd/qsh.service'
            with patch.object(deploy, 'UNIT_PATH', unit_path), \
                 patch.object(deploy.pwd, 'getpwnam', return_value=Mock(pw_uid=1000)), \
                 patch.object(deploy.shutil, 'which', return_value='/usr/sbin/runuser'), \
                 patch.object(deploy, 'run') as run:
                deploy.install(project, env_file, project, 'bot')
            unit = unit_path.read_text()
            self.assertNotIn('private-placeholder', unit)
            calls = [call.args for call in run.call_args_list]
            pip_call = next(call for call in calls if 'pip' in call and 'install' in call)
            self.assertEqual(pip_call[:5], ('/usr/sbin/runuser', '-u', 'bot', '--', python))
            self.assertEqual(calls[-3:], [
                ('systemctl', 'daemon-reload'), ('systemctl', 'enable', 'qsh.service'),
                ('systemctl', 'restart', 'qsh.service'),
            ])
            self.assertEqual(unit_path.stat().st_mode & 0o777, 0o644)

    def test_install_failure_does_not_replace_existing_unit(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            unit = root / 'qsh.service'
            unit.write_text('old unit')
            with patch.object(deploy, 'UNIT_PATH', unit), \
                 patch.object(deploy.pwd, 'getpwnam', return_value=Mock(pw_uid=1000)), \
                 patch.object(deploy, 'run') as run:
                with self.assertRaises(ValueError):
                    deploy.install(root, root / 'missing.env', root, 'bot')
            run.assert_not_called()
            self.assertEqual(unit.read_text(), 'old unit')

    def test_unit_verification_failure_keeps_old_service(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '.venv/bin').mkdir(parents=True)
            (root / '.venv/bin/python').touch()
            env_file = root / '.env'
            env_file.touch()
            unit = root / 'qsh.service'
            unit.write_text('old unit')

            def run(*args):
                if args[0] == 'systemd-analyze':
                    raise subprocess.CalledProcessError(1, args)

            with patch.object(deploy, 'UNIT_PATH', unit), \
                 patch.object(deploy.pwd, 'getpwnam', return_value=Mock(pw_uid=1000)), \
                 patch.object(deploy.shutil, 'which', return_value='/usr/sbin/runuser'), \
                 patch.object(deploy, 'run', side_effect=run):
                with self.assertRaises(subprocess.CalledProcessError):
                    deploy.install(root, env_file, root, 'bot')
            self.assertEqual(unit.read_text(), 'old unit')
            self.assertEqual(list(root.glob('*.service')), [unit])
