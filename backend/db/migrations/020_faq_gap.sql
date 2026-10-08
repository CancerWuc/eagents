-- 020｜DEV-17 人效提升工具：FAQ 候选 + 知识缺口
-- FAQ: 从真实运行记录挖高频问答对（Q=用户原话 A=总控最终回复，逐字
-- 保留不重写，杜绝生成幻觉），审批通过后自动作为新知识源进入 KB 入库
-- 规划（复用 upload_source → planner 链路）。
-- 缺口: kb_search 拒答（no_reliable_hit）聚类成清单，供知识补齐排期。
CREATE TABLE IF NOT EXISTS eag_faq_candidate (
    faq_id       VARCHAR(32)  NOT NULL PRIMARY KEY,
    master_id    VARCHAR(32)  NULL,
    question     VARCHAR(1024) NOT NULL,
    answer       MEDIUMTEXT   NOT NULL,
    freq         INT          NOT NULL DEFAULT 1,
    status       VARCHAR(16)  NOT NULL DEFAULT 'PENDING',
    source_run_id VARCHAR(64) NULL,
    created_by   VARCHAR(128) NOT NULL DEFAULT 'system',
    created_at   TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    decided_at   TIMESTAMP(6) NULL,
    ingested_source_id VARCHAR(32) NULL,
    INDEX idx_faq_status (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS eag_knowledge_gap (
    gap_id       VARCHAR(32)  NOT NULL PRIMARY KEY,
    master_id    VARCHAR(32)  NULL,
    query_norm   VARCHAR(512) NOT NULL,
    query_text   VARCHAR(1024) NOT NULL,
    hit_count    INT          NOT NULL DEFAULT 1,
    last_seen_at TIMESTAMP(6) NOT NULL,
    status       VARCHAR(16)  NOT NULL DEFAULT 'OPEN',
    created_at   TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uq_gap_norm (master_id, query_norm(190))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
