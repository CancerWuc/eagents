-- 页面权限：用户/用户组 × 页面 × 查看/管理。
-- 无权限不落行；个人与所在组授权在运行时取最高级别。
CREATE TABLE IF NOT EXISTS eag_page_permission (
    subject_type     VARCHAR(16)  NOT NULL COMMENT 'USER / GROUP',
    subject_id       VARCHAR(64)  NOT NULL,
    page_key         VARCHAR(32)  NOT NULL,
    permission_level VARCHAR(16)  NOT NULL COMMENT 'view / manage',
    created_at       TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at       TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                     ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (subject_type, subject_id, page_key),
    INDEX idx_page_permission_subject (subject_type, subject_id),
    INDEX idx_page_permission_page (page_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
