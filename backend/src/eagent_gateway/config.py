# -*- coding: utf-8 -*-
"""网关配置: 全部来自环境变量（12-factor），无硬编码凭证。"""
import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger("eagent.config")


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _parse_routing_rules(raw: str) -> list[dict]:
    """M5: 解析 EAGENT_LLM_ROUTING_RULES（JSON 数组）；非法/为空 → []。"""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        rules = json.loads(raw)
        if not isinstance(rules, list):
            raise ValueError("必须是 JSON 数组")
        return [r for r in rules if isinstance(r, dict)]
    except (ValueError, TypeError):
        logger.warning("EAGENT_LLM_ROUTING_RULES 解析失败，已忽略: %r", raw[:120])
        return []


@dataclass
class Settings:
    # 数据库（集群内连 RDS）——默认不指向任何生产地址：生产/联调必须显式
    # 设置 EAGENT_DB_HOST（k8s.yaml env / compose.dev.yml / tests/conftest.py
    # 均已显式配置；缺省回落本机开发地址，漏配表现为连不上而非静默连生产）
    db_host: str = field(default_factory=lambda: _env(
        "EAGENT_DB_HOST", "127.0.0.1"))
    db_port: int = field(default_factory=lambda: int(_env("EAGENT_DB_PORT", "3306")))
    db_user: str = field(default_factory=lambda: _env("EAGENT_DB_USER", "eagent"))
    db_password: str = field(default_factory=lambda: _env("EAGENT_DB_PASSWORD", ""))
    db_name: str = field(default_factory=lambda: _env("EAGENT_DB_NAME", "eagent"))

    # LLM（OpenAI 兼容协议; 未配置 key 时进入 scripted 演示模式）
    llm_api_key: str = field(default_factory=lambda: _env("EAGENT_LLM_API_KEY", ""))
    llm_base_url: str = field(default_factory=lambda: _env(
        "EAGENT_LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"))
    llm_model: str = field(default_factory=lambda: _env("EAGENT_LLM_MODEL", "glm-4.7"))
    # 主 provider 的注册名：M5 routing rules 的 "provider" 字段与
    # build_chat_model(only=...) 都按它匹配。默认 glm 保持既有 routing
    # 配置兼容；指向其他厂商（deepseek/qwen/...）时改这里，端点与模型
    # 仍由 EAGENT_LLM_BASE_URL / EAGENT_LLM_MODEL 决定。
    # 空串按默认处理，避免 fallback 链里出现无名 provider。
    llm_provider_name: str = field(default_factory=lambda: _env(
        "EAGENT_LLM_PROVIDER_NAME", "glm").strip() or "glm")
    # fallback 链（from_hermes_a2a/04 M1）: "name:base:model:key" 以 ; 分隔
    llm_fallback: list[str] = field(default_factory=lambda: [
        s for s in _env("EAGENT_LLM_FALLBACK", "").split(";") if s.strip()
    ])
    # M2a: run_master 对瞬时错误（非业务 EAgentError）的退避重试间隔（秒）
    llm_retry_backoff_s: float = field(default_factory=lambda: float(
        _env("EAGENT_LLM_RETRY_BACKOFF_S", "1.0")))

    # LLM 出站连接池（model_factory.shared_http_client）: AgentScope 每轮
    # 新建 openai.AsyncClient，不共享连接池就要每轮重付 TLS 握手。
    # keepalive_expiry 要大于 ReAct 轮次之间的间隔（httpx 默认只有 5s，
    # 一轮生成就超了），也别长到超过服务端的空闲回收时间。
    llm_http_keepalive_expiry_s: float = field(default_factory=lambda: float(
        _env("EAGENT_LLM_HTTP_KEEPALIVE_EXPIRY_S", "90")))
    llm_http_max_keepalive: int = field(default_factory=lambda: int(
        _env("EAGENT_LLM_HTTP_MAX_KEEPALIVE", "20")))
    llm_http_max_connections: int = field(default_factory=lambda: int(
        _env("EAGENT_LLM_HTTP_MAX_CONNECTIONS", "100")))

    # M3（from_hermes_a2a/04）: Loop Engineer 独立模型（空则回退主 LLM 配置）
    opt_llm_api_key: str = field(default_factory=lambda: _env(
        "EAGENT_OPT_LLM_API_KEY", ""))
    opt_llm_base_url: str = field(default_factory=lambda:
        _env("EAGENT_OPT_LLM_BASE_URL", "") or _env(
            "EAGENT_LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"))
    opt_llm_model: str = field(default_factory=lambda:
        _env("EAGENT_OPT_LLM_MODEL", "") or _env("EAGENT_LLM_MODEL", "glm-4.7"))

    # M5（from_hermes_a2a/04）: smart routing 规则，JSON 数组，每条
    # {"match": ["关键词", ...], "provider": "注册名"}；第一个命中生效。
    # 解析失败按空处理（优化特性 fail-open，不影响主链路）
    llm_routing_rules: list[dict] = field(default_factory=lambda: _parse_routing_rules(
        _env("EAGENT_LLM_ROUTING_RULES", "")))

    # 服务
    host: str = field(default_factory=lambda: _env("EAGENT_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_env("EAGENT_PORT", "8080")))
    # 对外发布的网关地址（写进 Agent Card 的 url）
    public_url: str = field(default_factory=lambda: _env(
        "EAGENT_PUBLIC_URL", "http://localhost:8080"))

    # 运行时
    default_max_iters: int = field(default_factory=lambda: int(
        _env("EAGENT_DEFAULT_MAX_ITERS", "8")))
    tool_timeout_s: float = field(default_factory=lambda: float(
        _env("EAGENT_TOOL_TIMEOUT_S", "60")))
    # 仅限本地开发: 允许注册内网/回环地址的 stub 服务（生产禁止开启）
    allow_private_addresses: bool = field(default_factory=lambda: _env(
        "EAGENT_ALLOW_PRIVATE_ADDRESSES", "") in ("1", "true", "yes"))
    # 鉴权（fail-secure）: strict=默认，管理面要求 X-Admin-Token；
    # dev 仅本地开发。生产建议置于统一登录网关之后（T3 最终方案）
    auth_mode: str = field(default_factory=lambda: _env(
        "EAGENT_AUTH_MODE", "strict"))
    admin_token: str = field(default_factory=lambda: _env("EAGENT_ADMIN_TOKEN", ""))
    # 运行记录保留期（天）；0 = 禁用自动清理
    retention_days: int = field(default_factory=lambda: int(
        _env("EAGENT_RETENTION_DAYS", "90")))

    # ----- 管理面账户体系（admin-auth 01；login-topic 规范） -----
    # Pluto OAuth2（员工登录，授权码模式）。dev 环境在外网连不到内网 Pluto，
    # 占位配置 + admin 本地登录兜底；test/prod 内网联调。
    # token/密钥类值不入库不入 git、不写日志（安全红线）。
    pluto_access_key: str = field(default_factory=lambda: _env(
        "PLUTO_ACCESS_KEY", ""))
    pluto_access_secret: str = field(default_factory=lambda: _env(
        "PLUTO_ACCESS_SECRET", ""))
    pluto_authorize_url: str = field(default_factory=lambda: _env(
        "PLUTO_AUTHORIZE_URL", ""))
    pluto_token_url: str = field(default_factory=lambda: _env(
        "PLUTO_TOKEN_URL", ""))
    pluto_introspect_url: str = field(default_factory=lambda: _env(
        "PLUTO_INTROSPECT_URL", ""))
    pluto_userinfo_url: str = field(default_factory=lambda: _env(
        "PLUTO_USERINFO_URL", ""))
    pluto_redirect_uri: str = field(default_factory=lambda: _env(
        "PLUTO_REDIRECT_URI", ""))
    # 本地超级管理员（用户名固定 admin；密码不入库不入 git，三环境一致）
    admin_password: str = field(default_factory=lambda: _env(
        "ADMIN_PASSWORD", ""))
    admin_session_expire_hours: float = field(default_factory=lambda: float(
        _env("ADMIN_SESSION_EXPIRE_HOURS", "12")))
    console_session_expire_hours: float = field(default_factory=lambda: float(
        _env("CONSOLE_SESSION_EXPIRE_HOURS", "8")))
    # CAP 用户信息接口（wecom_id → badge_id 补全；空 = 跳过补全直接拒登）
    cap_user_base_url: str = field(default_factory=lambda: _env(
        "CAP_USER_BASE_URL", ""))
    # CAP/Pluto 用户目录（参考 Flowable：当前 Pluto 管理员 token 调 CAP manage）
    # http（默认）走 CAP manage 接口；database 查询当前库的 `user` 视图。
    cap_user_directory_backend: str = field(default_factory=lambda: _env(
        "CAP_USER_DIRECTORY_BACKEND", "http").strip().lower())
    # *_URL 可直接配置完整地址；未配时按 CAP_USER_BASE_URL + *_PATH 拼接。
    cap_user_search_url: str = field(default_factory=lambda: _env(
        "CAP_USER_SEARCH_URL", ""))
    cap_user_search_path: str = field(default_factory=lambda: _env(
        "CAP_USER_SEARCH_PATH", "/manage/organization/get_pluto_orgnazation_user"))
    cap_user_detail_url: str = field(default_factory=lambda: _env(
        "CAP_USER_DETAIL_URL", ""))
    cap_user_detail_path: str = field(default_factory=lambda: _env(
        "CAP_USER_DETAIL_PATH", "/manage/flowable/selectUserByUserName"))
    # Pluto 出站 HTTP 超时（秒，login-topic §3.2）
    pluto_timeout_s: float = field(default_factory=lambda: float(
        _env("PLUTO_TIMEOUT_S", "15")))
    # 权限申请联系方式（permission-control v1）：展示在「我的权限」页，
    # 引导用户线下找管理员开通服务可见性；空 = 前端展示默认文案
    admin_contact: str = field(default_factory=lambda: _env(
        "EAGENT_ADMIN_CONTACT", ""))

    # ----- P0-2/P0-3 受管调用（from_nekiro/02、03） -----
    # 默认工作空间（未显式安装时的全局工作空间）
    default_workspace_id: str = field(default_factory=lambda: _env(
        "EAGENT_DEFAULT_WORKSPACE", "default-workspace"))
    # 受管调用超时（秒）；0 = 沿用 tool_timeout_s
    router_timeout_s: float = field(default_factory=lambda: float(
        _env("EAGENT_ROUTER_TIMEOUT_S", "0") or 0))
    # A2A 出站最大响应体（字节）
    router_max_response_bytes: int = field(default_factory=lambda: int(
        _env("EAGENT_ROUTER_MAX_RESPONSE_BYTES", str(4 * 1024 * 1024))))
    # Agent Card 进程内缓存 TTL（秒）
    router_card_cache_ttl_s: float = field(default_factory=lambda: float(
        _env("EAGENT_ROUTER_CARD_CACHE_TTL_S", "60")))
    # 出站流式（message/stream）模式（intranet-a2a-stream-adapter）:
    # auto = 按卡片 capabilities.streaming 分流；always = 强制流式
    # （内网对端卡片未声明 streaming 时用）；never = 仅 message/send
    router_stream_mode: str = field(default_factory=lambda: _env(
        "EAGENT_ROUTER_STREAM", "auto").strip().lower() or "auto")
    # 单次流式消费的事件数上限（禁止无界消费）
    router_sse_max_events: int = field(default_factory=lambda: int(
        _env("EAGENT_ROUTER_SSE_MAX_EVENTS", "200")))
    # 单次流式消费的总字节上限（与 max_response_bytes 对齐口径）
    router_sse_max_bytes: int = field(default_factory=lambda: int(
        _env("EAGENT_ROUTER_SSE_MAX_BYTES", str(4 * 1024 * 1024))))

    # ----- P1-1 短期签名凭证（from_nekiro/05） -----
    # Ed25519 私钥（PEM 文本，或 PEM 文件路径）；未配置 = 不签发
    router_cred_private_key: str = field(default_factory=lambda: _env(
        "EAGENT_ROUTER_CRED_PRIVATE_KEY", ""))
    # Ed25519 公钥（PEM 文本，或 PEM 文件路径）；未配置 = 不校验
    router_cred_public_key: str = field(default_factory=lambda: _env(
        "EAGENT_ROUTER_CRED_PUBLIC_KEY", ""))
    router_cred_ttl_s: int = field(default_factory=lambda: int(
        _env("EAGENT_ROUTER_CRED_TTL_S", "30")))
    router_cred_issuer: str = field(default_factory=lambda: _env(
        "EAGENT_ROUTER_CRED_ISSUER", "eagent-router"))
    router_cred_key_id: str = field(default_factory=lambda: _env(
        "EAGENT_ROUTER_CRED_KEY_ID", "router-ed25519-1"))
    # 受管 Agent 的 audience 标识（校验侧必须一致）
    router_cred_audience: str = field(default_factory=lambda: _env(
        "EAGENT_ROUTER_CRED_AUDIENCE", "eagent-a2a-server"))
    # 入站 A2A 是否强制校验 Router 凭证（配置公钥后仍需显式开启）
    router_cred_verify_inbound: bool = field(default_factory=lambda: _env(
        "EAGENT_ROUTER_CRED_VERIFY_INBOUND", "") in ("1", "true", "yes"))

    # ----- HiAgent 接入（hiagent-integration/01；platform-spec hiagent-topic） -----
    # 全局默认服务地址：HIAGENT_URL（规范拼写）优先，HIAGNET_URL（qagent 历史
    # 拼写）兜底；服务地址必须由部署方显式配置。
    # 实例级 base_url（eag_hiagent_agent.base_url 非空）优先于本值。
    hiagent_url: str = field(default_factory=lambda: (
        _env("HIAGENT_URL", "").strip()
        or _env("HIAGNET_URL", "").strip()))
    # 全局兜底 api_key（实例级 app_key 加密落库者优先）
    hiagent_api_key: str = field(default_factory=lambda: _env(
        "HIAGENT_API_KEY", _env("HIAGNET_API_KEY", "")))
    # 读超时（秒；流式回答耗时长，connect 固定 5s）
    hiagent_timeout_s: float = field(default_factory=lambda: float(
        _env("HIAGENT_TIMEOUT", "300") or 300))
    # SSRF 豁免主机（逗号分隔；空 = 仅 HIAGENT_URL/实例 base_url 的主机并集）。
    # 只放开地址段校验，固定 IP/禁重定向/超时/大小上限全部保留
    hiagent_allow_hosts: list[str] = field(default_factory=lambda: [
        s.strip().lower() for s in
        _env("EAGENT_HIAGENT_ALLOW_HOST", "").split(",") if s.strip()
    ])

    # ----- P1-2 实例发现与租约（from_nekiro/06） -----
    instance_default_lease_s: int = field(default_factory=lambda: int(
        _env("EAGENT_INSTANCE_DEFAULT_LEASE_S", "30")))
    instance_sweep_interval_s: int = field(default_factory=lambda: int(
        _env("EAGENT_INSTANCE_SWEEP_INTERVAL_S", "15")))

    # ----- from_hermes_a2a M0（R1/R2/L1） -----
    # 同 context 最大连续轮数（硬顶 20；tasks/cancel 重置）
    max_pingpong_turns: int = field(default_factory=lambda: int(
        _env("EAGENT_MAX_PINGPONG_TURNS", "5")))
    # 跳过入站注入 framing 的 cap-user-name 白名单（逗号分隔，默认无）
    a2a_inbound_trusted: list[str] = field(default_factory=lambda: [
        s for s in _env("EAGENT_A2A_INBOUND_TRUSTED", "").split(",") if s.strip()
    ])
    # Loop Engineer 候选 PROPOSE 的最小提升边际（防过拟合）
    opt_min_gain: float = field(default_factory=lambda: float(
        _env("EAGENT_OPT_MIN_GAIN", "0.02")))
    # 优化任务判定为"僵尸"的时长（秒）。任务跑在 BackgroundTasks 里，进程重启
    # （dev 的 --reload、生产的滚动更新）会把在跑的任务直接杀掉，而库里那行
    # 会永远停在 RUNNING——没有超时兜底的话前端只能一直转圈。
    opt_run_timeout_s: int = field(default_factory=lambda: int(
        _env("EAGENT_OPT_RUN_TIMEOUT_S", "1800")))

    # ----- from_hermes_a2a M2（R5/M4/M5） -----
    # R5: 单次 run_master wall-clock 超时（秒）；0 = 禁用（沿用 max_iters 预算）
    master_timeout_s: float = field(default_factory=lambda: float(
        _env("EAGENT_MASTER_TIMEOUT_S", "0") or 0))
    # M4: 辅助模型（失败样本摘要等；未配置时消费方自动跳过）
    aux_llm_api_key: str = field(default_factory=lambda: _env(
        "EAGENT_AUX_LLM_API_KEY", ""))
    aux_llm_base_url: str = field(default_factory=lambda:
        _env("EAGENT_AUX_LLM_BASE_URL", "") or _env(
            "EAGENT_LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"))
    aux_llm_model: str = field(default_factory=lambda:
        _env("EAGENT_AUX_LLM_MODEL", "") or _env("EAGENT_LLM_MODEL", "glm-4.7"))

    # ----- KB 知识库（from_llm_wiki 01–05；检索/存储改造 D-2026-08-16-02/08） -----
    # Runtime 内部只读令牌（/internal/kb/*，与 x-admin-token 不同密钥，D6/L1）；
    # 未配置时进程内自动生成（L1 同进程内 caller 直接读该值，等价持有令牌）
    kb_internal_token: str = field(default_factory=lambda: _env(
        "EAGENT_KB_INTERNAL_TOKEN", ""))
    # ⚠️ 已废弃（D-2026-08-16-02 去向量化，08 §3.4）：向量库不再使用；
    # 配置项保留仅为兼容既有部署 env，无任何效果；kb_vectors/ 停写
    kb_vector_backend: str = field(default_factory=lambda: _env(
        "EAGENT_KB_VECTOR_BACKEND", "auto"))
    kb_vector_dir: str = field(default_factory=lambda: _env(
        "EAGENT_KB_VECTOR_DIR", "kb_vectors"))
    # 本地 PVC 主存储根目录（raw/parsed/wiki；grep/glob 检索载体 + OSS 同步源）
    kb_storage_dir: str = field(default_factory=lambda: _env(
        "EAGENT_KB_STORAGE_DIR", "kb_data"))
    # kb_search 工具路径的结果窗口：top_k_default(20) 是 llm-wiki 式
    # 「上下文预算」口径，直接进 ReAct 上下文会把 ~20 页 snippet（实测
    # ~21KB）逐轮重复计入后续每次 LLM 调用；工具场景只需要定位页面
    # （配合 kb_read 细读），默认收敛到 6，管理面预览不受影响
    kb_tool_top_k: int = field(default_factory=lambda: int(
        _env("EAGENT_KB_TOOL_TOP_K", "6")))
    # DEV-12: 健康定时探测调度（秒；0 关闭）与告警
    health_probe_interval_s: int = field(default_factory=lambda: int(
        _env("EAGENT_HEALTH_PROBE_INTERVAL_S", "60")))
    health_probe_consecutive_down: int = field(default_factory=lambda: int(
        _env("EAGENT_HEALTH_PROBE_CONSECUTIVE_DOWN", "2")))
    alert_webhook_url: str = field(default_factory=lambda: _env(
        "EAGENT_ALERT_WEBHOOK_URL", ""))
    alert_silence_s: int = field(default_factory=lambda: int(
        _env("EAGENT_ALERT_SILENCE_S", "300")))
    # 远程日志 HTTP 摄取令牌（空 = 端点拒绝写入，fail-closed）
    remote_log_token: str = field(default_factory=lambda: _env(
        "EAGENT_REMOTE_LOG_TOKEN", ""))
    # DEV-04: 总控 A2A 端点是否声明并启用 message/stream（SSE 流式推送
    # 任务状态迁移 + ReAct 工具调用中间事件）。关闭后回到纯 message/send
    a2a_streaming: bool = field(default_factory=lambda: _env(
        "EAGENT_A2A_STREAMING", "1") in ("1", "true", "yes"))
    # 流程型绑定的活跃流程空闲超时（秒）：超时后下一轮回到总控重新分诊
    flow_idle_s: int = field(default_factory=lambda: int(
        _env("EAGENT_FLOW_IDLE_S", "1800")))
    # 入库规划服务（04 §2.3/§6.3）
    kb_planner_enabled: bool = field(default_factory=lambda: _env(
        "EAGENT_KB_PLANNER_ENABLED", "1") in ("1", "true", "yes"))
    kb_planner_debounce_s: float = field(default_factory=lambda: float(
        _env("EAGENT_KB_PLANNER_DEBOUNCE_S", "30")))
    kb_planner_budget_s: float = field(default_factory=lambda: float(
        _env("EAGENT_KB_PLANNER_BUDGET_S", "120")))
    # ⚠️ 已废弃（D-2026-08-16-02 去向量化，08 §3.4）：embedding 不再被
    # 检索/编译调用；配置项保留仅为兼容既有部署 env
    kb_embed_api_key: str = field(default_factory=lambda: _env(
        "EAGENT_KB_EMBED_API_KEY", ""))
    kb_embed_base_url: str = field(default_factory=lambda: _env(
        "EAGENT_KB_EMBED_BASE_URL", ""))
    # embedding 默认模型名（部署层 env 缺省；KB.embedding_config.model 可覆盖）
    kb_embed_model: str = field(default_factory=lambda: _env(
        "EAGENT_KB_EMBED_MODEL", ""))
    # 编译任务循环间隔（秒）
    kb_task_poll_s: float = field(default_factory=lambda: float(
        _env("EAGENT_KB_TASK_POLL_S", "0.5")))
    # Rev GC（03 §4.3）：保留窗口默认 5（constants.REV_RETAIN_WINDOW）；0 = 禁用循环
    kb_gc_interval_s: float = field(default_factory=lambda: float(
        _env("EAGENT_KB_GC_INTERVAL_S", str(24 * 3600))))
    kb_gc_retain_revs: int = field(default_factory=lambda: int(
        _env("EAGENT_KB_GC_RETAIN_REVS", "5")))
    # 上传源大小上限（字节，默认 20MB）
    kb_max_upload_bytes: int = field(default_factory=lambda: int(
        _env("EAGENT_KB_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024))))
    # 权限 scope 映射（02 §7.1：cap-user → scope，从零新建；逗号分隔）。
    # 任一映射配置后按显式映射判定（deny-by-default）；全部未配置 = 单管理员
    # 模式（管理面本就由 X-Admin-Token 统一放行，与现状一致）
    kb_scope_managers: list[str] = field(default_factory=lambda: [
        s for s in _env("EAGENT_KB_SCOPE_MANAGERS", "").split(",") if s.strip()
    ])
    kb_scope_reviewers: list[str] = field(default_factory=lambda: [
        s for s in _env("EAGENT_KB_SCOPE_REVIEWERS", "").split(",") if s.strip()
    ])
    kb_scope_agent_admins: list[str] = field(default_factory=lambda: [
        s for s in _env("EAGENT_KB_SCOPE_AGENT_ADMINS", "").split(",") if s.strip()
    ])

    # ----- KB 对象存储同步与对账（08 §4，D-2026-08-16-02 / R-05/R-06） -----
    # S3 兼容协议（本地 MinIO / k8s MinIO / 阿里云 OSS 只换 endpoint，零代码改动）
    kb_oss_endpoint: str = field(default_factory=lambda: _env(
        "EAGENT_KB_OSS_ENDPOINT", ""))
    kb_oss_bucket: str = field(default_factory=lambda: _env(
        "EAGENT_KB_OSS_BUCKET", "eagent-kb"))
    kb_oss_prefix: str = field(default_factory=lambda: _env(
        "EAGENT_KB_OSS_PREFIX", ""))
    kb_oss_access_key: str = field(default_factory=lambda: _env(
        "EAGENT_KB_OSS_ACCESS_KEY", ""))
    kb_oss_secret_key: str = field(default_factory=lambda: _env(
        "EAGENT_KB_OSS_SECRET_KEY", ""))
    # 同步循环间隔（秒）；0 = 禁用（默认）。本地开发 60、测试/生产 3600（08 §4.2）
    kb_oss_sync_interval_s: float = field(default_factory=lambda: float(
        _env("EAGENT_KB_OSS_SYNC_INTERVAL_S", "0")))
    # 三方对账循环间隔（秒，默认每日）；0 = 禁用（08 §4.3）
    kb_reconcile_interval_s: float = field(default_factory=lambda: float(
        _env("EAGENT_KB_RECONCILE_INTERVAL_S", str(24 * 3600))))

    # ----- KB MCP 对外暴露（09，streamable-http，/mcp/kb） -----
    # 总开关：1 = 挂载 /mcp/kb（FastMCP ASGI）；默认 0 = 不挂载零影响。
    # ⚠️ 本期无鉴权（09 §8：上线前必补 API key/仅内网/限流审计）
    kb_mcp_enabled: bool = field(default_factory=lambda: _env(
        "EAGENT_KB_MCP_ENABLED", "") in ("1", "true", "yes"))
    # slug 白名单（逗号分隔）；空 = 全部 READY KB（仅 READY+current_rev 可暴露）
    kb_mcp_kb_slugs: list[str] = field(default_factory=lambda: [
        s for s in _env("EAGENT_KB_MCP_KB_SLUGS", "").split(",") if s.strip()
    ])
    # 单次检索 wall-clock 预算（fail-closed KB_UNAVAILABLE）
    kb_mcp_timeout_s: float = field(default_factory=lambda: float(
        _env("EAGENT_KB_MCP_TIMEOUT_S", "10")))

    # ----- 性能测试（perf-test 01 §5/§7，独立 perf-worker 进程） -----
    # 数据集上传大小上限（字节，默认 20MB）
    perf_max_upload_bytes: int = field(default_factory=lambda: int(
        _env("EAGENT_PERF_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024))))
    # 单数据集最大行数（默认 10 万）
    perf_max_rows: int = field(default_factory=lambda: int(
        _env("EAGENT_PERF_MAX_ROWS", "100000")))
    # perf-worker 轮询间隔（秒）
    # 单 Pod 部署形态：perf-worker 以任务形式内嵌 manager 进程（省一个独立 Deployment）
    perf_worker_embedded: bool = field(default_factory=lambda: _env(
        "EAGENT_PERF_WORKER_EMBEDDED", "0") == "1")
    perf_worker_poll_s: float = field(default_factory=lambda: float(
        _env("EAGENT_PERF_WORKER_POLL_S", "2")))
    # 样本批量落库阈值：条数 / 间隔秒（02 §5.2：每 50 条或 1 秒 flush）
    perf_sample_flush_size: int = field(default_factory=lambda: int(
        _env("EAGENT_PERF_SAMPLE_FLUSH_SIZE", "50")))
    perf_sample_flush_interval_s: float = field(default_factory=lambda: float(
        _env("EAGENT_PERF_SAMPLE_FLUSH_INTERVAL_S", "1")))

    # ----- Higress 网关配置控制面（higress-mcp-gateway 01/03） -----
    # consumer 密钥主密钥（base64 编码的 32 字节）；fail-secure：
    # gateway 启用而未配置时拒绝启动（03 T12）
    gateway_master_key: str = field(default_factory=lambda: _env(
        "EAGENT_GATEWAY_MASTER_KEY", ""))
    # 功能开关：置 1 时启动即校验主密钥（未配置拒绝启动）；
    # 未开启时首次用到密钥的操作才失败（可选功能不拖垮共用部署）
    gateway_enabled: bool = field(default_factory=lambda: _env(
        "EAGENT_GATEWAY_ENABLED", "") in ("1", "true", "yes"))
    # consumer 轮换宽限期（秒，默认 300）：期内新旧 key 同时有效（03 T13）
    gateway_key_grace_seconds: int = field(default_factory=lambda: int(
        _env("EAGENT_GATEWAY_KEY_GRACE_SECONDS", "300")))
    # 宽限期到期清扫间隔（秒；0 = 禁用后台清扫，由下次读/写时惰性收敛）
    gateway_grace_sweep_interval_s: float = field(default_factory=lambda: float(
        _env("EAGENT_GATEWAY_GRACE_SWEEP_INTERVAL_S", "60")))
    # kubeconfig 文件路径（本地/dev 直连集群用；集群内自动走 in-cluster SA）
    gateway_kubeconfig: str = field(default_factory=lambda: _env(
        "EAGENT_KUBECONFIG", ""))
    # 启动 seed（03 T06）：三件套齐备时自动建 profile（幂等）
    gateway_profile_name: str = field(default_factory=lambda: _env(
        "EAGENT_GATEWAY_PROFILE_NAME", ""))
    gateway_profile_higress_ns: str = field(default_factory=lambda: _env(
        "EAGENT_GATEWAY_HIGRESS_NS", ""))
    gateway_profile_kube_context: str = field(default_factory=lambda: _env(
        "EAGENT_GATEWAY_KUBE_CONTEXT", ""))
    # gateway:manage scope 映射（与 KB 同法；未配置 = 单管理员模式放行）
    gateway_scope_managers: list[str] = field(default_factory=lambda: [
        s for s in _env("EAGENT_GATEWAY_SCOPE_MANAGERS", "").split(",")
        if s.strip()
    ])

    @property
    def db_dsn(self) -> str:
        from urllib.parse import quote_plus
        pwd = quote_plus(self.db_password)
        return (
            f"mysql+pymysql://{self.db_user}:{pwd}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
            "?charset=utf8mb4"
        )


settings = Settings()
