-- 003｜from_hermes_a2a 智能化优化（向后兼容，幂等）
-- 幂等性: 由 apply_pending_migrations 保证（容忍 1060 重复列）
-- 依赖: 001 baseline（db/schema.sql）、002 governance

-- L3: 候选携带 ReAct 参数覆盖（提示词 + max_iters 元组）
ALTER TABLE eag_prompt_candidate
    ADD COLUMN react_config_json JSON NULL
    COMMENT '候选的 ReAct 参数覆盖（如 {"max_iters": 8}）' AFTER metric_json;
