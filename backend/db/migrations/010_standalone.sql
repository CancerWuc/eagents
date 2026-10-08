-- 010｜standalone 独立部署导出登记（standalone-deployment 01 §7.1/S-07）
-- 幂等性: 由 apply_pending_migrations 保证（容忍 1050 重复表）

-- 设计给了两条登记路径（eag_installation 扩展列 / release_id+digest 关联）；
-- 代码实证：总控 Agent（eag_master_agent）没有 release 行（release 属
-- eag_a2a_service_registration 子服务），塞 eag_installation 会破坏其
-- （workspace, service, release）语义——故按「digest 关联可查」拍板为
-- 独立登记表：一次 export 一行，三件套 digest 全量落库，
-- master_id + agent_version 与配置状态关联可查（01 §9 S-07 验收）。
CREATE TABLE IF NOT EXISTS eag_standalone_export (
    export_id     VARCHAR(32)  NOT NULL PRIMARY KEY,
    master_id     VARCHAR(32)  NOT NULL,
    agent_version VARCHAR(64)  NOT NULL,
    image_tag     VARCHAR(128) NOT NULL,
    card_digest   CHAR(64)     NOT NULL,
    snapshot_hash CHAR(64)     NOT NULL,
    deploy_digest CHAR(64)     NOT NULL,
    state         VARCHAR(16)  NOT NULL DEFAULT 'EXPORTED',
    created_by    VARCHAR(64)  NOT NULL,
    created_at    TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_standalone_master (master_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
