-- 002｜P0-2/P0-3/P0-4/P1-2 治理模型（from_nekiro 02/04/06 文档）
-- 幂等性: 由 scripts/apply_migrations.py 保证（容忍重复列/表/索引错误）
-- 依赖: 001 baseline（db/schema.sql）

-- P0-2: Agent Release，一次不可变发布（from_nekiro/02 §4）
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

-- P0-2: Installation，工作空间显式安装
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

-- P0-4: append-only 调用事件（不存输入输出与密钥）
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

-- P1-2: 实例注册表（注册/心跳/租约）
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

-- P0-2: 绑定升级为可 pin 到精确 Release（NULL = 走工作空间安装解析）
ALTER TABLE eag_master_binding
    ADD COLUMN release_id VARCHAR(32) NULL AFTER service_id;

-- P0-4: 运行记录血缘字段 + 错误码（07 文档）
ALTER TABLE eag_run_record
    ADD COLUMN trace_id VARCHAR(64) NULL AFTER run_id;

ALTER TABLE eag_run_record
    ADD COLUMN root_task_id VARCHAR(64) NULL AFTER trace_id;

ALTER TABLE eag_run_record
    ADD COLUMN parent_invocation_id VARCHAR(64) NULL AFTER root_task_id;

ALTER TABLE eag_run_record
    ADD COLUMN invocation_id VARCHAR(64) NULL AFTER parent_invocation_id;

ALTER TABLE eag_run_record
    ADD COLUMN error_code VARCHAR(64) NULL AFTER status;

ALTER TABLE eag_run_record
    ADD INDEX idx_run_trace (trace_id, started_at);
