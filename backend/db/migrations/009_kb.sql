-- 009｜KB 检索/存储改造（from_llm_wiki 08，决策 D-2026-08-16-02 / R-10）
-- 幂等：DROP INDEX 已删除时返回 1091，由迁移引擎容忍
-- 依赖：004_kb.sql（ngram FULLTEXT 索引由 004 创建）
-- 背景：检索去向量化——keyword 供给改为落盘 glob+grep（08 §3.1），
--       ngram FULLTEXT 倒排不再被依赖，随本迁移删除（拍板 §9-3）

ALTER TABLE eag_kb_page DROP INDEX ft_kb_page_ngram;

-- 版本登记（幂等；与 001 baseline 的登记写法一致，02 §2.1）
INSERT INTO eag_schema_migration (version_no, name, applied_at)
SELECT 9, 'kb-devectorize', CURRENT_TIMESTAMP(6)
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM eag_schema_migration WHERE version_no = 9);
