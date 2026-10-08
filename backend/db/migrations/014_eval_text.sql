-- 014｜DEV-07 文本生成指标：用例标准答案文本 + 标注纠正回写
-- 路由维度（expected_service）之外，KB 直答 / 文本生成类用例需要标准答案
-- 文本才能计算 BLEU / ROUGE / F1；expected_text 为空表示该用例不参与
-- 文本指标（与既有路由维度用例兼容，零破坏）。
ALTER TABLE eag_eval_case
    ADD COLUMN expected_text TEXT NULL AFTER expected_service;
-- 与 013 的 corrected_service 同构：标注者给出"正确答案文本"时回写
-- eag_eval_case.expected_text，标注记录本身仍只追加。
ALTER TABLE eag_annotation
    ADD COLUMN corrected_text TEXT NULL AFTER corrected_service;
