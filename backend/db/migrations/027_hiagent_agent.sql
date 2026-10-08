-- 027｜HiAgent 接入（hiagent-integration/01）：统一 Agent Catalog 增 HIAGENT 来源
-- 版本号由原 026 顺延为 027（并入 master 时 025→026 连锁顺延，见文件 026_admin_auth.sql 说明）。
-- ① 注册目录增来源列：A2A_REMOTE（存量行默认归位）/ HIAGENT；
-- ② HIA 专属配置 side 表：base_url 为 NULL = 跟随全局默认
--    （HIAGENT_URL → HIAGNET_URL → qagent 同款默认地址），非空 = 实例级覆盖；
--    app_key AES-GCM 加密落库（复用 gateway/crypto），永不回显。
-- ③ A2A contextId ↔ hiagent AppConversationID 映射（多副本安全，替代 qagent
--    的 Redis 方案）；TTL 2 天惰性清理（对齐 qagent 渠道口径）。

ALTER TABLE eag_a2a_service_registration
  ADD COLUMN source_type VARCHAR(20) NOT NULL DEFAULT 'A2A_REMOTE';

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
    updated_at       TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (context_id, service_id),
    INDEX idx_hia_conv_ttl (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
