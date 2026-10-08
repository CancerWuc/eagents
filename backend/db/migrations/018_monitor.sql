-- 018｜DEV-12 日志监控增强：告警事件 + 远程日志纳管
-- 告警：定时探测连续 N 次不可达触发 SERVICE_DOWN（静默窗口内不重复），
-- 恢复触发 SERVICE_RECOVERED；webhook 通道可选。
-- 远程日志：远程运行服务经 HTTP（或未来 MQ 消费者调用同一 ingest 函数）
-- 推送日志事件，统一进平台观测口径、可按 trace/服务检索。
CREATE TABLE IF NOT EXISTS eag_alert_event (
    alert_id         VARCHAR(32)  NOT NULL PRIMARY KEY,
    service_id       VARCHAR(32)  NOT NULL,
    kind             VARCHAR(32)  NOT NULL,
    status           VARCHAR(16)  NOT NULL DEFAULT 'FIRED',
    detail           VARCHAR(1024) NULL,
    notified         TINYINT      NOT NULL DEFAULT 0,
    created_at       TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    resolved_at      TIMESTAMP(6) NULL,
    INDEX idx_alert_service (service_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS eag_remote_log_event (
    log_id       VARCHAR(32)  NOT NULL PRIMARY KEY,
    service_id   VARCHAR(32)  NULL,
    source       VARCHAR(16)  NOT NULL DEFAULT 'http',
    level        VARCHAR(16)  NOT NULL DEFAULT 'INFO',
    trace_id     VARCHAR(64)  NULL,
    run_id       VARCHAR(64)  NULL,
    message      VARCHAR(2048) NOT NULL,
    occurred_at  TIMESTAMP(6) NOT NULL,
    received_at  TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_rlog_trace (trace_id, occurred_at),
    INDEX idx_rlog_service (service_id, occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
