"""QQ WebSocket gateway: Identify, heartbeat ACK, Resume and reconnect."""

import asyncio
import logging
import math
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, WSMsgType

from .qq import APIError, QQAPI
from .runtime import Runtime

LOG = logging.getLogger(__name__)
INTENTS = 1 << 25  # GROUP_AND_C2C_EVENT


class Reconnect(Exception):
    pass


class Gateway:
    def __init__(self, session: ClientSession, api: QQAPI, runtime: Runtime):
        self.session = session
        self.api = api
        self.runtime = runtime
        self.session_id: str | None = None
        self.sequence: int | None = None
        self.ready = False
        self.pending_ack = False

    async def heartbeat(self, ws, interval: float):
        while True:
            await asyncio.sleep(interval)
            if self.pending_ack:
                LOG.warning("未收到心跳 ACK，重新连接")
                await ws.close()
                return
            self.pending_ack = True
            try:
                await ws.send_json({"op": 1, "d": self.sequence})
            except Exception:
                await ws.close()
                return

    async def connect(self):
        self.ready = False
        url = await self.api.gateway()
        token = await self.api.token()
        self.pending_ack = False
        async with self.session.ws_connect(url, max_msg_size=1024 * 1024) as ws:
            hello = await ws.receive_json(timeout=15)
            if not isinstance(hello, dict) or hello.get("op") != 10:
                raise Reconnect("未收到 Hello")
            try:
                interval = float(hello["d"]["heartbeat_interval"]) / 1000
            except (KeyError, TypeError, ValueError):
                raise Reconnect("无效心跳周期") from None
            if not math.isfinite(interval) or interval <= 0:
                raise Reconnect("无效心跳周期")
            if self.session_id is not None and self.sequence is not None:
                await ws.send_json({"op": 6, "d": {
                    "token": f"QQBot {token}", "session_id": self.session_id,
                    "seq": self.sequence,
                }})
            else:
                await ws.send_json({"op": 2, "d": {
                    "token": f"QQBot {token}", "intents": INTENTS, "shard": [0, 1],
                }})
            heartbeat = asyncio.create_task(self.heartbeat(ws, interval))
            login_deadline = asyncio.get_running_loop().time() + 15
            try:
                while True:
                    timeout = max(15, interval * 2)
                    if not self.ready:
                        timeout = min(timeout, login_deadline - asyncio.get_running_loop().time())
                        if timeout <= 0:
                            raise Reconnect("登录超时，未收到 READY/RESUMED")
                    message = await ws.receive(timeout=timeout)
                    if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING, WSMsgType.ERROR):
                        raise Reconnect(f"连接关闭，code={ws.close_code}")
                    if message.type != WSMsgType.TEXT:
                        continue
                    payload = message.json()
                    if not isinstance(payload, dict):
                        raise Reconnect("无效网关数据")
                    op = payload.get("op")
                    if op == 11:
                        self.pending_ack = False
                    elif op == 1:
                        self.pending_ack = True
                        await ws.send_json({"op": 1, "d": self.sequence})
                    elif op == 7:
                        raise Reconnect("网关要求重连")
                    elif op == 9:
                        self.session_id = None
                        self.sequence = None
                        self.api.invalidate_token()
                        raise Reconnect("会话无效，将重新鉴权；请检查平台接入方式及事件权限")
                    elif op == 0:
                        event = payload.get("t")
                        if event == "READY":
                            data = payload.get("d")
                            sid = data.get("session_id") if isinstance(data, dict) else None
                            if not isinstance(sid, str) or not sid:
                                raise Reconnect("READY 缺少 session_id")
                            self.session_id = sid
                            self.ready = True
                            LOG.info("QQ WebSocket 已就绪")
                        elif event == "RESUMED":
                            self.ready = True
                            LOG.info("QQ WebSocket 会话已恢复")
                        else:
                            try:
                                self.runtime.accept(payload)
                            except ValueError:
                                LOG.warning("忽略格式无效的消息事件")
                            except asyncio.QueueFull:
                                raise Reconnect("消息队列已满，尝试重连恢复") from None
                        # Advance only after the event has been accepted.
                        if isinstance(payload.get("s"), int):
                            self.sequence = payload["s"]
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def run(self):
        delay = 1
        while True:
            try:
                await self.connect()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Never log gateway payloads, URLs with credentials, or tokens.
                detail = str(error) if isinstance(error, (Reconnect, APIError)) else type(error).__name__
                if self.ready:
                    delay = 1
                LOG.warning("WebSocket 断开/连接失败：%s；%s 秒后重试", detail, delay)
            await asyncio.sleep(delay)
            delay = 1 if self.ready else min(delay * 2, 30)


async def run_bot(appid: str, secret: str, cwd: Path):
    async with ClientSession(timeout=ClientTimeout(total=15)) as session:
        api = QQAPI(session, appid, secret)
        runtime = Runtime(cwd, api)
        worker = asyncio.create_task(runtime.worker())
        LOG.info("正在连接 QQ WebSocket；初始目录：%s", cwd)
        try:
            await Gateway(session, api, runtime).run()
        finally:
            try:
                await asyncio.wait_for(runtime.queue.join(), timeout=20)
            except TimeoutError:
                LOG.warning("退出时仍有未完成消息；队列和会话状态不持久化")
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
