-- 025｜服务级出站凭证（intranet-a2a-stream-adapter）：authSecretRef
-- 语义升级为「AES-GCM 加密的 headers JSON（base64 文本）」，密文可能超过
-- 原 VARCHAR(256)；MODIFY COLUMN 无 MySQL/OceanBase 方言风险，幂等重跑
-- 由迁移引擎按登记表去重。
ALTER TABLE eag_a2a_service_registration
    MODIFY COLUMN auth_secret_ref VARCHAR(1024) NULL;
