-- 029｜权限控制 v1（permission-control）：用户资料扩展。
-- eag_console_user 补 last_login_at（权限管理·用户 Tab 展示最后登录时间）。
-- 角色表（eag_user_role）与可见性四表已在基线 schema/026 落地，本迁移只做增量列。
--
-- 版本号取 029：028 已被另一条未并入 master 的分支线（office-baseline 的
-- 028_pluto_console）占用——同号会被迁移登记的 INSERT IGNORE 静默跳过
-- （与 026→027 重编号同因，见 wiki from_hiagent/）。
--
-- 防撞号写法：经 information_schema 守卫，列已存在（如本地 dev 库已被该
-- 分支线建出同义列）时跳过 ALTER，两条迁移线都能安全通过。

SET @col_exists := (SELECT COUNT(*)
                      FROM information_schema.COLUMNS
                     WHERE TABLE_SCHEMA = DATABASE()
                       AND TABLE_NAME = 'eag_console_user'
                       AND COLUMN_NAME = 'last_login_at');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE eag_console_user ADD COLUMN last_login_at TIMESTAMP(6) NULL COMMENT ''最后成功登录时间（Pluto 登录时写入）''',
    'SELECT 1');
PREPARE _st FROM @ddl;
EXECUTE _st;
DEALLOCATE PREPARE _st;
