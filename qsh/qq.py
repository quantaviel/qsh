"""The current QQ OpenAPI: token cache, gateway discovery and passive text replies."""

import asyncio
import time
from urllib.parse import quote

from aiohttp import ClientSession
API_BASE = "https://api.bot.qq.com"


class APIError(Exception):
    pass


class QQAPI:
    def __init__(self, session: ClientSession, appid: str, secret: str):
        self.session = session
        self.appid = appid
        self.secret = secret
        self._token = ""
        self._refresh_at = 0.0
        self._token_lock = asyncio.Lock()

    async def _request(self, method: str, path: str, payload: dict | None = None, headers: dict | None = None) -> dict:
        async with self.session.request(
            method, API_BASE + path, json=payload, headers=headers
        ) as response:
            try:
                data = await response.json(content_type=None)
            except ValueError:
                raise APIError(f"QQ 返回非 JSON 响应，HTTP {response.status}") from None
            if not isinstance(data, dict):
                raise APIError("QQ 返回无效响应")
            code = data.get("err_code") or data.get("code") or 0
            if response.status >= 400 or code != 0:
                # Do not log response bodies: they may contain credentials or content.
                trace = response.headers.get("X-Tps-trace-ID", data.get("trace_id", ""))
                raise APIError(f"QQ API HTTP={response.status} code={code} trace={trace}")
            return data

    async def token(self) -> str:
        async with self._token_lock:
            if time.monotonic() >= self._refresh_at:
                data = await self._request(
                    "POST", "/app/getAppAccessToken",
                    {"appId": self.appid, "clientSecret": self.secret},
                )
                token = data.get("access_token")
                try:
                    ttl = float(data["expires_in"])
                except (KeyError, ValueError, TypeError):
                    raise APIError("QQ 凭证有效期无效") from None
                if not isinstance(token, str) or not token or ttl <= 0:
                    raise APIError("QQ 未返回有效凭证")
                self._token = token
                self._refresh_at = time.monotonic() + max(0, ttl - 55)
            return self._token

    def invalidate_token(self):
        self._refresh_at = 0.0

    async def gateway(self) -> str:
        data = await self._request(
            "GET", "/gateway", headers={"Authorization": f"QQBot {await self.token()}"}
        )
        url = data.get("url")
        if not isinstance(url, str) or not url.startswith("wss://"):
            raise APIError("QQ 未返回有效 WSS 网关地址")
        return url

    async def reply(self, kind: str, target: str, message_id: str, content: str):
        token = await self.token()
        resource = "groups" if kind == "group" else "users"
        await self._request(
            "POST", f"/v2/{resource}/{quote(target, safe='')}/messages",
            {"msg_type": 0, "content": content, "msg_id": message_id, "msg_seq": 1},
            {"Authorization": f"QQBot {token}"},
        )
