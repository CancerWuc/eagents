-- 021｜DEV-05 接口协议转换扩展：外部工具注册
-- kind = kb_query（外部知识库问答，统一 kb_search 语义+来源标注）|
-- rest（通用 REST 只读调用）| hiagent（预留实现位，待官方接口文档，
-- 注册即拒）。master_id 为空 = 所有总控可用；凭证列 AES-GCM 加密存储。
CREATE TABLE IF NOT EXISTS eag_external_tool (
    tool_id       VARCHAR(32)  NOT NULL PRIMARY KEY,
    master_id     VARCHAR(32)  NULL,
    kind          VARCHAR(16)  NOT NULL,
    name          VARCHAR(64)  NOT NULL,
    description   VARCHAR(500) NOT NULL,
    endpoint      VARCHAR(512) NOT NULL,
    method        VARCHAR(8)   NOT NULL DEFAULT 'POST',
    headers_enc   VARBINARY(2048) NULL,
    body_template TEXT         NULL,
    params_json   JSON         NULL,
    enabled       TINYINT      NOT NULL DEFAULT 1,
    created_by    VARCHAR(128) NOT NULL,
    created_at    TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uq_ext_tool_name (name),
    INDEX idx_ext_tool_master (master_id, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
