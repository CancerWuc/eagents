-- 012｜运行记录的阶段耗时拆解（LLM 每轮 / 工具每次 / 其余开销）
-- 账本原先只有出站 elapsed_ms 与 run 总时长，模型推理时间没有埋点。
ALTER TABLE eag_run_record
    ADD COLUMN timing_json JSON NULL AFTER error_detail;
