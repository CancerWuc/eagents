# -*- coding: utf-8 -*-
"""契约加载与校验（P0-1 契约优先）。

`<repo>/contracts/` 是接口与数据模型的单一版本化事实来源，本模块是其
Python 消费入口：

- 外部输入（远端 Agent Card 等）进入业务流程前必须过 Schema 校验，
  fail-closed：不符合契约的输入直接拒绝，不降级、不猜测。
- 内部 DTO 用 Pydantic 定义（snake_case 字段 + camelCase wire 别名），
  contracts/tests 保证「Pydantic 序列化结果 ⇔ JSON Schema」一致。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ContractViolation(ValueError):
    """实例不符合契约。消息只含字段路径与规则描述，可安全对外展示。"""


# 契约文件名注册表: 代码只通过这些常量引用契约文件
AGENT_CARD_SCHEMA = "agent-card.v1.schema.json"
AGENT_RELEASE_SCHEMA = "agent-release.v1.schema.json"
INSTALLATION_SCHEMA = "installation.v1.schema.json"
INVOCATION_EVENT_SCHEMA = "invocation-event.v1.schema.json"
PLATFORM_ERROR_SCHEMA = "platform-error.v1.schema.json"
# KB 知识库（from_llm_wiki/02 §4、04 §7）
KB_SCHEMA = "kb.v1.schema.json"
KB_PLAN_PROPOSAL_SCHEMA = "kb-plan-proposal.v2.schema.json"


def _contracts_dir() -> Path:
    candidates: list[Path] = []
    env_dir = os.environ.get("EAGENT_CONTRACTS_DIR", "")
    if env_dir:
        candidates.append(Path(env_dir))
    # 仓库布局: eagent-manager/src/eagent_gateway/contracts.py -> ../..
    candidates.append(Path(__file__).resolve().parents[2] / "contracts")
    # 容器布局（Dockerfile: COPY contracts /app/contracts）
    candidates.append(Path("/app/contracts"))
    for cand in candidates:
        if (cand / "schemas").is_dir():
            return cand
    raise RuntimeError(
        "contracts/ 目录不可用（schemas/ 未找到）；契约校验不允许跳过，"
        "可用 EAGENT_CONTRACTS_DIR 显式指定路径。",
    )


@lru_cache(maxsize=None)
def _registry():  # noqa: ANN202 - referencing.Registry
    from jsonschema.validators import validator_for
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    resources = []
    for path in sorted((_contracts_dir() / "schemas").glob("*.schema.json")):
        contents = json.loads(path.read_text(encoding="utf-8"))
        validator_for(contents).check_schema(contents)
        resources.append(
            (
                contents.get("$id", path.name),
                Resource.from_contents(
                    contents, default_specification=DRAFT202012,
                ),
            ),
        )
    return Registry().with_resources(resources)


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict[str, Any]:
    path = _contracts_dir() / "schemas" / name
    if not path.is_file():
        raise ContractViolation(f"契约文件不存在: {name}")
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def _validator(name: str):  # noqa: ANN202 - jsonschema validator instance
    from jsonschema.validators import validator_for

    schema = load_schema(name)
    validator_for(schema).check_schema(schema)
    return validator_for(schema)(schema, registry=_registry())


def validate(instance: Any, schema_name: str, *, max_errors: int = 5) -> list[str]:
    """校验实例，返回可读错误列表（空列表 = 通过）。"""
    errors: list[str] = []
    for err in _validator(schema_name).iter_errors(instance):
        loc = ".".join(str(p) for p in err.absolute_path) or "$"
        errors.append(f"{loc}: {err.message}")
        if len(errors) >= max_errors:
            break
    return errors


def validate_or_raise(instance: Any, schema_name: str) -> None:
    errors = validate(instance, schema_name)
    if errors:
        raise ContractViolation(
            f"不符合契约 {schema_name}: " + "; ".join(errors[:3]),
        )


# ---------------------------------------------------------------------------
# 契约 DTO（内部实现消费；wire 格式为 camelCase）
# ---------------------------------------------------------------------------
class _ContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
    )


class AgentRelease(_ContractModel):
    """agent-release.v1: 不可变发布版本（P0-2 消费）。"""

    release_id: str
    service_id: str
    agent_version: str
    card_revision_id: str
    card_digest: str
    endpoint: str
    state: Literal["DRAFT", "PUBLISHED", "SUSPENDED", "REVOKED"]
    created_by: str
    created_at: datetime
    published_at: datetime | None = None
    suspended_at: datetime | None = None
    revoked_at: datetime | None = None


class Installation(_ContractModel):
    """installation.v1: 工作空间显式安装（P0-2 消费）。"""

    installation_id: str
    workspace_id: str
    service_id: str
    release_id: str
    status: Literal["ENABLED", "DISABLED", "UNINSTALLED"]
    accepted_permissions: list[str] | None = None
    created_at: datetime
    updated_at: datetime
    uninstalled_at: datetime | None = None


class InvocationEvent(_ContractModel):
    """invocation-event.v1: append-only 调用事件（P0-4 消费）。"""

    event_id: str
    invocation_id: str
    trace_id: str
    root_task_id: str
    parent_invocation_id: str | None = None
    workspace_id: str | None = None
    master_id: str | None = None
    service_id: str | None = None
    release_id: str | None = None
    event_type: Literal[
        "INVOCATION_STARTED",
        "AGENT_CALL_STARTED",
        "AGENT_CALL_FINISHED",
        "INVOCATION_FINISHED",
    ]
    status: str
    error_code: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class ErrorDetail(_ContractModel):
    field: str
    issue: str


class PlatformErrorPayload(_ContractModel):
    """platform-error.v1: 统一错误响应（errors.py 生成，见契约测试）。"""

    code: str
    message: str
    trace_id: str | None = None
    details: list[ErrorDetail] | None = None
