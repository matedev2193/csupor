-- Private password reset email diagnostics (MySQL / MariaDB).
-- Additive and repeatable: normal application startup also creates this table.
-- Recovery tokens, credentials and SMTP provider replies are never stored here.
CREATE TABLE IF NOT EXISTS password_reset_deliveries (
  id VARCHAR(32) NOT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  recipient_email VARCHAR(120) NOT NULL,
  status VARCHAR(32) NOT NULL,
  safe_error_code VARCHAR(32) NULL,
  attempts INT NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  KEY ix_password_reset_deliveries_created_at (created_at)
) ENGINE=InnoDB;
