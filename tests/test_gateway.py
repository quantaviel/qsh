import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from qsh.gateway import Gateway, INTENTS, Reconnect, run_bot
from qsh.qq import QQAPI
from qsh.runtime import Runtime


def event(sequence, content="ls", user="alice", group="group1", mid=None):
    return {"op": 0, "s": sequence, "t": "GROUP_AT_MESSAGE_CREATE", "d": {
        "id": mid or str(sequence), "content": content, "group_openid": group,
        "author": {"member_openid": user},
    }}


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / "sub").mkdir()
        self.api = AsyncMock()
        self.api.token.return_value = "test-token"
        self.api.invalidate_token = Mock()
        self.runtime = Runtime(self.root, self.api)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def run_connection(self, handler, *, interval=10000):
        errors = []

        async def endpoint(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"op": 10, "d": {"heartbeat_interval": interval}})
            try:
                await handler(ws)
            except Exception as error:
                errors.append(error)
            finally:
                await ws.close()
            return ws

        app = web.Application()
        app.router.add_get("/ws", endpoint)
        async with TestServer(app) as server, ClientSession() as session:
            self.api.gateway.return_value = str(server.make_url("/ws")).replace("http:", "ws:")
            gateway = Gateway(session, self.api, self.runtime)
            with self.assertRaises(Reconnect):
                await asyncio.wait_for(gateway.connect(), 3)
        if errors:
            raise errors[0]
        return gateway

    async def test_identify_heartbeats_and_dispatch(self):
        async def handler(ws):
            identify = await ws.receive_json(timeout=1)
            self.assertEqual(identify, {"op": 2, "d": {
                "token": "QQBot test-token", "intents": INTENTS, "shard": [0, 1],
            }})
            await ws.send_json({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "session"}})
            await ws.send_json(event(2, "cd sub"))
            await ws.send_json(event(3, "pwd"))
            heartbeat = await ws.receive_json(timeout=1)
            self.assertEqual(heartbeat, {"op": 1, "d": 3})
            await ws.send_json({"op": 11})
            self.assertEqual((await ws.receive_json(timeout=1))["op"], 1)
            await ws.send_json({"op": 11})
            await ws.send_json({"op": 7})

        gateway = await self.run_connection(handler, interval=30)
        self.assertEqual(gateway.sequence, 3)
        self.assertEqual(gateway.session_id, "session")
        worker = asyncio.create_task(self.runtime.worker())
        await asyncio.wait_for(self.runtime.queue.join(), 3)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual(self.api.reply.await_count, 2)
        self.assertEqual(self.api.reply.await_args_list[-1].args[-1], str(self.root / "sub"))

    async def test_resume_and_replay_dedup(self):
        received = []

        async def endpoint(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"op": 10, "d": {"heartbeat_interval": 10000}})
            received.append(await ws.receive_json(timeout=1))
            if len(received) == 1:
                await ws.send_json({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "sid"}})
                await ws.send_json(event(2))
            else:
                await ws.send_json({"op": 0, "s": 3, "t": "RESUMED", "d": {}})
                await ws.send_json(event(4, mid="2"))
                await ws.send_json(event(5))
            await ws.send_json({"op": 7})
            await ws.close()
            return ws

        app = web.Application()
        app.router.add_get("/ws", endpoint)
        async with TestServer(app) as server, ClientSession() as session:
            self.api.gateway.return_value = str(server.make_url("/ws")).replace("http:", "ws:")
            gateway = Gateway(session, self.api, self.runtime)
            for _ in range(2):
                with self.assertRaises(Reconnect):
                    await asyncio.wait_for(gateway.connect(), 3)
        self.assertEqual(received[1], {"op": 6, "d": {"token": "QQBot test-token", "session_id": "sid", "seq": 2}})
        self.assertEqual(self.runtime.queue.qsize(), 2)
        self.assertEqual(gateway.sequence, 5)

    async def test_invalid_session_and_missing_heartbeat_ack(self):
        async def invalid(ws):
            await ws.receive_json()
            await ws.send_json({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "sid"}})
            await ws.send_json({"op": 9})

        gateway = await self.run_connection(invalid)
        self.assertIsNone(gateway.session_id)
        self.assertIsNone(gateway.sequence)
        self.api.invalidate_token.assert_called_once()

        async def silent(ws):
            await ws.receive_json()
            await ws.receive_json(timeout=1)  # Ignore heartbeat: client must close.
            await ws.receive(timeout=1)

        await self.run_connection(silent, interval=20)

    async def test_server_requested_heartbeat_and_retry_cancellation(self):
        async def handler(ws):
            await ws.receive_json()
            await ws.send_json({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "sid"}})
            await ws.send_json({"op": 1})
            self.assertEqual(await ws.receive_json(timeout=1), {"op": 1, "d": 1})
            await ws.send_json({"op": 11})
            await ws.send_json({"op": 7})

        await self.run_connection(handler)
        gateway = Gateway(Mock(), self.api, self.runtime)
        gateway.connect = AsyncMock(side_effect=[Reconnect("test"), asyncio.CancelledError()])
        with patch("qsh.gateway.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                await gateway.run()
        sleep.assert_awaited_once_with(1)

    async def test_queue_overflow_preserves_resume_sequence(self):
        self.runtime.queue = asyncio.Queue(maxsize=1)

        async def handler(ws):
            await ws.receive_json()
            await ws.send_json({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "sid"}})
            await ws.send_json(event(2))
            await ws.send_json(event(3))
            await ws.receive(timeout=1)

        gateway = await self.run_connection(handler)
        self.assertEqual(gateway.sequence, 2)
        self.assertEqual(len(self.runtime.seen), 1)

    async def test_runtime_c2c_and_directory_isolation(self):
        self.runtime.accept(event(1, "cd sub"))
        self.runtime.accept(event(2, "pwd"))
        self.runtime.accept(event(3, "pwd", user="bob"))
        self.runtime.accept(event(4, "pwd", group="group2"))
        self.runtime.accept({"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {
            "id": "c2c", "content": "pwd", "author": {"user_openid": "alice"}}})
        worker = asyncio.create_task(self.runtime.worker())
        await asyncio.wait_for(self.runtime.queue.join(), 3)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        calls = self.api.reply.await_args_list
        self.assertEqual(calls[1].args[-1], str(self.root / "sub"))
        self.assertTrue(all(call.args[-1] == str(self.root) for call in calls[2:]))
        self.assertEqual(calls[-1].args[:3], ("c2c", "alice", "c2c"))

    async def test_group_message_create_routes_and_deduplicates(self):
        payload = event(1, "<@!BOT_OPENID_ABC123> pwd")
        payload["t"] = "GROUP_MESSAGE_CREATE"
        self.runtime.accept(payload)
        # The same group message delivered under either event name is one
        # command, and shares the same session directory.
        duplicate = dict(payload, t="GROUP_AT_MESSAGE_CREATE")
        self.runtime.accept(duplicate)
        worker = asyncio.create_task(self.runtime.worker())
        await asyncio.wait_for(self.runtime.queue.join(), 3)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        self.api.reply.assert_awaited_once_with("group", "group1", "1", str(self.root))

    async def test_gateway_discovery_and_runner_cleanup(self):
        async def discover(request):
            self.assertEqual(request.headers["Authorization"], "QQBot test-token")
            return web.json_response({"url": "wss://example.com/ws"})

        app = web.Application()
        app.router.add_get("/gateway", discover)
        async with TestServer(app) as server, ClientSession() as session:
            api = QQAPI(session, "appid", "secret")
            api.token = AsyncMock(return_value="test-token")
            with patch("qsh.qq.API_BASE", str(server.make_url("/")).rstrip("/")):
                self.assertEqual(await api.gateway(), "wss://example.com/ws")
        with patch("qsh.gateway.Gateway.run", new=AsyncMock()):
            await run_bot("appid", "secret", self.root)
