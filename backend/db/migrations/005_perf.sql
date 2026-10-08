-- 005｜性能测试（perf-test 专题 01 §4，T01）
-- 幂等：全部 CREATE TABLE IF NOT EXISTS；重复索引错误由迁移引擎容忍（1061）
-- 依赖：001 baseline（db/schema.sql）
-- DDL 权威来源：eagent-wiki/perf-test/01-design.md §4
-- 实现级补充（相对 01 §4.3）：eag_perf_test 增加 cancel_flag（02 §3.2 T12
-- 的取消检查点，对齐 eag_kb_task.cancel_flag 写法）

-- 1. 数据集（挂在总控 Agent 下）---------------------------------
CREATE TABLE IF NOT EXISTS eag_perf_dataset (
    perf_dataset_id VARCHAR(32)   NOT NULL PRIMARY KEY,
    master_id       VARCHAR(32)   NOT NULL,
    name            VARCHAR(128)  NOT NULL,
    dataset_type    VARCHAR(16)   NOT NULL COMMENT 'PRESSURE / SIMULATION',
    status          VARCHAR(16)   NOT NULL DEFAULT 'ACTIVE',
    case_count      INT           NOT NULL DEFAULT 0,
    created_by      VARCHAR(64)   NOT NULL,
    created_at      TIMESTAMP(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at      TIMESTAMP(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_perf_dataset (master_id, name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 2. 用例（q/期望答案 a；SIMULATION 另有每日时刻）----------------
CREATE TABLE IF NOT EXISTS eag_perf_case (
    case_id         VARCHAR(32)   NOT NULL PRIMARY KEY,
    perf_dataset_id VARCHAR(32)   NOT NULL,
    seq_no          INT           NOT NULL,
    q               MEDIUMTEXT    NOT NULL,
    a               MEDIUMTEXT    NULL,
    send_time       VARCHAR(8)    NULL COMMENT 'HH:mm:ss（SIMULATION 必填）',
    time_sec        INT           NULL COMMENT '当日秒数 = HH*3600+MM*60+SS',
    created_at      TIMESTAMP(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_perf_case_dataset (perf_dataset_id, seq_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 3. 测试任务（全局同一时间仅一个 PENDING/RUNNING）----------------
CREATE TABLE IF NOT EXISTS eag_perf_test (
    perf_test_id    VARCHAR(32)   NOT NULL PRIMARY KEY,
    master_id       VARCHAR(32)   NOT NULL,
    perf_dataset_id VARCHAR(32)   NOT NULL,
    target_type     VARCHAR(16)   NOT NULL COMMENT 'MASTER / SERVICE',
    target_id       VARCHAR(32)   NOT NULL,
    test_type       VARCHAR(16)   NOT NULL COMMENT 'PRESSURE / SIMULATION',
    status          VARCHAR(16)   NOT NULL
                    COMMENT 'PENDING/RUNNING/COMPLETED/FAILED/CANCELED',
    config_json     JSON          NOT NULL,
    report_json     JSON          NULL,
    cancel_flag     TINYINT(1)    NOT NULL DEFAULT 0
                    COMMENT '管理面取消请求（T12 worker 检查点）',
    created_by      VARCHAR(64)   NOT NULL,
    created_at      TIMESTAMP(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    started_at      TIMESTAMP(6)  NULL,
    finished_at     TIMESTAMP(6)  NULL,
    INDEX idx_perf_test_status (status, created_at),
    INDEX idx_perf_test_master (master_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 4. 样本（每请求一行，含状态/延迟/血缘，供进度与报告）------------
CREATE TABLE IF NOT EXISTS eag_perf_sample (
    sample_id       VARCHAR(32)   NOT NULL PRIMARY KEY,
    perf_test_id    VARCHAR(32)   NOT NULL,
    seq_no          INT           NOT NULL,
    q               MEDIUMTEXT    NOT NULL,
    expected_a      MEDIUMTEXT    NULL,
    actual_output   MEDIUMTEXT    NULL,
    status          VARCHAR(16)   NOT NULL
                    COMMENT 'SUCCEEDED/TIMEOUT/FAILED/REJECTED',
    latency_ms      INT           NULL,
    error_code      VARCHAR(64)   NULL
                    COMMENT 'SUCCEEDED 的 REQUIRES_INPUT 亦用此列单独标注',
    error_detail    VARCHAR(1024) NULL,
    sent_at         TIMESTAMP(6)  NULL,
    finished_at     TIMESTAMP(6)  NULL,
    trace_id        VARCHAR(64)   NULL,
    run_id          VARCHAR(32)   NULL,
    context_id      VARCHAR(64)   NULL,
    answer_match    TINYINT       NULL,
    created_at      TIMESTAMP(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_perf_sample_test (perf_test_id, seq_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 版本登记（幂等；与 004 的登记写法一致）
INSERT INTO eag_schema_migration (version_no, name, applied_at)
SELECT 5, 'perf', CURRENT_TIMESTAMP(6)
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM eag_schema_migration WHERE version_no = 5);
