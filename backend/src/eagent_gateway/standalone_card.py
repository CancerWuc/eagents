# -*- coding: utf-8 -*-
"""standalone card.json 口径（standalone-deployment 01 §6/§7.1，S-04/S-06 共用）。

card.json = release 合并导出的 Agent 配置（**不含 api_key**，key 由部署方
env 注入），平台 export 端点（S-06）写、容器 standalone_main（S-04）读，
两侧唯一 schema 口径在本模块：

{
  "card_format": "eagent-standalone-card@1",
  "agent": {
    "master_id", "slug", "display_name",
    "prompt_text", "prompt_version_id",
    "react_config": {"max_iters": 8},
    "model_config": {"provider", "base_url", "model"}   ← api_key 一律剔除
  },
  "kb_bindings": ["<kb_slug>", ...],                     ← 绑定集（快照 slug）
  "release": {"release_id", "service_id", "agent_version", "created_by"},
  "snapshot_format_version": 1
}

deploy_digest（三件套联合锁定，§7.1）= sha256(card_digest + ":" +
snapshot_root_hash + ":" + image_tag)，其中 card_digest = sha256(card.json
**文件字节**)——card 自身不携带（自指不可能），经部署清单/env 注入，
运行时由引擎对文件实算复核（防篡改/版本错配）。
"""
from __future__ import annotations

import json
from pathlib import Path

from .kb.backends import (
    SNAPSHOT_FORMAT_VERSION,
    check_snapshot_format,
    load_snapshot_index,
    snapshot_root_hash,
)

CARD_FORMAT = "eagent-standalone-card@1"


def card_digest_of(card_path: str | Path) -> str:
    """card_digest = sha256(card.json 文件字节)（§7.1 合并导出实算口径）。"""
    import hashlib

    data = Path(card_path).read_bytes()
    return hashlib.sha256(data).hexdigest()


def compute_deploy_digest(
    card_digest: str, snapshot_hash: str, image_tag: str,
) -> str:
    """deploy_digest = sha256(card_digest:snapshot_hash:image_tag)（§7.1）。

    冒号拼接避免等长 hex 段歧义；image_tag 为引擎镜像 tag（构建期烘焙）。
    """
    import hashlib

    joined = f"{card_digest}:{snapshot_hash}:{image_tag}"
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def load_card(card_path: str | Path) -> dict:
    """加载并结构校验 card.json（fail-closed：schema 不符直接抛 ValueError）。"""
    path = Path(card_path)
    if not path.is_file():
        raise ValueError(f"card.json 不存在: {path}")
    try:
        card = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"card.json 解析失败: {exc}") from exc
    if not isinstance(card, dict):
        raise ValueError("card.json 必须是对象")
    if card.get("card_format") != CARD_FORMAT:
        raise ValueError(
            f"card_format 不支持: {card.get('card_format')!r}（引擎支持 "
            f"{CARD_FORMAT!r}）"
        )
    if card.get("snapshot_format_version") != SNAPSHOT_FORMAT_VERSION:
        raise ValueError(
            f"snapshot_format_version 不支持: "
            f"{card.get('snapshot_format_version')!r}（引擎支持 "
            f"{SNAPSHOT_FORMAT_VERSION}）"
        )
    agent = card.get("agent")
    if not isinstance(agent, dict) or not (agent.get("slug") or "").strip():
        raise ValueError("card.agent.slug 缺失")
    bindings = card.get("kb_bindings")
    if not isinstance(bindings, list) or any(
        not isinstance(s, str) or not s.strip() for s in bindings
    ):
        raise ValueError("card.kb_bindings 必须是 slug 字符串数组")
    return card


def verify_deploy_digest(
    card_path: str | Path, snapshot_root: str | Path, image_tag: str,
    expected_digest: str,
) -> str:
    """运行时复核 deploy_digest（EAGENT_STANDALONE_DEPLOY_DIGEST）。

    逐 KB 校验 snapshot_format_version 后重算三件套 digest；不符抛
    ValueError（fail-closed，standalone_main 拒绝启动，§7.1/§8 防篡改）。
    返回实算 digest（相符时）。
    """
    index = load_snapshot_index(snapshot_root)
    for entry in index.values():
        check_snapshot_format(Path(snapshot_root) / entry["dir"])
    actual = compute_deploy_digest(
        card_digest_of(card_path),
        snapshot_root_hash(snapshot_root),
        image_tag,
    )
    if actual != expected_digest:
        raise ValueError(
            "deploy_digest 校验失败（card/快照/镜像 tag 三件套错配或被篡改）"
        )
    return actual
