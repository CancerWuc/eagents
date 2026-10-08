# -*- coding: utf-8 -*-
"""consumer 密钥加密（higress-mcp-gateway 03 T12，01 §5.4）。

- AES-256-GCM；主密钥来自 EAGENT_GATEWAY_MASTER_KEY（base64 的 32 字节），
  不落库、不进日志。
- fail-secure：gateway 启用（EAGENT_GATEWAY_ENABLED=1）而未配置主密钥时
  拒绝启动；未启用时首次加解密操作抛 GatewayConfigError（共用部署不受拖累）。
- 密文格式： nonce(12B) || ciphertext || tag(16B)，整块存 VARBINARY；
  库内无法直接读出明文（无主密钥不可解）。
- 展示脱敏：只回显尾 4 位（****abcd），与 from_nekiro/05 口径一致。
"""
from __future__ import annotations

import base64
import os

from .. import config
from .errors import GatewayConfigError

_NONCE_BYTES = 12
_KEY_BYTES = 32
_TAG_BYTES = 16


def _master_key_bytes() -> bytes:
    raw = (config.settings.gateway_master_key or "").strip()
    if not raw:
        raise GatewayConfigError(
            "未配置 EAGENT_GATEWAY_MASTER_KEY（base64 的 32 字节），"
            "consumer 密钥加密不可用",
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except (ValueError, TypeError) as exc:
        raise GatewayConfigError("EAGENT_GATEWAY_MASTER_KEY 不是合法 base64") \
            from exc
    if len(key) != _KEY_BYTES:
        raise GatewayConfigError(
            f"EAGENT_GATEWAY_MASTER_KEY 必须为 base64 的 {_KEY_BYTES} 字节",
        )
    return key


def assert_master_key_configured() -> None:
    """启动校验（fail-secure）：配置非法直接抛错拒绝启动。"""
    _master_key_bytes()


def encrypt_credential(plaintext: str) -> bytes:
    """明文密钥 → AES-GCM 密文块（nonce||ct||tag）。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = _master_key_bytes()
    nonce = os.urandom(_NONCE_BYTES)
    block = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return nonce + block


def decrypt_credential(block: bytes) -> str:
    """密文块 → 明文密钥；主密钥不符/密文损坏抛 ValueError（不外泄细节）。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = _master_key_bytes()
    if len(block) <= _NONCE_BYTES + _TAG_BYTES:
        raise ValueError("credential 密文长度非法")
    nonce, body = block[:_NONCE_BYTES], block[_NONCE_BYTES:]
    return AESGCM(key).decrypt(nonce, body, None).decode("utf-8")


def generate_credential(prefix: str = "oma-") -> str:
    """生成随机 32 字节密钥（urlsafe base64，03 T09：随机 32B base64url）。"""
    return prefix + base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


def mask_credential(plaintext_or_block: str | bytes) -> str:
    """展示脱敏：只回显尾 4 位（密文字节块先解密再脱敏由调用方负责）。"""
    if isinstance(plaintext_or_block, (bytes, bytearray)):
        tail = decrypt_credential(bytes(plaintext_or_block))[-4:]
    else:
        tail = plaintext_or_block[-4:]
    return f"****{tail}"
