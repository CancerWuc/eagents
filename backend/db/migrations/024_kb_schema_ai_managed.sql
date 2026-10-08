-- 024｜KB schema/purpose AI 自动维护标记（原 011 重号被版本记账跳过，改号 024 后由启动迁移自动补齐）
-- 幂等性: 由 apply_pending_migrations 保证（容忍 1060 重复列）

-- 语义:
--   *_ai_managed = 1: 该字段内容由 AI 生成并随入库持续演进（UI 显示「AI 维护」徽章）；
--   用户通过 PUT /knowledge-bases/{id} 提交非空值 → 置 0（人工权威，AI 不再改写）；
--   用户提交空串 → 置 1（显式交回 AI 维护）；
--   建库未填（NULL）→ AI 首次演进时置 1。
ALTER TABLE eag_knowledge_base
  ADD COLUMN schema_ai_managed TINYINT(1) NOT NULL DEFAULT 0;

ALTER TABLE eag_knowledge_base
  ADD COLUMN purpose_ai_managed TINYINT(1) NOT NULL DEFAULT 0;

INSERT INTO eag_schema_migration (version_no, name)
VALUES (24, '024_kb_schema_ai_managed');
