-- SMTP configuration and durable leave-notification queue (MySQL / MariaDB).
-- Run against the selected CSUPOR database after users and leave_requests exist.
-- This additive migration is safe to repeat before or after application startup:
-- it creates missing tables only and preserves all existing settings and events.
-- No mail is sent, no historical notification events are manufactured, and SMTP
-- remains disabled until a developer explicitly saves and enables the settings.
-- The executing account needs CREATE and REFERENCES permissions. Foreign-key
-- integer types are read from the deployed schema because the SQL installation
-- uses unsigned user IDs while older ORM-created databases use signed user IDs.
-- Every queue DATETIME stores UTC, including Budapest's daily 20:00 deadline.

SET @csupor_mail_user_id_type = (
  SELECT COLUMN_TYPE
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'users'
    AND COLUMN_NAME = 'id'
);

SET @csupor_mail_leave_id_type = (
  SELECT COLUMN_TYPE
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'leave_requests'
    AND COLUMN_NAME = 'id'
);

SET @csupor_mail_settings_ddl = CONCAT(
  'CREATE TABLE IF NOT EXISTS mail_server_settings (
    id INT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    host VARCHAR(255) NOT NULL DEFAULT '''',
    port INT NOT NULL DEFAULT 587,
    security VARCHAR(10) NOT NULL DEFAULT ''starttls'',
    username VARCHAR(255) NOT NULL DEFAULT '''',
    encrypted_password TEXT NOT NULL,
    sender_email VARCHAR(254) NOT NULL DEFAULT '''',
    sender_name VARCHAR(120) NOT NULL DEFAULT ''CSUPOR'',
    base_url VARCHAR(500) NOT NULL DEFAULT '''',
    revision VARCHAR(32) NOT NULL,
    updated_at DATETIME NULL,
    updated_by_id ', @csupor_mail_user_id_type, ' NULL,
    PRIMARY KEY (id),
    CONSTRAINT single_mail_server_settings CHECK (id = 1),
    CONSTRAINT mail_server_port CHECK (port BETWEEN 1 AND 65535),
    CONSTRAINT mail_server_security CHECK (security IN (''starttls'', ''ssl'', ''none'')),
    CONSTRAINT fk_mail_server_settings_updated_by_id
      FOREIGN KEY (updated_by_id) REFERENCES users(id) ON DELETE SET NULL
  ) ENGINE=InnoDB'
);

PREPARE csupor_mail_settings_migration FROM @csupor_mail_settings_ddl;
EXECUTE csupor_mail_settings_migration;
DEALLOCATE PREPARE csupor_mail_settings_migration;

SET @csupor_mail_batches_ddl = CONCAT(
  'CREATE TABLE IF NOT EXISTS mail_batches (
    id VARCHAR(32) NOT NULL,
    batch_key VARCHAR(120) NOT NULL,
    recipient_id ', @csupor_mail_user_id_type, ' NOT NULL,
    kind VARCHAR(12) NOT NULL,
    created_at DATETIME NOT NULL,
    due_at DATETIME NOT NULL,
    sent_at DATETIME NULL,
    claimed_at DATETIME NULL,
    next_attempt_at DATETIME NULL,
    claim_token VARCHAR(32) NULL,
    status VARCHAR(12) NOT NULL DEFAULT ''pending'',
    attempts INT NOT NULL DEFAULT 0,
    last_error VARCHAR(80) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_mail_batches_batch_key (batch_key),
    KEY ix_mail_batches_dispatch (status, due_at, next_attempt_at),
    CONSTRAINT fk_mail_batches_recipient_id
      FOREIGN KEY (recipient_id) REFERENCES users(id) ON DELETE CASCADE
  ) ENGINE=InnoDB'
);

PREPARE csupor_mail_batches_migration FROM @csupor_mail_batches_ddl;
EXECUTE csupor_mail_batches_migration;
DEALLOCATE PREPARE csupor_mail_batches_migration;

SET @csupor_leave_notifications_ddl = CONCAT(
  'CREATE TABLE IF NOT EXISTS leave_notifications (
    id INT NOT NULL AUTO_INCREMENT,
    event_key VARCHAR(32) NOT NULL,
    recipient_id ', @csupor_mail_user_id_type, ' NOT NULL,
    leave_request_id ', @csupor_mail_leave_id_type, ' NOT NULL,
    event_type VARCHAR(40) NOT NULL,
    payload JSON NOT NULL,
    is_task BOOLEAN NOT NULL DEFAULT FALSE,
    is_owner BOOLEAN NOT NULL DEFAULT FALSE,
    urgent BOOLEAN NOT NULL DEFAULT FALSE,
    created_at DATETIME NOT NULL,
    due_at DATETIME NOT NULL,
    status VARCHAR(12) NOT NULL DEFAULT ''pending'',
    batch_id VARCHAR(32) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_leave_notification_event_recipient (event_key, recipient_id),
    KEY ix_leave_notifications_dispatch (status, batch_id, due_at),
    KEY ix_leave_notifications_batch_id (batch_id),
    CONSTRAINT fk_leave_notifications_recipient_id
      FOREIGN KEY (recipient_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_leave_notifications_leave_request_id
      FOREIGN KEY (leave_request_id) REFERENCES leave_requests(id) ON DELETE CASCADE,
    CONSTRAINT fk_leave_notifications_batch_id
      FOREIGN KEY (batch_id) REFERENCES mail_batches(id) ON DELETE SET NULL
  ) ENGINE=InnoDB'
);

PREPARE csupor_leave_notifications_migration FROM @csupor_leave_notifications_ddl;
EXECUTE csupor_leave_notifications_migration;
DEALLOCATE PREPARE csupor_leave_notifications_migration;

SET @csupor_mail_user_id_type = NULL;
SET @csupor_mail_leave_id_type = NULL;
SET @csupor_mail_settings_ddl = NULL;
SET @csupor_mail_batches_ddl = NULL;
SET @csupor_leave_notifications_ddl = NULL;
