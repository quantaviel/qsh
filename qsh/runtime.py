"""Message routing, session directories and ordered command processing."""

import asyncio
from collections import OrderedDict
import logging
from pathlib import Path
import re
import time

from .qq import QQAPI
from .shell import Shell

LOG = logging.getLogger(__name__)


def message_event(payload: dict) -> tuple | None:
    if not isinstance(payload.get("t"), str):
        return None
    kind = {"C2C_MESSAGE_CREATE": "c2c", "GROUP_AT_MESSAGE_CREATE": "group",
            "GROUP_MESSAGE_CREATE": "group"}.get(
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
        # Group mentions may contain an opaque OpenID, not a numeric QQ ID.
        content = re.sub(r"^\s*<@!?[^>\s]+>\s*", "", content)
    return kind, target, user, message_id, content.strip()


class Runtime:
    def __init__(self, cwd: Path, api: QQAPI):
        self.cwd = cwd
        self.api = api
        self.shells: dict[tuple, Shell] = {}
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self.seen: OrderedDict[tuple, float] = OrderedDict()

    def accept(self, payload: dict):
        event = message_event(payload)
        if event is None:
            return
        key = event[:4]
        now = time.monotonic()
        while self.seen and next(iter(self.seen.values())) < now - 7200:
            self.seen.popitem(last=False)
        if key not in self.seen:
            # On overflow the gateway reconnects without advancing its sequence,
            # allowing the server to replay this unaccepted event on Resume.
            self.queue.put_nowait(event)
            LOG.info("消息已入队：场景=%s", event[0])
            self.seen[key] = now
            if len(self.seen) > 10000:
                self.seen.popitem(last=False)

    async def worker(self):
        # One worker preserves accepted command order, including cd followed by
        # cat. Filesystem operations run off the gateway event loop.
        while True:
            kind, target, user, message_id, content = await self.queue.get()
            try:
                key = kind, target, user
                if key not in self.shells:
                    self.shells[key] = Shell(self.cwd)
                result = await asyncio.to_thread(self.shells[key].execute, content)
                await self.api.reply(kind, target, message_id, result)
                LOG.info("消息回复成功：场景=%s", kind)
            except Exception as error:
                LOG.error("处理或回复失败（%s）：%s", type(error).__name__, error)
            finally:
                self.queue.task_done()
