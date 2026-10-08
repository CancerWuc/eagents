-- Only routing/claim metadata. No card payload, business inputs or credentials.
CREATE TABLE IF NOT EXISTS eag_master_card_submission (
    card_id VARCHAR(32) NOT NULL PRIMARY KEY,
    subject_id VARCHAR(128) NOT NULL,
    master_id VARCHAR(32) NOT NULL,
    context_id VARCHAR(128) NOT NULL,
    service_id VARCHAR(32) NOT NULL,
    conversation_id VARCHAR(128) NOT NULL,
    identity_digest CHAR(64) NOT NULL,
    config_version CHAR(64) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    expires_at DATETIME(6) NOT NULL,
    state VARCHAR(16) NOT NULL DEFAULT 'OPEN',
    submission_run_id VARCHAR(32) NULL,
    finished_at DATETIME(6) NULL,
    error_code VARCHAR(64) NULL,
    KEY idx_master_card_expiry (expires_at),
    KEY idx_master_card_context (master_id, context_id)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
