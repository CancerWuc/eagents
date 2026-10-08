-- Flow bindings: the master only picks the target, input and output pass through.
ALTER TABLE eag_master_binding
 ADD COLUMN interaction_mode VARCHAR(16) NOT NULL DEFAULT 'TOOL';

-- Routing metadata only (which flow binding owns the context). No inputs/outputs.
CREATE TABLE IF NOT EXISTS eag_master_active_flow (
    master_id VARCHAR(32) NOT NULL,
    context_id VARCHAR(128) NOT NULL,
    subject_id VARCHAR(128) NOT NULL,
    binding_id VARCHAR(32) NOT NULL,
    service_id VARCHAR(32) NOT NULL,
    awaiting VARCHAR(16) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    expires_at DATETIME(6) NOT NULL,
    PRIMARY KEY (master_id, context_id, subject_id),
    KEY idx_master_active_flow_expiry (expires_at)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
