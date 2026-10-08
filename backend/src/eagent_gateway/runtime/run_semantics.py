# -*- coding: utf-8 -*-
"""总控回复语义的纯函数集合（平台 run_master 与 standalone 容器共用）。

抽出动机（standalone-deployment 01 §6.1）：容器装配复制 master_service
语义但不得 import 它（会拖入 persistence/registry/router 全链 DB 依赖）。
本模块只允许 stdlib import。
"""
from __future__ import annotations

INPUT_REQUIRED_MARKER = "[INPUT_REQUIRED]"


def extract_input_required(text: str) -> tuple[bool, str]:
    """R3 澄清握手（from_hermes_a2a/02）: 解析 `[INPUT_REQUIRED]` 标记。

    返回 (是否需要补充输入, 去除标记后的正文)；标记匹配大小写不敏感。
    模型可能把标记写在开头或结尾，两处都识别；正文中间的同名字样不算标记。
    """
    stripped = (text or "").strip()
    size = len(INPUT_REQUIRED_MARKER)
    if stripped[:size].upper() == INPUT_REQUIRED_MARKER:
        return True, stripped[size:].strip()
    if stripped[-size:].upper() == INPUT_REQUIRED_MARKER:
        return True, stripped[:-size].strip()
    return False, text or ""
