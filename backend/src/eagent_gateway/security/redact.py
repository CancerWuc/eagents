# -*- coding: utf-8 -*-
"""日志/评测数据脱敏（设计 §10.5 / T5）。

运行记录、评测集只存脱敏文本；对账用脱敏文本的摘要，不存原文哈希
（低熵原文的哈希可被字典攻击反推）。
"""
import hashlib
import re

_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_IDCARD = re.compile(r"(?<!\d)\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:[0-2]\d|3[01])\d{3}[\dXx](?![\dXx])")
_CARD_NUM = re.compile(r"(?<!\d)\d{13,19}(?!\d)")
_CREDENTIAL = re.compile(
    r"(?i)\b(?:sk-[a-z0-9]{8,}|bearer\s+[a-z0-9._-]{8,}|api[_-]?key[=:]\s*\S{6,})",
)


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def redact_text(text: str | None) -> str:
    """对自由文本做模式化脱敏。返回脱敏后文本（原文不落任何存储）。"""
    if not text:
        return ""

    def phone_sub(m: re.Match) -> str:
        s = m.group(0)
        return f"{s[:3]}****{s[-2:]}"

    def email_sub(m: re.Match) -> str:
        local = m.group(0).split("@", 1)[0]
        domain = m.group(0).split("@", 1)[1]
        head = local[:2]
        return f"{head}***@{domain}"

    def idcard_sub(m: re.Match) -> str:
        s = m.group(0)
        return f"{s[:6]}********{s[-4:]}"

    def card_sub(m: re.Match) -> str:
        s = m.group(0)
        if not _luhn_ok(s):
            return s  # 未通过 Luhn 校验的长数字不当作银行卡
        return f"****{s[-4:]}"

    text = _CREDENTIAL.sub("<credential>", text)
    text = _IDCARD.sub(idcard_sub, text)
    text = _PHONE.sub(phone_sub, text)
    text = _EMAIL.sub(email_sub, text)
    text = _CARD_NUM.sub(card_sub, text)
    return text


def digest_of(redacted: str) -> str:
    """脱敏文本的摘要（对账/去重用；不可逆推原文）。"""
    return "sha256:" + hashlib.sha256(redacted.encode()).hexdigest()[:16]
