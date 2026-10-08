# -*- coding: utf-8 -*-
"""出站凭证脱敏（from_hermes_a2a/02 R6）。

与 redact_text（落库口径: 手机/邮箱/证件/银行卡）不同，这里是出站
请求体口径: 清洗常见凭证形字符串，防止 master 把密钥原样转发给远端
peer。两者口径不同，不复用。
"""
import re

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"), "sk-ant-[redacted]"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "sk-[redacted]"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "ghp_[redacted]"),
    (re.compile(r"xox[bap]-[A-Za-z0-9\-]{10,}"), "xox-[redacted]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA[redacted]"),
    (
        re.compile(
            r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
        ),
        "[redacted-jwt]",
    ),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}"), "Bearer [redacted]"),
    (
        re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
        "[redacted-email]",
    ),
)


def redact_outbound(text: str) -> str:
    """清洗出站文本中的凭证形字符串；空值原样返回。"""
    if not text:
        return text
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text
