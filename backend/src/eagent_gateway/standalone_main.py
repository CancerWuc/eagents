# -*- coding: utf-8 -*-
"""独立部署容器入口（standalone-deployment 01 §6，S-04/S-05）。

容器内 = 总控 ReAct + KB 只读快照检索 + 模型调用，**不依赖平台进程、
不依赖 MySQL**（镜像不装 MySQL driver 可启动）：

- 读 env：EAGENT_STANDALONE_CARD_PATH / EAGENT_STANDALONE_KB_SNAPSHOT_DIR /
  EAGENT_STANDALONE_DEPLOY_DIGEST / EAGENT_STANDALONE_IMAGE_TAG /
  EAGENT_STANDALONE_AGENT_ID / EAGENT_LLM_*；
- 启动校验（全部 fail-closed，不符拒绝启动）：
  card_format / snapshot_format_version（每 KB manifest）/ deploy_digest
  三件套（card_digest + 快照哈希 + 镜像 tag，§7.1 防篡改/错配）；
- KB 工具装配：build_kb_tools_from_snapshot（绑定集来自 card，slug 来自
  快照 manifest；card 无 KB 绑定或快照根缺失 → 不装配，绑定了却缺快照 →
  拒绝启动）；A2ATool 不装配（一期边界 §1）；
- 模型解析（S-05，与平台同一「per-master 优先」语义，master-model-config
  01）：card 声明 base_url/model → api_key 从 EAGENT_LLM_API_KEY 注入
  （声明了模型而未供 key = 配置错误，拒绝启动）；无声明 → 整体走
  EAGENT_LLM_*（空 key 时回退 ScriptedRouterModel 演示模式，同平台
  「空 = 平台默认」）；容器内**不做 M5 智能路由**；
- 总控装配语义复制 master_service（发布版 prompt/工具清单/react_config/
  DONT_ASK/防环/INPUT_REQUIRED/工具失败态 fail-closed），去 DB 化——
  运行台账落容器日志（stdout JSON line，带 EAGENT_STANDALONE_AGENT_ID）。

运行：python -m eagent_gateway.standalone_main
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .config import settings
from .kb.backends import SNAPSHOT_FORMAT_VERSION
from .runtime import turn_tracker
from .runtime.kb_tool import build_kb_tools_from_snapshot
from .runtime.prompt_middleware import DbPromptMiddleware
from .runtime.run_semantics import extract_input_required
from .standalone_card import load_card, verify_deploy_digest

logger = logging.getLogger("eagent.standalone")


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default) or default


# ---------------------------------------------------------------------------
# 模型解析（S-05）：per-master 优先（card 声明 base_url/model，key 一律 env）
# ---------------------------------------------------------------------------
def resolve_model(agent_cfg: dict):
    """返回 (model, is_scripted, resolved_from)。

    - card.model_config 有 model：api_key 取 EAGENT_LLM_API_KEY（缺 →
      ValueError fail-closed：部署方按 card 声明供 key）；base_url 用 card
      声明（缺省回退 EAGENT_LLM_BASE_URL）；
    - card 无声明：整体 EAGENT_LLM_*（同平台「空 = 平台默认」）；
    - 两路都无 key：ScriptedRouterModel（演示/CI，与平台一致，warn 提示）。

    容器内不做 M5 智能路由（路由规则属平台 env 侧能力，不随 card 导出）。
    """
    from agentscope.credential import OpenAICredential
    from agentscope.model import OpenAIChatModel
    from .runtime.model_factory import ScriptedRouterModel

    env_key = (settings.llm_api_key or "").strip()
    cfg = agent_cfg.get("model_config") or {}
    card_model = (cfg.get("model") or "").strip()
    if card_model:
        if not env_key:
            raise ValueError(
                "card 声明了模型但未注入 api_key：请设置 EAGENT_LLM_API_KEY"
                f"（card.model.model={card_model}）",
            )
        base_url = (cfg.get("base_url") or "").strip() or None
        return OpenAIChatModel(
            credential=OpenAICredential(api_key=env_key, base_url=base_url),
            model=card_model,
            stream=False,
            max_retries=2,
        ), False, "card"
    if env_key:
        return OpenAIChatModel(
            credential=OpenAICredential(
                api_key=env_key,
                base_url=settings.llm_base_url or None,
            ),
            model=settings.llm_model,
            stream=False,
            max_retries=2,
        ), False, "env"
    logger.warning(
        "未配置任何模型 key（EAGENT_LLM_API_KEY），回退 ScriptedRouterModel"
        "演示模式——仅用于链路演示/CI，生产必须配置真实模型",
    )
    return ScriptedRouterModel(), True, "scripted"


# ---------------------------------------------------------------------------
# 启动装配（fail-closed：任一校验失败抛异常 → 容器退出）
# ---------------------------------------------------------------------------
class StandaloneRuntime:
    """启动期一次装配；每请求复用（Toolkit/中间件只读，state 按会话隔离）。"""

    def __init__(self) -> None:
        self.agent_id = _env("EAGENT_STANDALONE_AGENT_ID", "standalone-agent")
        card_path = _env("EAGENT_STANDALONE_CARD_PATH")
        if not card_path:
            raise ValueError("缺少 EAGENT_STANDALONE_CARD_PATH（card.json 路径）")
        snapshot_dir = _env("EAGENT_STANDALONE_KB_SNAPSHOT_DIR")
        image_tag = _env("EAGENT_STANDALONE_IMAGE_TAG")
        expected_digest = _env("EAGENT_STANDALONE_DEPLOY_DIGEST")

        self.card = load_card(card_path)
        self.card_path = card_path
        agent = self.card["agent"]
        self.slug = agent["slug"]

        # deploy_digest 复核（建议配置；不符 = 三件套错配/篡改，拒绝启动）
        if expected_digest:
            if not image_tag:
                raise ValueError(
                    "配置了 EAGENT_STANDALONE_DEPLOY_DIGEST 但缺"
                    " EAGENT_STANDALONE_IMAGE_TAG（digest 绑定镜像 tag，§7.1）",
                )
            verify_deploy_digest(
                card_path, snapshot_dir, image_tag, expected_digest,
            )
            logger.info("deploy_digest 校验通过（三件套联合锁定）")

        # KB 工具（快照模式）：绑定了却缺快照 → fail-closed
        bindings = self.card.get("kb_bindings") or []
        self.kb_tools = []
        if bindings:
            if not snapshot_dir:
                raise ValueError(
                    f"card 声明了 {len(bindings)} 个 KB 绑定但缺"
                    " EAGENT_STANDALONE_KB_SNAPSHOT_DIR",
                )
            self.kb_tools = build_kb_tools_from_snapshot(snapshot_dir, bindings)
            logger.info(
                "KB 快照工具装配: %d 个绑定 → %d 个工具",
                len(bindings), len(self.kb_tools),
            )

        # 模型（S-05 per-master 优先）
        self.model, self.is_scripted, self.model_from = resolve_model(agent)

        # 发布版 prompt / react_config（复制 master_service 装配语义）
        react_cfg = agent.get("react_config") or {}
        self.max_iters = int(react_cfg.get("max_iters", settings.default_max_iters))
        self.tool_lines = [f"{t.name}: {t.description}" for t in self.kb_tools]
        self.middleware = DbPromptMiddleware(
            prompt_text=agent.get("prompt_text"),
            tool_lines=self.tool_lines,
        )
        self.tracker = turn_tracker.TurnTracker(settings.max_pingpong_turns)
        self.snapshot_format_version = SNAPSHOT_FORMAT_VERSION

    # -- 运行（复制 run_master 语义，去 DB 化，台账落 stdout） --------------
    async def run(self, context_id: str, user_text: str) -> dict:
        from agentscope.agent import Agent, ReActConfig
        from agentscope.message import Msg, TextBlock, ToolResultState
        from agentscope.permission import PermissionContext, PermissionMode
        from agentscope.state import AgentState
        from agentscope.tool import Toolkit

        run_id = f"run_{uuid.uuid4().hex[:24]}"
        t0 = time.monotonic()
        status = "SUCCEEDED"
        error_code: str | None = None
        final_text = ""
        input_required = False
        routed: list[str] = []
        tool_calls_log: list[dict] = []

        if not self.tracker.register_turn(context_id):
            status = "REJECTED"
            error_code = "ANTI_LOOP_REJECTED"
            final_text = (
                f"连续对话轮数超过上限（{self.tracker.max_turns}），"
                "请新建上下文或稍后再试。"
            )
            self._log_run(run_id, context_id, status, error_code, user_text,
                          final_text, routed, tool_calls_log, t0)
            return {"run_id": run_id, "status": status,
                    "error_code": error_code, "output": final_text,
                    "input_required": False, "routed_tools": routed}

        state = AgentState(session_id=context_id)
        state.permission_context = PermissionContext(
            mode=PermissionMode.DONT_ASK,
        )
        agent = Agent(
            name=f"standalone-{self.slug}",
            system_prompt="",
            model=self.model,
            toolkit=Toolkit(tools=list(self.kb_tools)),
            middlewares=[self.middleware],
            state=state,
            react_config=ReActConfig(max_iters=self.max_iters),
        )
        try:
            final_msg = await asyncio.wait_for(
                agent.reply(Msg(
                    name="user", role="user",
                    content=[TextBlock(text=user_text)],
                )),
                timeout=settings.master_timeout_s or None,
            )
            final_text = final_msg.get_text_content() or ""
            input_required, final_text = extract_input_required(final_text)
            failed_tools: list[tuple[str, str]] = []
            for m in state.context:
                for blk in m.content:
                    if getattr(blk, "type", None) == "tool_result":
                        routed.append(blk.name)
                        output = blk.output
                        if isinstance(output, list):
                            output = " ".join(
                                getattr(x, "text", "") or "" for x in output
                            )
                        tool_calls_log.append(
                            {"tool": blk.name, "state": str(blk.state)},
                        )
                        if str(blk.state) != str(ToolResultState.SUCCESS):
                            failed_tools.append((blk.name, output or ""))
            if agent.state.cur_iter >= self.max_iters:
                status = "LIMIT_REACHED"
            if failed_tools:
                # P0-5 fail-closed 兜底（与 run_master 同语义）：上下文存在
                # 非成功态工具结果即 FAILED，绝不伪装成功
                status = "FAILED"
                error_code = "DEPENDENCY_ERROR"
                final_text = "处理失败，请稍后重试。"
        except asyncio.TimeoutError:
            status = "FAILED"
            error_code = "TIMEOUT"
            final_text = "处理失败，请稍后重试。"
        except asyncio.CancelledError:
            self._log_run(run_id, context_id, "CANCELED", "CANCELED",
                          user_text, "请求已取消。", routed, tool_calls_log, t0)
            raise
        except Exception as exc:  # noqa: BLE001
            status = "FAILED"
            error_code = "INTERNAL_ERROR"
            error_detail = str(exc)[:500]
            logger.exception("standalone run failed run_id=%s", run_id)
            final_text = "处理失败，请稍后重试。"
            self._log_run(run_id, context_id, status, error_code, user_text,
                          final_text, routed, tool_calls_log, t0,
                          error_detail=error_detail)
            return {"run_id": run_id, "status": status,
                    "error_code": error_code, "output": final_text,
                    "input_required": False, "routed_tools": routed}

        self._log_run(run_id, context_id, status, error_code, user_text,
                      final_text, routed, tool_calls_log, t0)
        return {"run_id": run_id, "status": status, "error_code": error_code,
                "output": final_text, "input_required": input_required,
                "routed_tools": routed}

    def _log_run(self, run_id, context_id, status, error_code,  # noqa: ANN001
                 user_text, final_text, routed, tool_calls, t0,  # noqa: ANN001
                 error_detail=None) -> None:
        """运行台账落容器日志（stdout JSON line；脱敏摘要，不落原文全文）。"""
        from .security.redact import digest_of, redact_text

        logger.info(
            "RUN %s",
            json.dumps({
                "agent_id": self.agent_id, "run_id": run_id,
                "context_id": context_id, "status": status,
                "error_code": error_code,
                "input_digest": digest_of(redact_text(user_text)),
                "output_digest": digest_of(redact_text(final_text)),
                "routed_tools": routed, "tool_calls": tool_calls,
                "elapsed_ms": int((time.monotonic() - t0) * 1000),
                **({"error_detail": error_detail} if error_detail else {}),
            }, ensure_ascii=False),
        )


# ---------------------------------------------------------------------------
# HTTP 面（单会话/常驻服务形态：/chat + /healthz + /）
# ---------------------------------------------------------------------------
class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=16384)
    context_id: str = Field(default="default", max_length=128)


def create_standalone_app() -> FastAPI:
    """构造独立容器 FastAPI 应用（装配失败抛异常 → 容器拒绝启动）。"""
    rt = StandaloneRuntime()
    app = FastAPI(title=f"eAgent Standalone: {rt.slug}", version="1")
    app.state.runtime = rt

    @app.get("/healthz")
    def healthz():
        return {
            "status": "ok",
            "agent_id": rt.agent_id,
            "slug": rt.slug,
            "kb_tools": len(rt.kb_tools),
            "model_from": rt.model_from,
            "scripted": rt.is_scripted,
            "snapshot_format_version": rt.snapshot_format_version,
        }

    @app.get("/")
    def info():
        release = rt.card.get("release") or {}
        return {
            "agent": rt.slug,
            "display_name": rt.card["agent"].get("display_name"),
            "release": {
                "release_id": release.get("release_id"),
                "agent_version": release.get("agent_version"),
            },
            "tools": [t.name for t in rt.kb_tools],
            "endpoints": ["POST /chat", "GET /healthz"],
        }

    @app.post("/chat")
    async def chat(body: ChatIn):
        return await rt.run(body.context_id, body.message)

    return app


def main() -> None:  # pragma: no cover - 进程入口
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    import uvicorn

    app = create_standalone_app()
    uvicorn.run(
        app,
        host=_env("EAGENT_STANDALONE_HOST", settings.host),
        port=int(_env("EAGENT_STANDALONE_PORT", str(settings.port))),
        log_level="info",
    )


if __name__ == "__main__":  # pragma: no cover
    main()
