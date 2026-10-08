-- 026｜管理面账户体系（admin-auth 01；login-topic 规范，蓝本 qagent_manager）
-- 五张表与 db/schema.sql 同步；全部 CREATE TABLE IF NOT EXISTS 幂等。
-- 版本号取 026：并入 master 时 025 已被 025_router_credentials 占用，故由原 025 顺延
-- （迁移器按文件名前缀取版本，同号会被 INSERT IGNORE 静默跳过；见 wiki from_hiagent/）。

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
