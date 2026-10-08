-- Old mappings have no authenticated owner and cannot be reused.
ALTER TABLE eag_hiagent_conversation
 ADD COLUMN subject_id VARCHAR(64) NOT NULL DEFAULT '';

ALTER TABLE eag_hiagent_conversation
 ADD COLUMN identity_digest CHAR(64) NOT NULL DEFAULT '';

-- OceanBase/MySQL compatibility: primary-key DDL must be executed separately.
ALTER TABLE eag_hiagent_conversation
 DROP PRIMARY KEY;

ALTER TABLE eag_hiagent_conversation
 ADD PRIMARY KEY (context_id, service_id, subject_id);
