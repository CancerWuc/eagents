-- eAgent A2A 网关 v1 数据模型（MySQL 5.7 兼容）
-- 依据: eAgent-A2A网关v1设计说明书.md §8
-- 约定: 全部 TIMESTAMP(6) NOT NULL 由应用写入；不使用外键约束（服务内保证）；
--       JSON 字段为事实来源；主键为应用生成的 32 位 ID。

CREATE DATABASE IF NOT EXISTS eagent
    DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE eagent;

-- --------------------------------------------------------------------
-- 迁移登记表
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eag_schema_migration (
    version_no        INT          NOT NULL PRIMARY KEY,
    name              VARCHAR(128) NOT NULL,
    applied_at        TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- --------------------------------------------------------------------
-- 能力② A2A 服务注册 + Card 快照 + 健康观测
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eag_a2a_service_registration (
    service_id          VARCHAR(32)  NOT NULL PRIMARY KEY,
    slug                VARCHAR(64)  NOT NULL UNIQUE,
    display_name        VARCHAR(128) NOT NULL,
    agent_card_url      VARCHAR(512) NOT NULL,
    auth_secret_ref     VARCHAR(1024) NULL,
    tags                VARCHAR(512) NULL,
    enabled             TINYINT(1)   NOT NULL DEFAULT 1,
    compatibility       VARCHAR(20)  NOT NULL DEFAULT 'VERIFYING',
    compatibility_reason VARCHAR(512) NULL,
    current_revision_id VARCHAR(32)  NULL,
    source_type         VARCHAR(20)  NOT NULL DEFAULT 'A2A_REMOTE' COMMENT 'A2A_REMOTE | HIAGENT（027）',
    created_at          TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at          TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_reg_enabled (enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS eag_a2a_card_revision (
    revision_id       VARCHAR(32)  NOT NULL PRIMARY KEY,
    service_id        VARCHAR(32)  NOT NULL,
    protocol_version  VARCHAR(32)  NOT NULL,
    card_json         JSON         NOT NULL,
    normalized_json   JSON         NOT NULL,
    content_digest    CHAR(64)     NOT NULL,
    compatibility     VARCHAR(20)  NOT NULL,
    fetched_at        TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_rev_service (service_id, fetched_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS eag_a2a_health_observation (
    observation_id    VARCHAR(32)  NOT NULL PRIMARY KEY,
    service_id        VARCHAR(32)  NOT NULL,
    health            VARCHAR(20)  NOT NULL,
    latency_ms        INT          NULL,
    detail            VARCHAR(1024) NULL,
    observed_at       TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_health_service (service_id, observed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- --------------------------------------------------------------------
-- HiAgent 接入（027）：HIA 专属配置 + 会话映射
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eag_hiagent_agent (
    service_id       VARCHAR(32)  NOT NULL PRIMARY KEY COMMENT '= 目录表 service_id',
    base_url         VARCHAR(512) NULL COMMENT 'NULL=全局默认（HIAGENT_URL 兜底链）',
    app_key_enc      VARBINARY(512) NULL COMMENT 'AES-GCM；NULL=全局 HIAGENT_API_KEY 兜底',
    agent_config_json JSON        NULL COMMENT '生成式 Card 素材（skills/描述等）',
    created_by       VARCHAR(64)  NOT NULL DEFAULT 'admin',
    created_at       TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at       TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS eag_hiagent_conversation (
    context_id       VARCHAR(64)  NOT NULL,
    service_id       VARCHAR(32)  NOT NULL,
    conversation_id  VARCHAR(128) NOT NULL,
    subject_id       VARCHAR(64)  NOT NULL DEFAULT '',
    identity_digest  CHAR(64)     NOT NULL DEFAULT '',
    updated_at       TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (context_id, service_id, subject_id),
    INDEX idx_hia_conv_ttl (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- --------------------------------------------------------------------
-- 能力③ 总控 Agent（COMPOSITE，一等公民，多实例）+ 绑定 + 提示词版本
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eag_master_agent (
    master_id                 VARCHAR(32)  NOT NULL PRIMARY KEY,
    slug                      VARCHAR(64)  NOT NULL UNIQUE,
    display_name              VARCHAR(128) NOT NULL,
    description               VARCHAR(1024) NULL,
    source_type               VARCHAR(20)  NOT NULL DEFAULT 'COMPOSITE',
    enabled                   TINYINT(1)   NOT NULL DEFAULT 1,
    current_prompt_version_id VARCHAR(32)  NULL,
    react_config_json         JSON         NULL,
    model_config_json         JSON         NULL COMMENT '总控 Agent 独立模型配置（{provider,base_url,model,api_key}；空=平台默认）',
    created_at                TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS eag_master_binding (
    binding_id        VARCHAR(32)  NOT NULL PRIMARY KEY,
    master_id         VARCHAR(32)  NOT NULL,
    service_id        VARCHAR(32)  NOT NULL,
    release_id        VARCHAR(32)  NULL,
    tool_name         VARCHAR(64)  NOT NULL,
    tool_description  TEXT         NOT NULL,
    enabled           TINYINT(1)   NOT NULL DEFAULT 1,
    -- TOOL: LLM 转述/汇总；FLOW: 只选目标，原话透传、原样返回（032）
    interaction_mode  VARCHAR(16)  NOT NULL DEFAULT 'TOOL',
    created_at        TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_binding (master_id, service_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 总控会话当前归属的流程型绑定（032）：只存路由元数据，不存输入输出
CREATE TABLE IF NOT EXISTS eag_master_active_flow (
    master_id   VARCHAR(32)  NOT NULL,
    context_id  VARCHAR(128) NOT NULL,
    subject_id  VARCHAR(128) NOT NULL,
    binding_id  VARCHAR(32)  NOT NULL,
    service_id  VARCHAR(32)  NOT NULL,
    awaiting    VARCHAR(16)  NOT NULL,
    updated_at  DATETIME(6)  NOT NULL,
    expires_at  DATETIME(6)  NOT NULL,
    PRIMARY KEY (master_id, context_id, subject_id),
    KEY idx_master_active_flow_expiry (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS eag_master_prompt_version (
    prompt_version_id VARCHAR(32)  NOT NULL PRIMARY KEY,
    master_id         VARCHAR(32)  NOT NULL,
    version_no        INT          NOT NULL,
    prompt_text       MEDIUMTEXT   NOT NULL,
    status            VARCHAR(16)  NOT NULL,
    created_by        VARCHAR(64)  NOT NULL,
    source            VARCHAR(16)  NOT NULL,
    eval_report_json  JSON         NULL,
    created_at        TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_prompt_ver (master_id, version_no),
    INDEX idx_prompt_status (master_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- --------------------------------------------------------------------
-- 权限: 用户/用户组 → 可见服务子集（§6）
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eag_user_group (
    group_id          VARCHAR(32)  NOT NULL PRIMARY KEY,
    name              VARCHAR(64)  NOT NULL UNIQUE,
    description       VARCHAR(256) NULL,
    created_at        TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS eag_group_membership (
    group_id          VARCHAR(32)  NOT NULL,
    cap_user_name     VARCHAR(64)  NOT NULL,
    created_at        TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (group_id, cap_user_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS eag_group_visibility (
    group_id          VARCHAR(32)  NOT NULL,
    service_id        VARCHAR(32)  NOT NULL,
    created_at        TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (group_id, service_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS eag_user_visibility (
    cap_user_name     VARCHAR(64)  NOT NULL,
    service_id        VARCHAR(32)  NOT NULL,
    created_at        TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (cap_user_name, service_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- --------------------------------------------------------------------
-- 能力④ 运行记录 / 评测集 / 用例 / 标注 / 优化 / 候选
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eag_run_record (
    run_id               VARCHAR(32)  NOT NULL PRIMARY KEY,
    trace_id             VARCHAR(64)  NULL,
    root_task_id         VARCHAR(64)  NULL,
    parent_invocation_id VARCHAR(64)  NULL,
    invocation_id        VARCHAR(64)  NULL,
    master_id            VARCHAR(32)  NOT NULL,
    prompt_version_id    VARCHAR(32)  NOT NULL,
    cap_user_name        VARCHAR(64)  NOT NULL,
    context_id           VARCHAR(64)  NOT NULL,
    input_text           MEDIUMTEXT   NULL,
    input_digest         VARCHAR(512) NULL,
    final_output         MEDIUMTEXT   NULL,
    routed_services      JSON         NULL,
    tool_calls           JSON         NULL,
    iterations           INT          NOT NULL DEFAULT 0,
    status               VARCHAR(24)  NOT NULL,
    error_code           VARCHAR(64)  NULL,
    error_detail         VARCHAR(1024) NULL,
    -- 阶段耗时拆解（012）: {totalMs, llmMs, llmCalls, toolMs, otherMs, spans[]}
    timing_json          JSON         NULL,
    started_at           TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    finished_at          TIMESTAMP(6) NULL,
    INDEX idx_run_master (master_id, started_at),
    INDEX idx_run_user (cap_user_name, started_at),
    INDEX idx_run_trace (trace_id, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS eag_eval_dataset (
    dataset_id        VARCHAR(32)  NOT NULL PRIMARY KEY,
    master_id         VARCHAR(32)  NOT NULL,
    name              VARCHAR(128) NOT NULL,
    version_no        INT          NOT NULL,
    status            VARCHAR(16)  NOT NULL,
    created_at        TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_dataset (name, version_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS eag_eval_case (
    case_id           VARCHAR(32)  NOT NULL PRIMARY KEY,
    dataset_id        VARCHAR(32)  NOT NULL,
    input_text        MEDIUMTEXT   NOT NULL,
    expected_service  VARCHAR(64)  NULL,
    source_run_id     VARCHAR(32)  NULL,
    created_at        TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_case_dataset (dataset_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS eag_annotation (
    annotation_id     VARCHAR(32)  NOT NULL PRIMARY KEY,
    case_id           VARCHAR(32)  NOT NULL,
    cap_user_name     VARCHAR(64)  NOT NULL,
    intent_correct    TINYINT(1)   NULL,
    -- 013: 标注可纠正期望服务（人工给出"该是谁"，回写 eag_eval_case）
    corrected_service VARCHAR(128) NULL,
    subjective_note   TEXT         NULL,
    created_at        TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_anno_case (case_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS eag_optimization_run (
    optimization_id          VARCHAR(32)  NOT NULL PRIMARY KEY,
    master_id                VARCHAR(32)  NOT NULL,
    dataset_id               VARCHAR(32)  NOT NULL,
    base_prompt_version_id   VARCHAR(32)  NOT NULL,
    status                   VARCHAR(16)  NOT NULL,
    report_json              JSON         NULL,
    created_by               VARCHAR(64)  NOT NULL,
    created_at               TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    finished_at              TIMESTAMP(6) NULL,
    INDEX idx_opt_master (master_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS eag_prompt_candidate (
    candidate_id      VARCHAR(32)  NOT NULL PRIMARY KEY,
    optimization_id   VARCHAR(32)  NOT NULL,
    prompt_version_id VARCHAR(32)  NULL,
    prompt_text       MEDIUMTEXT   NOT NULL,
    metric_json       JSON         NOT NULL,
    react_config_json JSON         NULL COMMENT '候选的 ReAct 参数覆盖（from_hermes_a2a L3）',
    recommendation    VARCHAR(16)  NOT NULL,
    rationale         TEXT         NULL,
    created_at        TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_cand_opt (optimization_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- --------------------------------------------------------------------
-- P0-2/P0-4/P1-2 治理模型（from_nekiro 02/04/06；与 migrations/002 一致）
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eag_agent_release (
    release_id        VARCHAR(32)  NOT NULL PRIMARY KEY,
    service_id        VARCHAR(32)  NOT NULL,
    agent_version     VARCHAR(64)  NOT NULL,
    card_revision_id  VARCHAR(32)  NOT NULL,
    card_digest       CHAR(64)     NOT NULL,
    endpoint          VARCHAR(512) NOT NULL,
    state             VARCHAR(20)  NOT NULL DEFAULT 'DRAFT',
    created_by        VARCHAR(64)  NOT NULL,
    created_at        TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    published_at      TIMESTAMP(6) NULL,
    suspended_at      TIMESTAMP(6) NULL,
    revoked_at        TIMESTAMP(6) NULL,
    UNIQUE KEY uk_release_version (service_id, agent_version),
    INDEX idx_release_state (service_id, state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS eag_installation (
    installation_id     VARCHAR(32)  NOT NULL PRIMARY KEY,
    workspace_id        VARCHAR(32)  NOT NULL,
    service_id          VARCHAR(32)  NOT NULL,
    release_id          VARCHAR(32)  NOT NULL,
    status              VARCHAR(16)  NOT NULL DEFAULT 'ENABLED',
    accepted_permissions JSON        NULL,
    created_at          TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at          TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    uninstalled_at      TIMESTAMP(6) NULL,
    UNIQUE KEY uk_install (workspace_id, service_id, release_id),
    INDEX idx_install_workspace (workspace_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS eag_invocation_event (
    event_id              VARCHAR(32)  NOT NULL PRIMARY KEY,
    invocation_id         VARCHAR(64)  NOT NULL,
    trace_id              VARCHAR(64)  NOT NULL,
    root_task_id          VARCHAR(64)  NOT NULL,
    parent_invocation_id  VARCHAR(64)  NULL,
    workspace_id          VARCHAR(32)  NULL,
    master_id             VARCHAR(32)  NULL,
    service_id            VARCHAR(32)  NULL,
    release_id            VARCHAR(32)  NULL,
    event_type            VARCHAR(32)  NOT NULL,
    status                VARCHAR(24)  NOT NULL,
    error_code            VARCHAR(64)  NULL,
    started_at            TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    finished_at           TIMESTAMP(6) NULL,
    metadata_json         JSON         NULL,
    INDEX idx_event_trace (trace_id, started_at),
    INDEX idx_event_invocation (invocation_id, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS eag_agent_instance (
    instance_id       VARCHAR(32)  NOT NULL PRIMARY KEY,
    service_id        VARCHAR(32)  NOT NULL,
    instance_name     VARCHAR(128) NOT NULL,
    endpoint          VARCHAR(512) NOT NULL,
    state             VARCHAR(20)  NOT NULL DEFAULT 'UNKNOWN',
    ready             TINYINT(1)   NOT NULL DEFAULT 0,
    serving           TINYINT(1)   NOT NULL DEFAULT 1,
    terminating       TINYINT(1)   NOT NULL DEFAULT 0,
    lease_expires_at  TIMESTAMP(6) NULL,
    last_heartbeat_at TIMESTAMP(6) NULL,
    metadata_json     JSON         NULL,
    created_at        TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at        TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_instance_endpoint (service_id, endpoint),
    INDEX idx_instance_state (service_id, state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- --------------------------------------------------------------------
-- 审计
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eag_audit_event (
    audit_id          VARCHAR(32)  NOT NULL PRIMARY KEY,
    cap_user_name     VARCHAR(64)  NOT NULL,
    action            VARCHAR(64)  NOT NULL,
    resource_type     VARCHAR(32)  NOT NULL,
    resource_id       VARCHAR(64)  NULL,
    detail            JSON         NULL,
    created_at        TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_audit_time (created_at),
    INDEX idx_audit_resource (resource_type, resource_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- --------------------------------------------------------------------
-- 管理面账户体系（admin-auth 01；login-topic 规范，蓝本 qagent_manager）
-- 员工 Pluto OAuth2 登录懒同步 + admin 本地账号兜底 + 双轨会话（sha256 落库）
-- --------------------------------------------------------------------
-- 五张表与 db/schema.sql 同步；全部 CREATE TABLE IF NOT EXISTS 幂等。

CREATE TABLE IF NOT EXISTS eag_console_user (
    user_id             VARCHAR(64)  NOT NULL PRIMARY KEY COMMENT '=badge_id 工号',
    pluto_sub           VARCHAR(128) NOT NULL COMMENT 'Pluto 唯一标识，upsert 定位',
    preferred_username  VARCHAR(128) NULL,
    email               VARCHAR(256) NULL,
    wecom_id            VARCHAR(128) NULL,
    display_name        VARCHAR(128) NULL,
    department          VARCHAR(256) NULL,
    raw_pluto_profile   JSON         NULL COMMENT 'userinfo 原始 JSON，排查字段变更',
    profile_sync_status VARCHAR(24)  NOT NULL DEFAULT 'pending',
    status              VARCHAR(24)  NOT NULL DEFAULT 'active' COMMENT 'disabled 登录 403',
    created_at          TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at          TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_console_user_sub (pluto_sub)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS eag_user_role (
    user_id    VARCHAR(64)  NOT NULL,
    role       VARCHAR(64)  NOT NULL COMMENT 'eagent_user / eagent_admin',
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (user_id, role)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 控制台页面权限（个人/用户组；manage 隐含 view）
CREATE TABLE IF NOT EXISTS eag_page_permission (
    subject_type     VARCHAR(16)  NOT NULL COMMENT 'USER / GROUP',
    subject_id       VARCHAR(64)  NOT NULL,
    page_key         VARCHAR(32)  NOT NULL,
    permission_level VARCHAR(16)  NOT NULL COMMENT 'view / manage',
    created_at       TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at       TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                     ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (subject_type, subject_id, page_key),
    INDEX idx_page_permission_subject (subject_type, subject_id),
    INDEX idx_page_permission_page (page_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS eag_console_session (
    token_hash          CHAR(64)     NOT NULL PRIMARY KEY COMMENT 'sha256(明文 token)',
    user_id             VARCHAR(64)  NOT NULL,
    pluto_sub           VARCHAR(128) NULL,
    pluto_access_token  TEXT         NULL,
    pluto_refresh_token TEXT         NULL,
    expires_at          TIMESTAMP(6) NOT NULL,
    created_at          TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_console_session_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS eag_admin_session (
    token_hash CHAR(64)     NOT NULL PRIMARY KEY COMMENT 'sha256(明文 token)',
    username   VARCHAR(64)  NOT NULL,
    expires_at TIMESTAMP(6) NOT NULL,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS eag_system_config (
    config_key   VARCHAR(64)   NOT NULL PRIMARY KEY,
    config_value VARCHAR(1024) NOT NULL,
    updated_at   TIMESTAMP(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO eag_schema_migration (version_no, name, applied_at)
SELECT 1, 'baseline-v1', CURRENT_TIMESTAMP(6)
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM eag_schema_migration WHERE version_no = 1);

INSERT INTO eag_schema_migration (version_no, name, applied_at)
SELECT 2, 'governance-p0-p1', CURRENT_TIMESTAMP(6)
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM eag_schema_migration WHERE version_no = 2);
