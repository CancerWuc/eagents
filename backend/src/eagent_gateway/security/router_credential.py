# -*- coding: utf-8 -*-
"""Router 短期签名凭证（from_nekiro/05-credential-security.md，P1-1）。

- Ed25519 JWT（PyJWT + cryptography），TTL 默认 30s，每次调用新签发、新 jti。
- 凭证绑定 endpoint 语义（audience）、workspace/agent/release/cardDigest/
  capability/invocation/rootTask/trace，Agent 侧可校验一致性。
- 私钥只从环境变量/文件注入，不落库、不打日志；jti 不进 Ledger。
"""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from ..config import settings
from .replay_guard import InMemoryReplayGuard

# 校验时必须与请求上下文一致的 claims
BOUND_CLAIMS = (
    "workspaceId",
    "agentId",
    "releaseId",
    "cardDigest",
    "capability",
    "invocationId",
    "rootTaskId",
    "traceId",
)


def _load_pem(value: str) -> str:
    """PEM 文本或 PEM 文件路径（docker secret / k8s secret 挂载友好）。"""
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("-----"):
        return value
    path = Path(value)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    raise ValueError(f"Router 凭证密钥既不是 PEM 文本也不是可读文件: {value[:60]}")


class RouterCredentialIssuer:
    """Router 侧签发（ ManagedA2AClient 每次调用前调用）。"""

    def __init__(
        self,
        private_key: Ed25519PrivateKey,
        issuer: str,
        key_id: str,
        ttl_seconds: int = 30,
    ) -> None:
        self._private_key = private_key
        self._issuer = issuer
        self._key_id = key_id
        self._ttl_seconds = ttl_seconds

    def issue(
        self,
        *,
        audience: str,
        workspace_id: str,
        agent_id: str,
        release_id: str,
        card_digest: str,
        capability: str,
        invocation_id: str,
        root_task_id: str,
        parent_invocation_id: str | None,
        trace_id: str,
        subject_id: str = "",
        display_name: str = "",
    ) -> str:
        now = int(time.time())
        payload = {
            "sub": subject_id,
            "displayName": display_name,
            "iss": self._issuer,
            "aud": audience,
            "iat": now,
            "exp": now + self._ttl_seconds,
            "jti": "rtj_" + uuid.uuid4().hex,
            "workspaceId": workspace_id,
            "agentId": agent_id,
            "releaseId": release_id,
            "cardDigest": card_digest,
            "capability": capability,
            "invocationId": invocation_id,
            "rootTaskId": root_task_id,
            "parentInvocationId": parent_invocation_id,
            "traceId": trace_id,
        }
        token = jwt.encode(
            payload,
            self._private_key,
            algorithm="EdDSA",
            headers={"kid": self._key_id, "typ": "router-agent-credential"},
        )
        if isinstance(token, bytes):  # PyJWT < 2 兼容
            token = token.decode()
        return token


class RouterCredentialVerifier:
    """Agent 侧校验（eAgent 自有 A2A server 可选启用）。"""

    def __init__(
        self,
        public_key: Ed25519PublicKey,
        issuer: str,
        expected_audience: str,
        replay_guard: InMemoryReplayGuard | None = None,
    ) -> None:
        self._public_key = public_key
        self._issuer = issuer
        self._expected_audience = expected_audience
        self._replay_guard = replay_guard

    def verify(self, token: str, *, expected_claims: dict | None = None) -> dict:
        """校验签名/时效/重放/绑定 claims；失败抛 PermissionError。"""
        try:
            claims = jwt.decode(
                token,
                self._public_key,
                algorithms=["EdDSA"],
                audience=self._expected_audience,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "jti"]},
            )
        except jwt.InvalidTokenError as exc:
            raise PermissionError(f"Router 凭证无效: {type(exc).__name__}") from exc
        if self._replay_guard is not None and not self._replay_guard.check_and_mark(
            claims["jti"],
        ):
            raise PermissionError("Router 凭证重放（jti 已使用）")
        for field in BOUND_CLAIMS:
            if field in (expected_claims or {}) and claims.get(field) != \
                    expected_claims[field]:
                raise PermissionError(f"Router 凭证 claim 不匹配: {field}")
        return claims


_issuer_singleton: RouterCredentialIssuer | None = None
_verifier_singleton: RouterCredentialVerifier | None = None


def get_credential_issuer() -> RouterCredentialIssuer | None:
    """按配置惰性构建；未配置私钥 = 不签发（返回 None）。"""
    global _issuer_singleton
    if _issuer_singleton is None:
        pem = _load_pem(settings.router_cred_private_key)
        if not pem:
            return None
        key = serialization.load_pem_private_key(
            pem.encode(), password=None,
        )
        if not isinstance(key, Ed25519PrivateKey):
            raise RuntimeError("EAGENT_ROUTER_CRED_PRIVATE_KEY 必须是 Ed25519 私钥")
        _issuer_singleton = RouterCredentialIssuer(
            private_key=key,
            issuer=settings.router_cred_issuer,
            key_id=settings.router_cred_key_id,
            ttl_seconds=settings.router_cred_ttl_s,
        )
    return _issuer_singleton


def get_credential_verifier() -> RouterCredentialVerifier | None:
    """按配置惰性构建；未配置公钥 = 不校验（返回 None）。"""
    global _verifier_singleton
    if _verifier_singleton is None:
        pem = _load_pem(settings.router_cred_public_key)
        if not pem:
            return None
        key = serialization.load_pem_public_key(pem.encode())
        if not isinstance(key, Ed25519PublicKey):
            raise RuntimeError("EAGENT_ROUTER_CRED_PUBLIC_KEY 必须是 Ed25519 公钥")
        _verifier_singleton = RouterCredentialVerifier(
            public_key=key,
            issuer=settings.router_cred_issuer,
            expected_audience=settings.router_cred_audience,
            replay_guard=InMemoryReplayGuard(
                ttl_seconds=settings.router_cred_ttl_s * 2,
            ),
        )
    return _verifier_singleton


def reset_credential_singletons() -> None:
    """测试用: 清空惰性单例（改配置后重建）。"""
    global _issuer_singleton, _verifier_singleton
    _issuer_singleton = None
    _verifier_singleton = None
