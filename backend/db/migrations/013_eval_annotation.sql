-- 013｜标注可纠正期望服务
-- 原先标注只能记 intent_correct / subjective_note，说得出"错了"却说不出
-- "该是谁"；而 eag_eval_case.expected_service 建集时取自源运行的首个路由，
-- 一旦源运行本身就路由错了，这条用例的标准答案永远是错的，标注也纠不回来。
ALTER TABLE eag_annotation
    ADD COLUMN corrected_service VARCHAR(128) NULL AFTER intent_correct;
