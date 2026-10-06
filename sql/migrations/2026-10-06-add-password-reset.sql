-- Email password recovery (MySQL / MariaDB).
-- Additive and repeatable: normal application startup also creates these tables.
-- Match users.id in older signed and SQL-installed unsigned databases.
SET @csupor_reset_user_id_type = (
  SELECT COLUMN_TYPE FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'id'
);
SET @csupor_reset_tokens_ddl = CONCAT(
  'CREATE TABLE IF NOT EXISTS password_reset_tokens (
    token_hash VARCHAR(64) NOT NULL,
    user_id ', @csupor_reset_user_id_type, ' NOT NULL,
    credentials_hash VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL,
    expires_at DATETIME NOT NULL,
    consumed_at DATETIME NULL,
    PRIMARY KEY (token_hash),
    KEY ix_password_reset_tokens_user_id (user_id),
    KEY ix_password_reset_tokens_expires_at (expires_at),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
  ) ENGINE=InnoDB'
);
PREPARE csupor_reset_tokens_migration FROM @csupor_reset_tokens_ddl;
EXECUTE csupor_reset_tokens_migration;
DEALLOCATE PREPARE csupor_reset_tokens_migration;

CREATE TABLE IF NOT EXISTS password_reset_throttles (
  key_hash VARCHAR(64) NOT NULL,
  expires_at DATETIME NOT NULL,
  request_count INT NOT NULL,
  PRIMARY KEY (key_hash),
  KEY ix_password_reset_throttles_expires_at (expires_at)
) ENGINE=InnoDB;

SET @csupor_reset_user_id_type = NULL;
SET @csupor_reset_tokens_ddl = NULL;
