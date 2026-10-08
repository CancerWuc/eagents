-- 019｜DEV-01 分层记忆：会话持久化 + 统一记忆条目
-- 会话: AgentState 序列化落库（LRU 仅作一级缓存），重启 / 多副本多轮
-- 上下文不丢（wiki「多实例会话状态持久化」跳过项的核心落地）。
-- 记忆条目: scope = user（跨会话偏好）/ agent（总控沉淀，如优化结论）/
-- shared（多 Agent 共享区，绑定来源 trace），TTL 过期惰性过滤。
CREATE TABLE IF NOT EXISTS eag_agent_session (
    session_id    VARCHAR(64)  NOT NULL PRIMARY KEY,
    master_id     VARCHAR(32)  NOT NULL,
    cap_user_name VARCHAR(64)  NULL,
    state_json    MEDIUMTEXT   NOT NULL,
    updated_at    TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    INDEX idx_session_master (master_id, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS eag_memory_item (
    memory_id        VARCHAR(32)  NOT NULL PRIMARY KEY,
    scope            VARCHAR(16)  NOT NULL,
    owner_id         VARCHAR(64)  NOT NULL,
    content          VARCHAR(1024) NOT NULL,
    source_trace_id  VARCHAR(64)  NULL,
    expires_at       TIMESTAMP(6) NULL,
    created_at       TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_memory_owner (scope, owner_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
