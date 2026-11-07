import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from qsh.qq import APIError, QQAPI, Signature
from qsh.server import Runtime, create_app
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
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "sub").mkdir()
        self.api = AsyncMock()
        self.runtime = Runtime("123", "test-secret", self.root, self.api)
        app = web.Application()
        app.router.add_post("/qqbot", self.runtime.receive)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def send(self, payload, signed=True, tamper=False):
        body = json.dumps(payload).encode()
        headers = {"X-Bot-Appid": "123"}
        if signed:
            timestamp = "1725442341"
            signature = self.runtime.signature.key.sign(timestamp.encode() + body).hex()
            headers.update({"X-Signature-Timestamp": timestamp, "X-Signature-Ed25519": signature})
        if tamper:
            body += b" "
        return await self.client.post("/qqbot", data=body, headers=headers)

    async def test_official_challenge_vector(self):
        signature = Signature("DG5g3B4j9X2KOErG")
        result = signature.challenge("Arq0D5A61EgUu4OxUvOp", "1725442341")
        self.assertEqual(result["signature"], "87befc99c42c651b3aac0278e71ada338433ae26fcb24307bdc5ad38c1adc2d01bcfcadc0842edac85e85205028a1132afe09280305f13aa6909ffc2d652c706")
        response = await self.send({"op": 13, "d": {"plain_token": "hello", "event_ts": "123"}}, signed=False)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["plain_token"], "hello")

    async def test_app_lifecycle(self):
        app = create_app("123", "test-secret", self.root)
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/qqbot", json={
                "op": 13, "d": {"plain_token": "hello", "event_ts": "123"}
            }, headers={"X-Bot-Appid": "123"})
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["plain_token"], "hello")

    async def test_webhook_auth_dedup_order_and_sessions(self):
        def event(mid, content, user="alice", group="group1"):
            return {"op": 0, "id": mid, "t": "GROUP_AT_MESSAGE_CREATE", "d": {
                "id": mid, "content": content, "group_openid": group,
                "author": {"member_openid": user},
            }}
        payload = event("1", "cd sub")
        self.assertEqual((await self.send(payload, signed=False)).status, 401)
        self.assertEqual((await self.send(payload, tamper=True)).status, 401)
        for item in (payload, payload, event("2", "ls"), event("3", "ls", user="bob"), event("4", "ls", group="group2")):
            response = await self.send(item)
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["op_code"], 12)
        worker = asyncio.create_task(self.runtime.worker())
        await asyncio.wait_for(self.runtime.queue.join(), 5)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        calls = self.api.reply.await_args_list
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[1].args[-1], "（无输出）")
        self.assertEqual(calls[2].args[-1], "sub")
        self.assertEqual(calls[3].args[-1], "sub")

    async def test_c2c_and_queue_backpressure(self):
        event = {"op": 0, "id": "event", "t": "C2C_MESSAGE_CREATE", "d": {
            "id": "message", "content": "ls", "author": {"user_openid": "alice"}}}
        self.runtime.queue = asyncio.Queue(maxsize=1)
        self.runtime.queue.put_nowait(())
        self.assertEqual((await self.send(event)).status, 503)
        self.runtime.queue.get_nowait()
        self.assertEqual((await self.send(event)).status, 200)
        self.assertEqual(self.runtime.queue.get_nowait()[:4], ("c2c", "alice", "alice", "message"))

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
