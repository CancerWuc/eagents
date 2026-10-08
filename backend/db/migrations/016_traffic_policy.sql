-- 016｜DEV-10 多版本流量比例切换
-- 同一服务的多个 PUBLISHED Release 按权重分流（校验合计 100%），关闭策略
-- 即回切默认解析链。条目内的 release 在调用期仍走 fail-closed 校验
-- （PUBLISHED + 安装 ENABLED），不可用条目自动让位给其余条目。
CREATE TABLE IF NOT EXISTS eag_traffic_policy (
    policy_id    VARCHAR(32)  NOT NULL PRIMARY KEY,
    service_id   VARCHAR(32)  NOT NULL,
    entries_json JSON         NOT NULL,
    status       VARCHAR(16)  NOT NULL DEFAULT 'ACTIVE',
    created_by   VARCHAR(128) NOT NULL,
    created_at   TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at   TIMESTAMP(6) NULL,
    INDEX idx_traffic_service (service_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
