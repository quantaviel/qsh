"""Signed QQ webhook receiver with ordered background command processing."""

import asyncio
from collections import OrderedDict
import json
import logging
from pathlib import Path
import re
import time

from aiohttp import ClientSession, ClientTimeout, web

from .qq import QQAPI, Signature
from .shell import Shell

LOG = logging.getLogger(__name__)
RUNTIME = web.AppKey("runtime", object)


def message_event(payload: dict) -> tuple | None:
    kind = {"C2C_MESSAGE_CREATE": "c2c", "GROUP_AT_MESSAGE_CREATE": "group"}.get(
        payload.get("t")
    )
    if kind is None:
        return None
    data = payload.get("d")
    if not isinstance(data, dict) or not isinstance(data.get("author"), dict):
        raise ValueError("无效消息结构")
    author = data["author"]
    if author.get("bot"):
        return None
    user = author.get("member_openid" if kind == "group" else "user_openid")
    target = data.get("group_openid") if kind == "group" else user
    message_id = data.get("id")
    content = data.get("content", "")
    if not all(isinstance(value, str) and value for value in (user, target, message_id)):
        raise ValueError("缺少消息标识")
    if not isinstance(content, str):
        raise ValueError("无效消息正文")
    if kind == "group":
        content = re.sub(r"^\s*<@!?\d+>\s*", "", content)
    return kind, target, user, message_id, content.strip()


class Runtime:
    def __init__(self, appid: str, secret: str, cwd: Path, api: QQAPI):
        self.appid = appid
        self.signature = Signature(secret)
        self.cwd = cwd
        self.api = api
        self.shells: dict[tuple, Shell] = {}
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self.seen: OrderedDict[tuple, float] = OrderedDict()

    async def receive(self, request: web.Request) -> web.Response:
        body = await request.read()
        if request.headers.get("X-Bot-Appid") != self.appid:
            raise web.HTTPUnauthorized()
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest() from None

        # QQ's op=13 address-validation request has no signature headers in its
        # official example; it never executes commands or returns file content.
        if payload.get("op") == 13:
            data = payload.get("d")
            if not isinstance(data, dict) or not all(
                isinstance(data.get(key), str) and data[key]
                for key in ("plain_token", "event_ts")
            ):
                raise web.HTTPBadRequest()
            return web.json_response(self.signature.challenge(data["plain_token"], data["event_ts"]))

        if not self.signature.verify(
            request.headers.get("X-Signature-Timestamp", ""),
            body,
            request.headers.get("X-Signature-Ed25519", ""),
        ):
            raise web.HTTPUnauthorized()
        if payload.get("op") != 0:
            raise web.HTTPBadRequest()
        try:
            event = message_event(payload)
        except ValueError:
            raise web.HTTPBadRequest() from None
        if event is not None:
            key = event[:4]
            now = time.monotonic()
            while self.seen and next(iter(self.seen.values())) < now - 7200:
                self.seen.popitem(last=False)
            if key not in self.seen:
                try:
                    self.queue.put_nowait(event)
                except asyncio.QueueFull:
                    raise web.HTTPServiceUnavailable() from None
                self.seen[key] = now
                if len(self.seen) > 10000:
                    self.seen.popitem(last=False)
        return web.json_response(
            {"op_code": 12, "d": {"event_id": payload.get("id", ""), "status": 0, "message": "success"}}
        )

    async def worker(self):
        # One worker preserves accepted command order, including cd followed by
        # cat. Filesystem operations run off the webhook event loop.
        while True:
            kind, target, user, message_id, content = await self.queue.get()
            try:
                key = kind, target, user
                if key not in self.shells:
                    self.shells[key] = Shell(self.cwd)
                result = await asyncio.to_thread(self.shells[key].execute, content)
                await self.api.reply(kind, target, message_id, result)
            except Exception as error:
                LOG.error("处理或回复失败（%s）：%s", type(error).__name__, error)
            finally:
                self.queue.task_done()


def create_app(appid: str, secret: str, cwd: Path) -> web.Application:
    app = web.Application(client_max_size=1024 * 1024)

    async def lifecycle(app):
        async with ClientSession(timeout=ClientTimeout(total=15)) as session:
            runtime = Runtime(appid, secret, cwd, QQAPI(session, appid, secret))
            app[RUNTIME] = runtime
            worker = asyncio.create_task(runtime.worker())
            LOG.info("Webhook 就绪：/qqbot；初始目录：%s", cwd)
            try:
                yield
            finally:
                try:
                    await asyncio.wait_for(runtime.queue.join(), timeout=20)
                except TimeoutError:
                    LOG.warning("退出时仍有未完成消息；队列和会话状态不持久化")
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    async def receive(request):
        return await request.app[RUNTIME].receive(request)

    app.cleanup_ctx.append(lifecycle)
    app.router.add_post("/qqbot", receive)
    return app
