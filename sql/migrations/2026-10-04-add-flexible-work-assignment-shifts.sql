-- Opt-in assignments without an assigned shift (MySQL / MariaDB).
-- Run against the selected CSUPOR database after work_assignments exists.
-- The executing account needs ALTER permission on work_assignments when the
-- column is absent. The application performs the same additive change during
-- startup, so applying this script before or after startup is safe to repeat.
-- FALSE preserves every existing assignment's original 0/1 shift_phase.
-- TRUE lets the scheduler choose the shift; no phase or existing row is changed.

SET @csupor_flexible_shift_ddl = (
  SELECT IF(
    COUNT(*) = 0,
    'ALTER TABLE work_assignments ADD COLUMN flexible_shift BOOLEAN NOT NULL DEFAULT FALSE',
    'SELECT ''flexible_shift already exists'' AS migration_status'
  )
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'work_assignments'
    AND COLUMN_NAME = 'flexible_shift'
);

PREPARE csupor_flexible_shift_migration FROM @csupor_flexible_shift_ddl;
EXECUTE csupor_flexible_shift_migration;
DEALLOCATE PREPARE csupor_flexible_shift_migration;
