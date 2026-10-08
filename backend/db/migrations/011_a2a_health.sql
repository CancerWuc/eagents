-- 011｜A2A 注册兼容原因与第一阶段健康观测
ALTER TABLE eag_a2a_service_registration
    ADD COLUMN compatibility_reason VARCHAR(512) NULL AFTER compatibility;
