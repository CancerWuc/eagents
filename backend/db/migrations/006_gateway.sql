-- 006｜Higress 网关配置控制面（higress-mcp-gateway 专题 01 §5.1，T01）
-- 幂等：全部 CREATE TABLE IF NOT EXISTS；重复索引错误由迁移引擎容忍（1061）
-- 依赖：001 baseline（db/schema.sql）
-- DDL 权威来源：eagent-wiki/higress-mcp-gateway/01-eagent-higress-integration.md §5.1
-- 实现级补充（相对 01 §5.1，来源均在专题文档内）：
--   1) eag_gateway_profile.higress_version —— 01 §8 风险表：公司内部环境版本
--      可能非 2.2.3，适配层按版本分支；
--   2) eag_gateway_mcp_service.provider_config —— 02 基准 §5：四步写序列第 2 步
--      ai-proxy provider（type/protocol/apiUrl/apiTokens）的载体；
--   3) eag_gateway_consumer.prev_credential / prev_expires_at —— T13 轮换宽限期
--      （旧 key 在宽限期内仍写入 key-auth，到期由后台清扫下线）。

-- 1. 网关环境 profile（形态 A 跨 ns / 形态 B 同 ns 共用）----------------
CREATE TABLE IF NOT EXISTS eag_gateway_profile (
    id           BIGINT        NOT NULL AUTO_INCREMENT PRIMARY KEY,
    name         VARCHAR(64)   NOT NULL COMMENT '环境名：aliyun-dev / corp-internal',
    higress_ns   VARCHAR(64)   NOT NULL COMMENT 'Higress 资源所在 namespace',
    kube_context VARCHAR(128) NOT NULL COMMENT 'kubeconfig context 名（空=默认）',
    higress_version VARCHAR(16) NULL COMMENT 'Higress 版本（空=未探测）',
    enabled      TINYINT       NOT NULL DEFAULT 1,
    created_at   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP
                               ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_gw_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2. MCP 服务（对应 McpBridge.spec.registries 条目）---------------------
CREATE TABLE IF NOT EXISTS eag_gateway_mcp_service (
    id            BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    profile_id    BIGINT       NOT NULL,
    name          VARCHAR(128) NOT NULL COMMENT '展示名；registry 名= llm-llm-<slug>.internal',
    domain        VARCHAR(255) NOT NULL COMMENT '上游域名，如 api.deepseek.com',
    port          INT          NOT NULL DEFAULT 443,
    protocol      VARCHAR(16)  NOT NULL DEFAULT 'https',
    registry_type VARCHAR(16)  NOT NULL DEFAULT 'dns' COMMENT 'dns / static / failover',
    provider_config JSON       NULL COMMENT 'ai-proxy provider：{type,protocol,apiUrl,apiTokens}',
    status        VARCHAR(16)  NOT NULL DEFAULT 'active',
    created_by    VARCHAR(64)  NOT NULL,
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                               ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_mcp_name (profile_id, name),
    KEY idx_mcp_profile (profile_id),
    CONSTRAINT fk_mcp_profile FOREIGN KEY (profile_id)
        REFERENCES eag_gateway_profile (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3. 路由（对应 ai-route-route-<slug> ConfigMap）--------------------------
CREATE TABLE IF NOT EXISTS eag_gateway_route (
    id            BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    profile_id    BIGINT       NOT NULL,
    name          VARCHAR(128) NOT NULL,
    match_type    VARCHAR(16)  NOT NULL DEFAULT 'PRE' COMMENT 'PRE / EXACT / REGEX',
    match_value   VARCHAR(512) NOT NULL,
    upstream_mcp  BIGINT       NOT NULL COMMENT '→ eag_gateway_mcp_service.id',
    weight        INT          NOT NULL DEFAULT 100,
    auth_enabled  TINYINT      NOT NULL DEFAULT 1,
    auth_type     VARCHAR(32)  NOT NULL DEFAULT 'key-auth',
    retry_enabled TINYINT      NOT NULL DEFAULT 1,
    retry_attempts INT         NOT NULL DEFAULT 3,
    status        VARCHAR(16)  NOT NULL DEFAULT 'active',
    created_by    VARCHAR(64)  NOT NULL,
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                               ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_route_name (profile_id, name),
    KEY idx_route_profile (profile_id),
    CONSTRAINT fk_route_profile FOREIGN KEY (profile_id)
        REFERENCES eag_gateway_profile (id),
    CONSTRAINT fk_route_upstream FOREIGN KEY (upstream_mcp)
        REFERENCES eag_gateway_mcp_service (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 4. consumer 密钥（AES-GCM 加密存储，写入 Higress 时明文展开）-------------
CREATE TABLE IF NOT EXISTS eag_gateway_consumer (
    id             BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    profile_id     BIGINT       NOT NULL,
    name           VARCHAR(128) NOT NULL COMMENT 'consumer-<slug>',
    credential     VARBINARY(512) NOT NULL COMMENT 'AES-GCM 密文（nonce+ct+tag）',
    prev_credential VARBINARY(512) NULL COMMENT '轮换宽限期内的旧密钥密文',
    prev_expires_at DATETIME    NULL COMMENT '旧密钥下线时刻',
    route_ids      JSON         NULL COMMENT '绑定的路由 id 列表',
    status         VARCHAR(16)  NOT NULL DEFAULT 'active',
    created_by     VARCHAR(64)  NOT NULL,
    created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_consumer_name (profile_id, name),
    KEY idx_consumer_profile (profile_id),
    CONSTRAINT fk_consumer_profile FOREIGN KEY (profile_id)
        REFERENCES eag_gateway_profile (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 5. 变更审计（所有写操作含失败均落库）------------------------------------
CREATE TABLE IF NOT EXISTS eag_gateway_audit (
    id             BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    profile_id     BIGINT       NOT NULL,
    resource_type  VARCHAR(32)  NOT NULL COMMENT 'profile/mcpbridge/wasmplugin/route/consumer/sync',
    action         VARCHAR(16)  NOT NULL COMMENT 'create / update / delete',
    payload_before JSON         NULL,
    payload_after  JSON         NOT NULL,
    result         VARCHAR(16)  NOT NULL COMMENT 'success / failed',
    operator       VARCHAR(64)  NOT NULL,
    created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_audit_profile (profile_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 版本登记（幂等；与 005 的登记写法一致）
INSERT INTO eag_schema_migration (version_no, name, applied_at)
SELECT 6, 'gateway', CURRENT_TIMESTAMP(6)
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM eag_schema_migration WHERE version_no = 6);
