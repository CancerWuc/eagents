# -*- coding: utf-8 -*-
"""ManagedA2AClient: 所有 A2A 出站调用的唯一出口（from_nekiro/03、12 §S1）。

统一规则:
- 目标地址来自 Release（安装哪个版本就调用哪个版本），实例目录优先生效。
- 出站一律经 transport/（SSRF 固定 IP、禁重定向、统一超时、大小上限、
  JSON-RPC envelope 与 id 校验、错误分类）——本文件不再直接使用 httpx。
- 平台血缘走上下文头；权威用户身份同时进入 canonical 身份头和签名
  Router Bearer claims，两者来自同一个 RouterContext。
- 每次 Agent 调用记 append-only 事件（P0-4），子调用获得新 invocation_id。
- fail-closed: 超时→TIMEOUT、远端失败→DEPENDENCY_ERROR、无实例→
  INSTANCE_UNAVAILABLE；不重试、不换地址、不返回陈旧成功。
- 禁止业务代码直接创建 A2AClient / A2ACardResolver / 直接使用 transport。
"""
from __future__ import annotations

import time
import uuid

from ..a2a_server.protocol import endpoint as card_endpoint


from ..catalog import service as catalog
from ..config import settings
from ..contracts import AGENT_CARD_SCHEMA, validate as schema_validate
from ..errors import DependencyError, InstanceUnavailableError
from ..persistence.db import Database, now as db_now
from ..security.outbound_redact import redact_outbound
from ..security.router_credential import get_credential_issuer
from ..transport import A2ATransport, classify_transport_error
from . import ledger
from .context import RouterContext, new_invocation_id

# endpoint -> (expires_at, card_dict)
_card_cache: dict[str, tuple[float, dict]] = {}


def _text_of(part: dict) -> str | None:
    return part.get("text") if isinstance(part, dict) else None


def _first_text(parts) -> str | None:
    for part in parts or []:
        text = _text_of(part)
        if text:
            return text
    return None


