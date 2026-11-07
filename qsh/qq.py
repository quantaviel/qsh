"""The current QQ OpenAPI: token cache, passive text replies and signatures."""

import asyncio
import time
from urllib.parse import quote

from aiohttp import ClientSession
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

API_BASE = "https://api.bot.qq.com"


class APIError(Exception):
    pass


class Signature:
    def __init__(self, secret: str):
        seed = secret.encode("utf-8")
        if not seed:
            raise ValueError("secret 不能为空")
        seed = (seed * ((32 + len(seed) - 1) // len(seed)))[:32]
        self.key = Ed25519PrivateKey.from_private_bytes(seed)

    def verify(self, timestamp: str, body: bytes, signature: str) -> bool:
        if not timestamp or not signature:
            return False
        try:
            self.key.public_key().verify(
                bytes.fromhex(signature), timestamp.encode("utf-8") + body
            )
            return True
        except (ValueError, InvalidSignature):
            return False

    def challenge(self, plain_token: str, event_ts: str) -> dict:
        return {
            "plain_token": plain_token,
            "signature": self.key.sign((event_ts + plain_token).encode("utf-8")).hex(),
        }


class QQAPI:
    def __init__(self, session: ClientSession, appid: str, secret: str):
        self.session = session
        self.appid = appid
        self.secret = secret
        self._token = ""
        self._refresh_at = 0.0
        self._token_lock = asyncio.Lock()

    async def _post(self, path: str, payload: dict, headers: dict | None = None) -> dict:
        async with self.session.post(
            API_BASE + path, json=payload, headers=headers
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
                data = await self._post(
                    "/app/getAppAccessToken",
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

    async def reply(self, kind: str, target: str, message_id: str, content: str):
        token = await self.token()
        resource = "groups" if kind == "group" else "users"
        await self._post(
            f"/v2/{resource}/{quote(target, safe='')}/messages",
            {"msg_type": 0, "content": content, "msg_id": message_id, "msg_seq": 1},
            {"Authorization": f"QQBot {token}"},
        )
