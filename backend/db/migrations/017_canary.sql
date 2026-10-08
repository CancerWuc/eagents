-- 017｜DEV-11 灰度测试（依赖 016 流量策略的解析注入点）
-- 一个服务至多一个 RUNNING 灰度：白名单用户优先走候选版，其余按稳定哈希
-- 比例分流（同一用户恒定同组，不漂移）；候选/基线均须 PUBLISHED。
-- 指标从 eag_invocation_event 账本按 release_id 聚合（真实调用数据）。
CREATE TABLE IF NOT EXISTS eag_canary_run (
    canary_id            VARCHAR(32)  NOT NULL PRIMARY KEY,
    service_id           VARCHAR(32)  NOT NULL,
    candidate_release_id VARCHAR(32)  NOT NULL,
    baseline_release_id  VARCHAR(32)  NOT NULL,
    status               VARCHAR(16)  NOT NULL DEFAULT 'RUNNING',
    group_json           JSON         NOT NULL,
    thresholds_json      JSON         NOT NULL,
    created_by           VARCHAR(128) NOT NULL,
    created_at           TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    decided_at           TIMESTAMP(6) NULL,
    decision_note        VARCHAR(512) NULL,
    INDEX idx_canary_service (service_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
