-- 004｜KB 知识库（from_llm_wiki 01/02/03/04/05，T01）
-- 幂等：全部 CREATE TABLE IF NOT EXISTS；重复索引错误由迁移引擎容忍（1061）
-- 依赖：001 baseline（db/schema.sql）/ 002 governance / 003 intelligence
-- DDL 权威来源：eagent-wiki/from_llm_wiki/02-data-model-api.md §3

-- 1. 知识库（治理资源）---------------------------------------
CREATE TABLE IF NOT EXISTS eag_knowledge_base (
    kb_id                VARCHAR(32)   NOT NULL PRIMARY KEY,
    slug                 VARCHAR(64)   NOT NULL UNIQUE
                         COMMENT '工具名域：kb_search_<slug>',
    display_name         VARCHAR(128)  NOT NULL,
    description          VARCHAR(1024) NULL,
    classification       VARCHAR(16)   NOT NULL DEFAULT 'INTERNAL'
                         COMMENT '密级：PUBLIC/INTERNAL/CONFIDENTIAL（S7，一期仅 PUBLIC/INTERNAL 两级实际使用）',
    owner_app            VARCHAR(64)   NULL COMMENT '创建方客服应用标识，可空',
    status               VARCHAR(20)   NOT NULL DEFAULT 'CREATED'
                         COMMENT 'CREATED/PARSING/READY/STALE/FAILED/ARCHIVED',
    current_rev          VARCHAR(64)   NULL COMMENT '当前检索口径 Rev（KB FAILED 时保持不变，D9）',
    schema_text          MEDIUMTEXT    NULL COMMENT 'schema.md（目录路由权威规则）',
    purpose_text         MEDIUMTEXT    NULL COMMENT 'purpose.md（知识库目标）',
    embedding_config     JSON          NULL COMMENT 'provider/model/dimension/endpoint（见 02 §4.1）',
    chunking_config      JSON          NULL COMMENT 'max_tokens/overlap/title_hierarchy（参考 RAGFlow）',
    search_config        JSON          NULL COMMENT '检索治理配置（D3）：top_k 窗口/聚合字节预算/相关度地板',
    created_at           TIMESTAMP(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at           TIMESTAMP(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_kb_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 2. 原始源（不可变输入，SHA256 指纹驱动增量）-----------------
CREATE TABLE IF NOT EXISTS eag_kb_source (
    source_id           VARCHAR(32)   NOT NULL PRIMARY KEY,
    kb_id               VARCHAR(32)   NOT NULL,
    rel_path            VARCHAR(512)  NOT NULL COMMENT 'raw/sources/...',
    file_name           VARCHAR(256)  NOT NULL,
    mime_type           VARCHAR(64)   NULL,
    sha256              CHAR(64)      NOT NULL COMMENT '增量缓存判断 + 重复内容检测（D7）',
    status              VARCHAR(20)   NOT NULL DEFAULT 'PENDING'
                        COMMENT 'PENDING/PLANNING/INGESTING/DONE/FAILED/DELETED',
    last_rev            VARCHAR(64)   NULL COMMENT '最后成功编译进哪个 Rev',
    error_code          VARCHAR(64)   NULL,
    error_detail        VARCHAR(1024) NULL,
    created_at          TIMESTAMP(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at          TIMESTAMP(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_kb_source (kb_id, rel_path),
    INDEX idx_kb_source_sha (kb_id, sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 3. Wiki 页面（内容寻址，B5）---------------------------------
-- 一行 = 一个内容版本；同一 (kb_id, rel_path, content_hash) 跨 Rev 只存一行。
-- 「当前版本」= 按 (kb_id, rel_path) 取 updated_at 最新行。
CREATE TABLE IF NOT EXISTS eag_kb_page (
    page_id             VARCHAR(32)   NOT NULL PRIMARY KEY,
    kb_id               VARCHAR(32)   NOT NULL,
    rel_path            VARCHAR(512)  NOT NULL COMMENT 'wiki/...（命名空间限定，S6）',
    title               VARCHAR(256)  NOT NULL,
    content_hash        CHAR(64)      NOT NULL COMMENT 'sha256(frontmatter_json + body_md)；跨 Rev 夆用判断',
    frontmatter_json    JSON          NULL COMMENT 'sources[]/tags/type 等',
    body_md             MEDIUMTEXT    NOT NULL,
    source_hashes_json  JSON          NULL COMMENT 'sources[] 反查（源 SHA256 集合）',
    embedded            TINYINT(1)    NOT NULL DEFAULT 0,
    chunk_count         INT           NOT NULL DEFAULT 0,
    error_code          VARCHAR(64)   NULL COMMENT '单页生成/重嵌失败码（S2）',
    error_detail        VARCHAR(1024) NULL,
    created_at          TIMESTAMP(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at          TIMESTAMP(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_kb_page_ver (kb_id, rel_path, content_hash),
    INDEX idx_kb_page (kb_id, rel_path, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 4. chunk（键不含 rev：页面内容未变则行不变，跨 Rev 自然复用）-
CREATE TABLE IF NOT EXISTS eag_kb_chunk (
    chunk_id            VARCHAR(32)   NOT NULL PRIMARY KEY,
    kb_id               VARCHAR(32)   NOT NULL,
    page_id             VARCHAR(32)   NOT NULL,
    page_content_hash   CHAR(64)      NOT NULL COMMENT '所属页面内容指纹（冗余，供校验）',
    chunk_index         INT           NOT NULL,
    text_md             TEXT          NOT NULL,
    token_count         INT           NOT NULL DEFAULT 0,
    vector_id           VARCHAR(64)   NULL COMMENT '向量库内向量 id（B3）；LanceDB 行主键',
    content_hash        CHAR(64)      NOT NULL COMMENT 'chunk 文本指纹（B5）',
    created_at          TIMESTAMP(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_kb_chunk (kb_id, page_id, chunk_index),
    INDEX idx_kb_chunk_page (page_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 5. embedding 缓存（B5/S8）-----------------------------------
-- 命中 (content_hash, config_fingerprint) 即跳过重嵌，跨 Rev 零成本复用
CREATE TABLE IF NOT EXISTS eag_kb_embed_cache (
    cache_id            VARCHAR(32)   NOT NULL PRIMARY KEY,
    kb_id               VARCHAR(32)   NOT NULL,
    content_hash        CHAR(64)      NOT NULL,
    config_fingerprint  CHAR(64)      NOT NULL COMMENT 'sha256(embedding_config 规范化 JSON)',
    status              VARCHAR(16)   NOT NULL DEFAULT 'DONE' COMMENT 'PENDING/DONE/FAILED',
    vectors_written     INT           NOT NULL DEFAULT 0,
    updated_at          TIMESTAMP(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_kb_embed_cache (content_hash, config_fingerprint)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 6. Rev（不可变版本头）---------------------------------------
CREATE TABLE IF NOT EXISTS eag_kb_rev (
    rev                 VARCHAR(64)   NOT NULL,
    kb_id               VARCHAR(32)   NOT NULL,
    status              VARCHAR(20)   NOT NULL DEFAULT 'BUILDING'
                        COMMENT 'BUILDING/READY/FAILED（FAILED 不影响 current_rev，D9）',
    summary_json        JSON          NULL COMMENT '新增/更新/删除页面与源统计',
    report_json         JSON          NULL COMMENT 'lint/review 汇总',
    created_by          VARCHAR(64)   NOT NULL,
    created_at          TIMESTAMP(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    finished_at         TIMESTAMP(6) NULL,
    PRIMARY KEY (kb_id, rev),
    INDEX idx_kb_rev_status (kb_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 6b. Rev manifest（B5）：rev → 页面内容 hash 集合 -------------
CREATE TABLE IF NOT EXISTS eag_kb_rev_entry (
    kb_id               VARCHAR(32)   NOT NULL,
    rev                 VARCHAR(64)   NOT NULL,
    page_id             VARCHAR(32)   NOT NULL,
    content_hash        CHAR(64)      NOT NULL,
    chunk_ids_json      JSON          NULL COMMENT '本 Rev 该页引用的 chunk_id 列表',
    PRIMARY KEY (kb_id, rev, page_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 7. wikilink 邻接表（D-2026-08-16-01 基底替换 #2）-------------
-- 编译期提取落库；查询期一跳扩展只查此表，不扫文件
-- 实现级修正（相对 02 §3 表 7）: utf8mb4 下 (kb_id+src_path+dst_path)
-- 全长唯一键 = 4224B 超 InnoDB 3072B 上限 → 前缀唯一键（300+300 字符）
CREATE TABLE IF NOT EXISTS eag_kb_graph_link (
    link_id             VARCHAR(32)   NOT NULL PRIMARY KEY,
    kb_id               VARCHAR(32)   NOT NULL,
    src_path            VARCHAR(512)  NOT NULL COMMENT 'wiki/... 源页路径',
    dst_path            VARCHAR(512)  NOT NULL COMMENT 'wiki/... 目标页路径',
    link_type           VARCHAR(16)   NOT NULL DEFAULT 'WIKILINK' COMMENT 'WIKILINK（一期）',
    created_at          TIMESTAMP(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_kb_graph (kb_id, src_path(300), dst_path(300))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 8. Review（B2，对齐 llm-wiki 四类型）-------------------------
CREATE TABLE IF NOT EXISTS eag_kb_review (
    review_id           VARCHAR(32)   NOT NULL PRIMARY KEY,
    kb_id               VARCHAR(32)   NOT NULL,
    rev                 VARCHAR(64)   NULL COMMENT '产生该 Review 的 Rev',
    page_id             VARCHAR(32)   NULL,
    type                VARCHAR(20)   NOT NULL
                        COMMENT 'contradiction/duplicate/missing_page/suggestion',
    payload_json        JSON          NULL COMMENT '证据/位置/建议',
    status              VARCHAR(16)   NOT NULL DEFAULT 'OPEN'
                        COMMENT 'OPEN/RESOLVED/DISMISSED',
    resolved_by         VARCHAR(64)   NULL,
    resolved_at         TIMESTAMP(6)  NULL,
    created_at          TIMESTAMP(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_kb_review_kb (kb_id, status),
    INDEX idx_kb_review_page (page_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 9. 客服应用 ↔ KB 绑定（S1：主键 binding_id）------------------
CREATE TABLE IF NOT EXISTS eag_kb_binding (
    binding_id          VARCHAR(32)   NOT NULL PRIMARY KEY,
    target_type         VARCHAR(16)   NOT NULL DEFAULT 'MASTER'
                        COMMENT 'MASTER（总控）/AGENT（普通 A2A 应用）',
    target_id           VARCHAR(32)   NOT NULL COMMENT 'master_id 或 service_id',
    kb_id               VARCHAR(32)   NOT NULL,
    tool_prefix         VARCHAR(64)   NULL COMMENT '空=自动生成 kb_search_<slug>',
    sort_order          INT           NOT NULL DEFAULT 0,
    enabled             TINYINT(1)    NOT NULL DEFAULT 1,
    created_at          TIMESTAMP(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_kb_binding (target_type, target_id, kb_id),
    INDEX idx_kb_binding_target (target_type, target_id, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 10. 入库规划（DRAFT 专用）------------------------------------
-- B1：JSON 列不建索引；S5：plan_type 一期仅 UPLOAD_PLAN；
-- D9：APPLIED_WITH_SKIPS（部分动作 SKIP）
CREATE TABLE IF NOT EXISTS eag_kb_ingest_plan (
    plan_id             VARCHAR(32)   NOT NULL PRIMARY KEY,
    kb_id               VARCHAR(32)   NOT NULL,
    source_ids_json     JSON          NULL COMMENT '本次规划针对的新上传 source_id 列表',
    status              VARCHAR(16)   NOT NULL DEFAULT 'DRAFT'
                        COMMENT 'DRAFT/CONFIRMED/REJECTED/SUPERSEDED/APPLIED/APPLIED_WITH_SKIPS',
    plan_type           VARCHAR(32)   NOT NULL DEFAULT 'UPLOAD_PLAN'
                        COMMENT '一期仅 UPLOAD_PLAN；RESTRUCTURE/LINT_FIX 二期（S5）',
    base_rev            VARCHAR(64)   NULL,
    proposal_json       JSON          NOT NULL COMMENT '结构化入库动作（含 depends_on）',
    preview_json        JSON          NULL COMMENT '页面级 diff 预览（供 Tab 展示）',
    rationale_md        MEDIUMTEXT    NOT NULL COMMENT '为什么这样放/拆/改',
    risk_md             MEDIUMTEXT    NULL,
    created_by          VARCHAR(64)   NOT NULL,
    confirm_by          VARCHAR(64)   NULL COMMENT '人工确认人',
    confirm_note        TEXT          NULL COMMENT '驳回必填原因（S3，注入后续规划 prompt）',
    applied_rev         VARCHAR(64)   NULL,
    created_at          TIMESTAMP(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    confirmed_at        TIMESTAMP(6)  NULL,
    applied_at          TIMESTAMP(6)  NULL,
    INDEX idx_kb_plan_kb (kb_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 11. 编译任务（B4）--------------------------------------------
CREATE TABLE IF NOT EXISTS eag_kb_task (
    task_id             VARCHAR(32)   NOT NULL PRIMARY KEY,
    kb_id               VARCHAR(32)   NOT NULL,
    rev                 VARCHAR(64)   NULL COMMENT '本次任务产出/目标 Rev',
    kind                VARCHAR(16)   NOT NULL COMMENT 'INGEST/REBUILD/EMBED',
    status              VARCHAR(16)   NOT NULL DEFAULT 'PENDING'
                        COMMENT 'PENDING/RUNNING/DONE/FAILED/CANCELED',
    retry_count         INT           NOT NULL DEFAULT 0 COMMENT '≤3',
    progress_msg        VARCHAR(1024) NULL COMMENT '前端 5s 轮询展示',
    cancel_flag         TINYINT(1)    NOT NULL DEFAULT 0,
    trigger_by          VARCHAR(64)   NOT NULL COMMENT 'admin 用户或 kb-planner',
    created_at          TIMESTAMP(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    finished_at         TIMESTAMP(6)  NULL,
    INDEX idx_kb_task_kb (kb_id, status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 12. keyword 倒排候选集（D1，索引级增补，不改表结构）----------
-- ngram 解析器：CJK bigram + 单字；ngram_token_size 需在建索引前定死（03 §5.2/§8.1）
ALTER TABLE eag_kb_page
    ADD FULLTEXT KEY ft_kb_page_ngram (title, body_md) WITH PARSER ngram;

-- 版本登记（幂等；与 001 baseline 的登记写法一致，02 §2.1）
INSERT INTO eag_schema_migration (version_no, name, applied_at)
SELECT 4, 'kb-p0-p1', CURRENT_TIMESTAMP(6)
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM eag_schema_migration WHERE version_no = 4);
