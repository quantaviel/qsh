import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from qsh.qq import APIError, QQAPI
from qsh.shell import OUTPUT_LIMIT, Shell


class ShellTests(unittest.TestCase):
    def test_paths_commands_and_isolation(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / "sub dir").mkdir()
            (root / "sub dir" / "中文.txt").write_text("你好", encoding="utf-8")
            (root / ".hidden").touch()
            first, second = Shell(root), Shell(root)
            process_cwd = Path.cwd()
            self.assertNotIn(".hidden", first.execute("ls"))
            self.assertIn(".hidden", first.execute("ls -a"))
            self.assertEqual(first.execute('cd "sub dir"'), str(root / "sub dir"))
            self.assertEqual(first.execute("cat 中文.txt"), "你好")
            self.assertEqual(second.cwd, root)
            self.assertTrue(first.execute("cd missing").startswith("错误"))
            self.assertEqual(first.cwd, root / "sub dir")
            self.assertEqual(first.execute("cd ../.."), str(root.parent))
            self.assertEqual(Path.cwd(), process_cwd)
            self.assertIn("未知命令", second.execute("rm anything"))
            self.assertIn("错误", second.execute('cat "'))
            self.assertFalse((root / "PWNED").exists())
            second.execute("cat x; touch PWNED")
            self.assertFalse((root / "PWNED").exists())

    def test_bounded_system_cat(self):

        with TemporaryDirectory() as temp:
            root = Path(temp)
            shell = Shell(root)
            (root / "large").write_text("界" * 10000, encoding="utf-8")
            result = shell.execute("cat large")
            self.assertLessEqual(len(result), OUTPUT_LIMIT)
            self.assertIn("截断", result)
            (root / "invalid").write_bytes(b"\xe4")
            self.assertIn("�", shell.execute("cat invalid"))
            self.assertIn("退出码", shell.execute("cat missing"))
            self.assertEqual(shell.execute("cat"), "（无输出）")


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_api_token_cache_and_business_errors(self):
        from aiohttp import ClientSession
        requests = []
        token_result = {"access_token": "test-token", "expires_in": "7200"}

        async def endpoint(request):
            requests.append((request.path, dict(request.headers), await request.json()))
            return web.json_response(token_result if request.path.endswith("getAppAccessToken") else {"id": "reply"})

        app = web.Application()
        app.router.add_post("/{path:.*}", endpoint)
        async with TestServer(app) as server:
            from unittest.mock import patch
            async with ClientSession() as session:
                with patch("qsh.qq.API_BASE", str(server.make_url("/")).rstrip("/")):
                    api = QQAPI(session, "123", "secret")
                    await asyncio.gather(api.token(), api.token())
                    await api.reply("c2c", "alice", "message", "result")
                    self.assertEqual(len(requests), 2)
                    self.assertEqual(requests[0][2], {"appId": "123", "clientSecret": "secret"})
                    self.assertEqual(requests[1][0], "/v2/users/alice/messages")
                    self.assertEqual(requests[1][1]["Authorization"], "QQBot test-token")
                    self.assertEqual(requests[1][2]["msg_id"], "message")
                    self.assertEqual(requests[1][2]["msg_seq"], 1)
                    token_result = {"code": 100016, "message": "invalid secret"}
                    api._refresh_at = 0
                    with self.assertRaises(APIError):
                        await api.token()


if __name__ == "__main__":
    unittest.main()
