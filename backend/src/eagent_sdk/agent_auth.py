# -*- coding: utf-8 -*-
"""Agent Auth: 自有/第三方 Agent 校验 eAgent Router 凭证（from_nekiro/12 §S2 / B2）。

用途: 让受管 Agent 拒绝「绕过 eAgent 网关的直连请求」。
- `RouterCredentialGuard`: 纯校验器（任何框架可用）。
- `ASGIAgentAuthMiddleware`: Starlette/FastAPI/a2a-sdk 子应用中间件。

本模块独立于 eagent_gateway（第三方 Agent 可直接复制使用），
仅依赖 pyjwt + cryptography。
"""
from __future__ import annotations

import threading
import time
from typing import Mapping

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class InMemoryReplayGuard:
    def __init__(self, ttl_seconds: int = 60) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds

    def check_and_mark(self, jti: str) -> bool:
        now = time.time()
        with self._lock:
            self._seen = {k: v for k, v in self._seen.items() if v > now}
            if jti in self._seen:
                return False
            self._seen[jti] = now + self._ttl
            return True


class RouterCredentialGuard:
    """校验 Authorization: Bearer <eAgent Router JWT>。"""

    def __init__(
        self,
        *,
        public_key_pem: str,
        issuer: str = "eagent-router",
        audience: str | None = None,
        replay_guard: InMemoryReplayGuard | None = None,
    ) -> None:
        if not public_key_pem:
            raise ValueError("public_key_pem 不能为空")
        key = serialization.load_pem_public_key(public_key_pem.encode())
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("必须是 Ed25519 公钥（PEM）")
        self._key = key
        self._issuer = issuer
        self._audience = audience
        self._replay_guard = replay_guard or InMemoryReplayGuard()

    def verify_token(self, token: str) -> dict:
        """校验签名/时效/issuer/(audience)/重放；失败抛 PermissionError。"""
        try:
            claims = jwt.decode(
                token,
                self._key,
                algorithms=["EdDSA"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "jti"]},
            )
        except jwt.InvalidTokenError as exc:
            raise PermissionError(
                f"eAgent Router 凭证无效: {type(exc).__name__}",
            ) from exc
        if not self._replay_guard.check_and_mark(claims["jti"]):
            raise PermissionError("凭证重放（jti 已使用）")
        return claims

    def verify_headers(self, headers: Mapping[str, str]) -> dict:
        auth = ""
        for k, v in headers.items():
            if k.lower() == "authorization":
                auth = v
                break
        if not auth.lower().startswith("bearer "):
            raise PermissionError("缺少 eAgent Router 凭证（Authorization: Bearer）")
        return self.verify_token(auth[7:].strip())


class ASGIAgentAuthMiddleware:
    """Starlette 风格中间件: 非 Card 发现路径一律要求有效凭证。

    用法:
        guard = RouterCredentialGuard(public_key_pem=..., audience="http://me:9000/")
        app.add_middleware(ASGIAgentAuthMiddleware, guard=guard)
    """

    def __init__(self, app, guard: RouterCredentialGuard) -> None:  # noqa: ANN001
        self.app = app
        self.guard = guard

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if "/.well-known/" in path:
            await self.app(scope, receive, send)  # Card 发现公开
            return
        headers = {
            k.decode(): v.decode()
            for k, v in scope.get("headers", [])
        }
        try:
            self.guard.verify_headers(headers)
        except PermissionError as exc:
            import json as _json

            body = _json.dumps({
                "code": "UNAUTHENTICATED",
                "message": f"{exc}（只接受经 eAgent 网关的受管调用）",
                "traceId": headers.get("x-eagent-trace-id"),
            }).encode()
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)
