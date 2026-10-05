-- Developer-managed page access (MySQL / MariaDB).
-- Run against the selected CSUPOR database after the users table exists.
-- This additive migration may be repeated before or after application startup:
-- it creates only missing tables, preserving every existing permission and user.
-- The application supplies its existing-access defaults when no saved override
-- exists; this migration deliberately does not seed or replace permission rows.
-- Developer access to the permission editor is enforced by application code,
-- independently of submitted checkboxes or database permission overrides.
-- The executing account needs CREATE and REFERENCES permissions. Match the
-- deployed users.id size and signedness to support both SQL-installed unsigned
-- identifiers and older ORM-created signed identifiers.

SET @csupor_page_access_user_id_type = (
  SELECT COLUMN_TYPE
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'users'
    AND COLUMN_NAME = 'id'
);

SET @csupor_page_access_settings_ddl = CONCAT(
  'CREATE TABLE IF NOT EXISTS page_access_settings (
    id INT NOT NULL,
    revision VARCHAR(32) NOT NULL,
    updated_at DATETIME NULL,
    updated_by_id ', @csupor_page_access_user_id_type, ' NULL,
    PRIMARY KEY (id),
    CONSTRAINT single_page_access_settings CHECK (id = 1),
    CONSTRAINT fk_page_access_settings_updated_by_id
      FOREIGN KEY (updated_by_id) REFERENCES users(id) ON DELETE SET NULL
  ) ENGINE=InnoDB'
);

PREPARE csupor_page_access_settings_migration FROM @csupor_page_access_settings_ddl;
EXECUTE csupor_page_access_settings_migration;
DEALLOCATE PREPARE csupor_page_access_settings_migration;

CREATE TABLE IF NOT EXISTS page_role_permissions (
  page_key VARCHAR(64) NOT NULL,
  `role` VARCHAR(20) NOT NULL,
  allowed BOOLEAN NOT NULL DEFAULT FALSE,
  PRIMARY KEY (page_key, `role`),
  CONSTRAINT page_permission_role CHECK (`role` IN ('employee', 'hr', 'ceo', 'developer'))
) ENGINE=InnoDB;

SET @csupor_page_access_user_id_type = NULL;
SET @csupor_page_access_settings_ddl = NULL;
