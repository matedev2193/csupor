-- Unified qualification documents and HR processing (MySQL / MariaDB).
-- Additive and repeatable. Application startup performs the same migration
-- automatically under its database initialisation lock. No legacy rows are
-- removed or updated; existing processed records are never overwritten.
-- Run while application writers are stopped, using a client that stops on error.
SET @csupor_qual_user_type = (
  SELECT COLUMN_TYPE FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'id'
);

SET @csupor_qual_ddl = CONCAT('
CREATE TABLE IF NOT EXISTS qualification_records (
  id INT NOT NULL AUTO_INCREMENT,
  user_id ', @csupor_qual_user_type, ' NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT ''uploaded'',
  kind VARCHAR(40) NULL,
  qualification_name VARCHAR(255) NULL,
  level_or_type VARCHAR(120) NULL,
  institution_name VARCHAR(255) NULL,
  degree_number VARCHAR(120) NULL,
  year_obtained INT NULL,
  date_obtained DATE NULL,
  highest TINYINT(1) NOT NULL DEFAULT 0,
  completion_state VARCHAR(20) NULL,
  study_start_date DATE NULL,
  study_end_date DATE NULL,
  study_categories JSON NULL,
  award_categories JSON NULL,
  training_topic VARCHAR(40) NULL,
  organiser_type VARCHAR(40) NULL,
  funding_type VARCHAR(40) NULL,
  attendance_mode VARCHAR(40) NULL,
  duration_hours DECIMAL(8,2) NULL,
  credits DECIMAL(8,2) NULL,
  digital_pedagogy TINYINT(1) NOT NULL DEFAULT 0,
  notes TEXT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  processed_at DATETIME NULL,
  processed_by_id ', @csupor_qual_user_type, ' NULL,
  revision INT NOT NULL DEFAULT 1,
  legacy_source VARCHAR(40) NULL,
  legacy_id INT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_qualification_record_legacy (legacy_source, legacy_id),
  KEY ix_qualification_records_user_id (user_id),
  KEY ix_qualification_records_status_user_id (status, user_id),
  CONSTRAINT fk_qualification_records_user_id
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_qualification_records_processed_by_id
    FOREIGN KEY (processed_by_id) REFERENCES users(id) ON DELETE SET NULL,
  CONSTRAINT qualification_record_status CHECK (status IN (''uploaded'', ''processed''))
) ENGINE=InnoDB');
PREPARE csupor_qual_migration FROM @csupor_qual_ddl;
EXECUTE csupor_qual_migration;
DEALLOCATE PREPARE csupor_qual_migration;

SET @csupor_qual_record_type = (
  SELECT COLUMN_TYPE FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'qualification_records' AND COLUMN_NAME = 'id'
);

SET @csupor_qual_ddl = CONCAT('
CREATE TABLE IF NOT EXISTS qualification_documents (
  id INT NOT NULL AUTO_INCREMENT,
  record_id ', @csupor_qual_record_type, ' NOT NULL,
  filename VARCHAR(255) NOT NULL,
  mime_type VARCHAR(80) NOT NULL,
  size_bytes INT NOT NULL,
  data MEDIUMBLOB NOT NULL,
  uploaded_by_id ', @csupor_qual_user_type, ' NULL,
  uploaded_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_qualification_documents_record_id (record_id),
  CONSTRAINT fk_qualification_documents_record_id
    FOREIGN KEY (record_id) REFERENCES qualification_records(id) ON DELETE CASCADE,
  CONSTRAINT fk_qualification_documents_uploaded_by_id
    FOREIGN KEY (uploaded_by_id) REFERENCES users(id) ON DELETE SET NULL,
  CONSTRAINT qualification_document_size CHECK (size_bytes > 0 AND size_bytes <= 10485760)
) ENGINE=InnoDB');
PREPARE csupor_qual_migration FROM @csupor_qual_ddl;
EXECUTE csupor_qual_migration;
DEALLOCATE PREPARE csupor_qual_migration;

-- Older installations might still contain year-only source tables.
SET @csupor_qual_ddl = IF(
  EXISTS(SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'educational_qualifications' AND COLUMN_NAME = 'date_obtained'),
  'SELECT 1', 'ALTER TABLE educational_qualifications ADD COLUMN date_obtained DATE NULL'
);
PREPARE csupor_qual_migration FROM @csupor_qual_ddl;
EXECUTE csupor_qual_migration;
DEALLOCATE PREPARE csupor_qual_migration;

-- Older installations might still contain year-only source tables.
SET @csupor_qual_ddl = IF(
  EXISTS(SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'professional_exams' AND COLUMN_NAME = 'date_obtained'),
  'SELECT 1', 'ALTER TABLE professional_exams ADD COLUMN date_obtained DATE NULL'
);
PREPARE csupor_qual_migration FROM @csupor_qual_ddl;
EXECUTE csupor_qual_migration;
DEALLOCATE PREPARE csupor_qual_migration;

START TRANSACTION;
INSERT INTO qualification_records (
  user_id, status, kind, qualification_name, level_or_type, institution_name,
  degree_number, year_obtained, date_obtained, highest, completion_state,
  created_at, updated_at, processed_at, legacy_source, legacy_id
)
SELECT q.user_id, 'processed', 'qualification', q.qualification_name,
  q.level_or_type, q.institution_name, q.degree_number, q.year_obtained,
  q.date_obtained, q.highest, 'completed', UTC_TIMESTAMP(), UTC_TIMESTAMP(),
  UTC_TIMESTAMP(), 'educational_qualifications', q.id
FROM educational_qualifications q
LEFT JOIN qualification_records r
  ON r.legacy_source = 'educational_qualifications' AND r.legacy_id = q.id
WHERE r.id IS NULL;
INSERT INTO qualification_records (
  user_id, status, kind, qualification_name, degree_number, year_obtained,
  date_obtained, completion_state, award_categories, created_at, updated_at,
  processed_at, legacy_source, legacy_id
)
SELECT e.user_id, 'processed', 'exam', e.qualification_name, e.degree_number,
  e.year_obtained, e.date_obtained, 'completed', JSON_ARRAY('professional_exam'),
  UTC_TIMESTAMP(), UTC_TIMESTAMP(), UTC_TIMESTAMP(), 'professional_exams', e.id
FROM professional_exams e
LEFT JOIN qualification_records r
  ON r.legacy_source = 'professional_exams' AND r.legacy_id = e.id
WHERE r.id IS NULL;
COMMIT;
SET @csupor_qual_user_type = NULL;
SET @csupor_qual_record_type = NULL;
SET @csupor_qual_ddl = NULL;
