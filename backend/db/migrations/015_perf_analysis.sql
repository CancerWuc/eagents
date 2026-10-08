-- 015｜DEV-09 压测/仿真测试 LLM 分析报告
-- build_report 的结构化数字已存 report_json；本迁移补自然语言分析报告的
-- 存储（与测试 1:1，不建独立 eag_perf_report 表——触发即覆盖重生成，
-- 无多版本诉求）。analysis_model 记录生成时模型，便于追溯口径。
ALTER TABLE eag_perf_test
    ADD COLUMN analysis_md MEDIUMTEXT NULL;

ALTER TABLE eag_perf_test
    ADD COLUMN analysis_model VARCHAR(128) NULL;

ALTER TABLE eag_perf_test
    ADD COLUMN analysis_at TIMESTAMP(6) NULL;
