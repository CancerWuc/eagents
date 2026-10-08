-- 007｜master model config（总控 Agent 模型可配置化，脱钩单一模型）
-- 幂等性: 由 apply_pending_migrations 保证（容忍 1060 重复列）

-- 每个总控 Agent 可独立指定 OpenAI 兼容模型（base_url/model/api_key）；
-- model_config_json 为空 = 回退平台默认（EAGENT_LLM_* 全局配置）。
-- api_key 存于 JSON 内（一期明文，API 返回一律脱敏，见 master-model-config 专题）。
ALTER TABLE eag_master_agent
    ADD COLUMN model_config_json JSON NULL
    COMMENT '总控 Agent 独立模型配置（{provider,base_url,model,api_key}；空=平台默认）'
    AFTER react_config_json;
