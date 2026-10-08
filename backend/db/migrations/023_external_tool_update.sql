-- 023｜外部工具编辑能力（DEV-05 补齐）：更新时间戳列
-- DATETIME(6)（本地直存口径，与 022 决策一致——仅展示，不参与比较）。
ALTER TABLE eag_external_tool
    ADD COLUMN updated_at DATETIME(6) NULL;