class ManagedA2AClient:
    """进程内受管 Router（from_nekiro/03 §4.1 一期形态）。"""

    def __init__(
        self,
        db: Database,
        *,
        instance_directory=None,  # registry.InstanceDirectory; None=不启用实例发现
        credential_issuer=None,  # 测试可注入; 默认按配置惰性构建
    ) -> None:
        self._db = db
        self._instance_directory = instance_directory
        self._credential_issuer = credential_issuer

    # ------------------------------------------------------------------
    # 目标解析（Release + 实例目录）
    # ------------------------------------------------------------------
    def _resolve_endpoint(self, release: dict) -> str:
        """实例目录优先生效（P1-2）; 服务从未注册实例时用 Release 地址。"""
        if self._instance_directory is not None:
            has_any = self._instance_directory.has_instances(
                release["service_id"],
            )
            if has_any:
                ready = self._instance_directory.ready_instances(
                    release["service_id"],
                )
                if len(ready) == 1:
                    return ready[0].endpoint
                if not ready:
                    raise InstanceUnavailableError(
                        "服务没有 READY 实例（租约可能已过期）",
                    )
                raise InstanceUnavailableError(
                    "服务存在多个 READY 实例但未配置选择策略（一期仅支持单实例）",
                )
        return release["endpoint"]

    async def _get_card(self, endpoint: str, release: dict) -> dict:
        """拉取并缓存远端 Card; 校验契约 + 与 Release 摘要一致（fail-closed）。

        双路径发现（a2a-compat）：0.2.x 卡在 /.well-known/agent.json，
        0.3.x/1.x 卡在 /.well-known/agent-card.json，逐个尝试。"""
        cached = _card_cache.get(endpoint)
        if cached and cached[0] > time.monotonic():
            card = cached[1]
        else:
            transport = self._transport()
            card = None
            last_error: Exception | None = None
            for path in ("/.well-known/agent-card.json",):
                try:
                    card = await transport.get_json(
                        f"{endpoint.removesuffix('/rpc').rstrip('/')}{path}",
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
            if card is None:
                # 卡片重拉兜底（intranet-a2a-stream-adapter）: release.endpoint
                # 取自卡片 url 字段，可能与注册地址不同源（代理前缀分叉等，
                # 该 url 下不承载 well-known）。回落到注册行验证过的
                # agent_card_url 再试一轮；digest 校验不变。
                fallback = self._registration_card_url(release["service_id"])
                if fallback and fallback.rstrip("/") != endpoint.rstrip("/"):
                    for path in (
                        "/.well-known/agent-card.json",
                    ):
                        try:
                            card = await transport.get_json(
                                f"{fallback.rstrip('/')}{path}",
                            )
                            break
                        except Exception as exc:  # noqa: BLE001
                            last_error = exc
            if card is None:
                raise last_error or DependencyError("Agent Card 拉取失败")
            violations = schema_validate(card, AGENT_CARD_SCHEMA)
            if violations:
                raise DependencyError(
                    "远端 Agent Card 不符合 agent-card.v1 契约",
                )
            _card_cache[endpoint] = (
                time.monotonic() + settings.router_card_cache_ttl_s, card,
            )
        digest = catalog.card_digest(catalog.normalize_card(card))
        if digest != release["card_digest"]:
            raise DependencyError(
                "远端 Agent Card 与 Release 摘要不一致（远端已变更，"
                "请刷新并发布新 Release）",
            )
        return card

    @staticmethod
    def _transport() -> A2ATransport:
        return A2ATransport(
            timeout_s=settings.router_timeout_s or settings.tool_timeout_s,
            max_response_bytes=settings.router_max_response_bytes,
        )

    def _registration_card_url(self, service_id: str) -> str | None:
        """注册行的 agent_card_url（卡片重拉兜底用）。"""
        row = self._db.query_one(
            "SELECT agent_card_url FROM eag_a2a_service_registration"
            " WHERE service_id = :sid",
            {"sid": service_id},
        )
        return row["agent_card_url"] if row else None

    async def send_message(
        self,
        *,
        release: dict,
        context: RouterContext,
        user_text: str,
        capability: str | None = None,
        master_id: str | None = None,
        workspace_id: str | None = None,
    ) -> str:
        """受管 message/send；成功返回首个文本结果，失败抛统一错误。

        无论成败都先记 AGENT_CALL_STARTED / 后记 AGENT_CALL_FINISHED
        （失败不伪装成功，也不丢失审计，from_nekiro/04 §1）。"""
        child_invocation_id = new_invocation_id()
        headers = context.child_call_headers(
            child_invocation_id, workspace_id=workspace_id,
        )
        started = db_now()
        t0 = time.monotonic()
        ledger.append_event(
            self._db,
            event_type=ledger.AGENT_CALL_STARTED,
            status="RUNNING",
            invocation_id=child_invocation_id,
            trace_id=context.trace_id,
            root_task_id=context.root_task_id,
            parent_invocation_id=context.invocation_id,
            workspace_id=workspace_id or context.workspace_id,
            master_id=master_id,
            service_id=release["service_id"],
            release_id=release["release_id"],
            started_at=started,
        )

        error: Exception | None = None
        use_stream = False
        try:
            try:
                endpoint = self._resolve_endpoint(release)
                card = await self._get_card(endpoint, release)
            except Exception as exc:  # noqa: BLE001
                raise classify_transport_error(exc) from exc

            headers = self._with_credential(
                headers,
                endpoint=card_endpoint(card),
                release=release,
                context=context,
                child_invocation_id=child_invocation_id,
                workspace_id=workspace_id,
                capability=capability,
            )

            # R6 出站脱敏（from_hermes_a2a/02）: 凭证形字符串不发给远端 peer
            request_id = uuid.uuid4().hex
            payload = {"jsonrpc": "2.0", "id": request_id, "method": "SendMessage", "params": {
                "message": {"role": "ROLE_USER", "messageId": uuid.uuid4().hex,
                            "parts": [{"text": redact_outbound(user_text), "mediaType": "text/plain"}]}}}
            headers = {**headers, "A2A-Version": "1.0", "Accept": "application/json"}
            envelope = await self._transport().post_jsonrpc(card_endpoint(card), payload,
                headers=headers, request_id=request_id)
            return self._extract_text(envelope)
        except Exception as exc:  # noqa: BLE001
            error = classify_transport_error(exc)
            raise error from exc
        finally:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            ledger.append_event(
                self._db,
                event_type=ledger.AGENT_CALL_FINISHED,
                status="FAILED" if error is not None else "SUCCEEDED",
                invocation_id=child_invocation_id,
                trace_id=context.trace_id,
                root_task_id=context.root_task_id,
                parent_invocation_id=context.invocation_id,
                workspace_id=workspace_id or context.workspace_id,
                master_id=master_id,
                service_id=release["service_id"],
                release_id=release["release_id"],
                error_code=getattr(error, "code", None),
                started_at=started,
                finished_at=db_now(),
                metadata={
                    "elapsed_ms": elapsed_ms,
                    "release_version": release["agent_version"],
                    "transport": "stream" if use_stream else "send",
                },
            )

    def _with_credential(
        self,
        headers: dict[str, str | bytes],
        *,
        endpoint: str,
        release: dict,
        context: RouterContext,
        child_invocation_id: str,
        workspace_id: str | None,
        capability: str | None,
    ) -> dict[str, str | bytes]:
        """注入短期凭证（未配置签发方时原样返回）。

        claims 与平台上下文头/账本同源: 子调用的直接父是「当前调用」
        （context.invocation_id），而不是当前调用自己的 parent。"""
        # 服务级出站凭证（intranet-a2a-stream-adapter）: 注册时配置的
        # headers JSON（如内网对端的 apikey），解密失败 fail-closed。
        # db=None 仅出现在无库单测形态，跳过注入。
        if self._db is not None:
            cred_headers = catalog.outbound_headers(
                self._db, release["service_id"],
            )
            if cred_headers:
                headers = {**headers, **cred_headers}
        issuer = self._credential_issuer
        if issuer is None:
            issuer = get_credential_issuer()
        if issuer is None:
            return headers
        headers["Authorization"] = "Bearer " + issuer.issue(
            audience=endpoint.rstrip("/"),
            workspace_id=workspace_id or context.workspace_id or "",
            agent_id=release["service_id"],
            release_id=release["release_id"],
            card_digest=release["card_digest"],
            capability=capability or "SendMessage",
            subject_id=context.cap_user_name or "",
            display_name=context.real_name or "",
            invocation_id=child_invocation_id,
            root_task_id=context.root_task_id,
            parent_invocation_id=context.invocation_id,
            trace_id=context.trace_id,
        )
        return headers

    @staticmethod
    def _extract_text(envelope: dict) -> str:
        if envelope.get("error"):
            raise DependencyError("Remote A2A error: " + str(envelope["error"].get("code")))
        result = envelope.get("result") or {}
        if "message" in result:
            return _first_text(result["message"].get("parts")) or ""
        task = result.get("task")
        if not isinstance(task, dict):
            raise DependencyError("Invalid A2A 1.0 response")
        status = task.get("status", {})
        if status.get("state") in {"TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED", "TASK_STATE_AUTH_REQUIRED"}:
            raise DependencyError("Remote task: " + status["state"])
        if status.get("state") == "TASK_STATE_INPUT_REQUIRED":
            return "[INPUT_REQUIRED] " + (_first_text(status.get("message", {}).get("parts")) or "请补充信息")
        return "".join(p.get("text", "") for a in task.get("artifacts", []) for p in a.get("parts", []))

    @classmethod
    def clear_card_cache(cls) -> None:
        """测试/运维用: 清空 Card 缓存（Release 刷新后立即生效）。"""
        _card_cache.clear()
