-- 022｜时区漂移修复：与"python 本地时间"比较的 TIMESTAMP 列统一改 DATETIME(6)
-- 根因（2026-08-25 实测缺陷定位）：TIMESTAMP 会按连接时区做 UTC 转换，
-- 而 run_record.started_at（DATETIME）/ persistence.now()（宿主本地时间）
-- 是"字面直存"语义。两口径混用时差 8 小时，引发三个真实缺陷：
--   1) 知识缺口水位线：last_seen_at(UTC) vs run.started_at(本地) → 恒有
--      "新增" → 已解决缺口被误重开；
--   2) 记忆 TTL：expires_at 判"已过期"的窗口偏移 8h；
--   3) 告警静默窗：created_at 与 now() 差恒 >8h → 复告警被永久静默。
-- eag_canary_run.created_at 不改：其比较对端 invocation_event.started_at
-- 同为 TIMESTAMP（两端对称，读回一致，实测灰度指标正确）。
ALTER TABLE eag_knowledge_gap
    MODIFY last_seen_at DATETIME(6) NOT NULL;
ALTER TABLE eag_memory_item
    MODIFY expires_at DATETIME(6) NULL;
ALTER TABLE eag_alert_event
    MODIFY created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6);
ALTER TABLE eag_alert_event
    MODIFY resolved_at DATETIME(6) NULL;
