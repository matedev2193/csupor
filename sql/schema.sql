CREATE DATABASE IF NOT EXISTS csupor
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE csupor;

CREATE TABLE IF NOT EXISTS users (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT,
  email VARCHAR(120) NOT NULL,
  username VARCHAR(50) NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  privilege ENUM('employee', 'hr', 'ceo', 'developer') NOT NULL DEFAULT 'employee',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_users_email (email),
  UNIQUE KEY uq_users_username (username)
) ENGINE=InnoDB;

INSERT INTO users (email, username, password_hash, privilege)
VALUES (
  'admin@csupor.matedev.hu',
  'admin',
  'scrypt:32768:8:1$F7DowUSAoUixs84T$274b978abad81b5c326b4443cfbc498921e2be34e7e3c61f76b019833af034def12ed0542c52ac7670fdee803ba6ac1ddfcaddac0e90901c2276b2cc4df60113',
  'developer'
)
ON DUPLICATE KEY UPDATE
  email = VALUES(email),
  password_hash = VALUES(password_hash),
  privilege = VALUES(privilege);

CREATE TABLE IF NOT EXISTS user_profiles (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id INT UNSIGNED NOT NULL,
  full_name VARCHAR(120) NULL,
  name_at_birth VARCHAR(120) NULL,
  date_of_birth DATE NULL,
  place_of_birth VARCHAR(120) NULL,
  gender ENUM('male', 'female', 'other') NULL,
  mothers_maiden_name VARCHAR(120) NULL,
  citizenships VARCHAR(255) NULL,
  social_security_number CHAR(9) NULL,
  tax_number CHAR(10) NULL,
  education_number CHAR(11) NULL,
  teacher_id_card_number VARCHAR(64) NULL,
  permanent_residence VARCHAR(255) NULL,
  temporary_address VARCHAR(255) NULL,
  phone_number VARCHAR(40) NULL,
  bank_account_number VARCHAR(64) NULL,
  marital_status ENUM('single', 'married', 'divorced', 'widowed', 'civil partnership') NULL,
  disability VARCHAR(255) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_user_profiles_user_id (user_id),
  CONSTRAINT fk_user_profiles_user_id
    FOREIGN KEY (user_id) REFERENCES users(id)
    ON DELETE CASCADE
    ON UPDATE CASCADE,
  CONSTRAINT chk_user_profiles_ssn_digits
    CHECK (social_security_number IS NULL OR social_security_number REGEXP '^[0-9]{9}$'),
  CONSTRAINT chk_user_profiles_tax_digits
    CHECK (tax_number IS NULL OR tax_number REGEXP '^[0-9]{10}$'),
  CONSTRAINT chk_user_profiles_education_digits
    CHECK (education_number IS NULL OR education_number REGEXP '^[0-9]{11}$')
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS dependents (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id INT UNSIGNED NOT NULL,
  name VARCHAR(120) NOT NULL,
  dependent_type ENUM('child', 'other dependent') NOT NULL DEFAULT 'child',
  date_of_birth DATE NOT NULL,
  social_security_number CHAR(9) NOT NULL,
  dependency_start DATE NOT NULL,
  disability VARCHAR(255) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_dependents_user_id (user_id),
  CONSTRAINT fk_dependents_user_id
    FOREIGN KEY (user_id) REFERENCES users(id)
    ON DELETE CASCADE
    ON UPDATE CASCADE,
  CONSTRAINT chk_dependents_ssn_digits
    CHECK (social_security_number REGEXP '^[0-9]{9}$')
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS educational_qualifications (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id INT UNSIGNED NOT NULL,
  level_or_type VARCHAR(120) NOT NULL,
  qualification_name VARCHAR(120) NOT NULL,
  institution_name VARCHAR(120) NOT NULL,
  degree_number VARCHAR(80) NOT NULL,
  year_obtained SMALLINT UNSIGNED NOT NULL,
  highest TINYINT(1) NOT NULL DEFAULT 0,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_educational_qualifications_user_id (user_id),
  KEY idx_educational_qualifications_highest (user_id, highest),
  CONSTRAINT fk_educational_qualifications_user_id
    FOREIGN KEY (user_id) REFERENCES users(id)
    ON DELETE CASCADE
    ON UPDATE CASCADE,
  CONSTRAINT chk_educational_qualifications_year
    CHECK (year_obtained BETWEEN 1900 AND 9999)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS professional_exams (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id INT UNSIGNED NOT NULL,
  qualification_name VARCHAR(120) NOT NULL,
  year_obtained SMALLINT UNSIGNED NOT NULL,
  degree_number VARCHAR(80) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_professional_exams_user_id (user_id),
  CONSTRAINT fk_professional_exams_user_id
    FOREIGN KEY (user_id) REFERENCES users(id)
    ON DELETE CASCADE
    ON UPDATE CASCADE,
  CONSTRAINT chk_professional_exams_year
    CHECK (year_obtained BETWEEN 1900 AND 9999)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS legal_entities (
  id INT NOT NULL AUTO_INCREMENT,
  name VARCHAR(120) NOT NULL,
  address VARCHAR(255) NOT NULL,
  om_id CHAR(6) NOT NULL,
  tax_number CHAR(11) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  CONSTRAINT chk_legal_entities_om_id_digits
    CHECK (om_id REGEXP '^[0-9]{6}$'),
  CONSTRAINT chk_legal_entities_tax_number_digits
    CHECK (tax_number REGEXP '^[0-9]{11}$')
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS places_of_work (
  id INT NOT NULL AUTO_INCREMENT,
  legal_entity_id INT NOT NULL,
  address VARCHAR(255) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_places_of_work_legal_entity_id (legal_entity_id),
  CONSTRAINT fk_places_of_work_legal_entity_id
    FOREIGN KEY (legal_entity_id) REFERENCES legal_entities(id)
    ON DELETE CASCADE
    ON UPDATE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS contracts (
  id INT NOT NULL AUTO_INCREMENT,
  user_id INT UNSIGNED NOT NULL,
  contract_type ENUM('Teacher', 'Teaching Assistant', 'Nursery Assistant', 'Secretary', 'Employee under the Labour Code') NOT NULL,
  start_date DATE NOT NULL,
  end_date DATE NULL,
  certificate_of_good_conduct_number VARCHAR(64) NULL,
  certificate_of_good_conduct_date DATE NULL,
  job_title VARCHAR(120) NOT NULL,
  working_hours_per_week INT NOT NULL,
  teacher_classification ENUM('Trainee', 'Teacher I', 'Teacher II', 'Master Teacher', 'Research Teacher') NULL,
  classification_start_date DATE NULL,
  legal_entity_id INT NOT NULL,
  place_of_work_id INT NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_contracts_user_id (user_id),
  KEY idx_contracts_legal_entity_id (legal_entity_id),
  KEY idx_contracts_place_of_work_id (place_of_work_id),
  CONSTRAINT fk_contracts_user_id
    FOREIGN KEY (user_id) REFERENCES users(id)
    ON DELETE CASCADE
    ON UPDATE CASCADE,
  CONSTRAINT fk_contracts_legal_entity_id
    FOREIGN KEY (legal_entity_id) REFERENCES legal_entities(id)
    ON DELETE RESTRICT
    ON UPDATE CASCADE,
  CONSTRAINT fk_contracts_place_of_work_id
    FOREIGN KEY (place_of_work_id) REFERENCES places_of_work(id)
    ON DELETE RESTRICT
    ON UPDATE CASCADE,
  CONSTRAINT chk_contracts_working_hours_positive
    CHECK (working_hours_per_week > 0),
  CONSTRAINT chk_contracts_date_order
    CHECK (end_date IS NULL OR end_date >= start_date)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS leadership (
  id INT NOT NULL AUTO_INCREMENT,
  legal_entity_id INT NOT NULL,
  contract_id INT NOT NULL,
  position ENUM('principal', 'deputy principal') NOT NULL DEFAULT 'principal',
  start_date DATE NOT NULL,
  end_date DATE NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_leadership_legal_entity_id (legal_entity_id),
  KEY idx_leadership_contract_id (contract_id),
  CONSTRAINT fk_leadership_legal_entity_id
    FOREIGN KEY (legal_entity_id) REFERENCES legal_entities(id)
    ON DELETE RESTRICT
    ON UPDATE CASCADE,
  CONSTRAINT fk_leadership_contract_id
    FOREIGN KEY (contract_id) REFERENCES contracts(id)
    ON DELETE CASCADE
    ON UPDATE CASCADE,
  CONSTRAINT chk_leadership_date_order
    CHECK (end_date IS NULL OR end_date >= start_date)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS contract_leave_limits (
  id INT NOT NULL AUTO_INCREMENT,
  contract_id INT NOT NULL,
  calendar_year SMALLINT UNSIGNED NOT NULL,
  leave_type ENUM(
    'basic leave',
    'supplementary leave based on age',
    'supplementary leave for children',
    'supplementary leave for children with disability',
    'supplementary leave for young employees',
    'supplementary leave for employees with reduced working capacity / eligible for disability benefits',
    'sick leave',
    'leave carried over from previous year',
    'maternity leave',
    'paternity leave',
    'parental leave',
    'childcare fee',
    'childcare allowance',
    'supplementary leave for the birth of a grandchild',
    'supplementary leave for first marriage',
    'exemption from obligation to work'
  ) NOT NULL,
  limit_days INT NOT NULL,
  period_start DATE NULL,
  period_end DATE NULL,
  imported TINYINT(1) NOT NULL DEFAULT 0,
  previous_limit_days INT NULL,
  previous_period_start DATE NULL,
  previous_period_end DATE NULL,
  previous_imported TINYINT(1) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_contract_leave_limits_scope (contract_id, calendar_year, leave_type),
  KEY idx_contract_leave_limits_contract (contract_id),
  CONSTRAINT fk_contract_leave_limits_contract_id
    FOREIGN KEY (contract_id) REFERENCES contracts(id)
    ON DELETE CASCADE
    ON UPDATE CASCADE,
  CONSTRAINT chk_contract_leave_limits_non_negative
    CHECK (limit_days >= 0),
  CONSTRAINT chk_contract_leave_limits_date_order
    CHECK (period_end IS NULL OR period_start IS NULL OR period_end >= period_start)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS leave_years (
  year SMALLINT UNSIGNED NOT NULL,
  is_open TINYINT(1) NOT NULL DEFAULT 0,
  imported_by_id INT UNSIGNED NULL,
  imported_at DATETIME NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (year),
  CONSTRAINT fk_leave_years_imported_by_id
    FOREIGN KEY (imported_by_id) REFERENCES users(id)
    ON DELETE SET NULL
    ON UPDATE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS leave_requests (
  id INT NOT NULL AUTO_INCREMENT,
  user_id INT UNSIGNED NOT NULL,
  contract_id INT NOT NULL,
  category ENUM(
    'paid leave',
    'health leave',
    'childcare sickness benefit',
    'childbirth leave',
    'exemption from obligation to work',
    'unpaid leave'
  ) NOT NULL,
  start_date DATE NOT NULL,
  end_date DATE NULL,
  status ENUM(
    'pending approval',
    'approved',
    'rejected',
    'pending cancellation',
    'cancelled'
  ) NOT NULL DEFAULT 'pending approval',
  note TEXT NULL,
  ceo_approved_by_id INT UNSIGNED NULL,
  leadership_approved_by_id INT UNSIGNED NULL,
  decided_by_id INT UNSIGNED NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_leave_requests_user_contract (user_id, contract_id),
  KEY idx_leave_requests_dates (start_date, end_date),
  KEY idx_leave_requests_status (status),
  KEY idx_leave_requests_ceo_approved_by_id (ceo_approved_by_id),
  KEY idx_leave_requests_leadership_approved_by_id (leadership_approved_by_id),
  KEY idx_leave_requests_decided_by_id (decided_by_id),
  CONSTRAINT fk_leave_requests_user_id
    FOREIGN KEY (user_id) REFERENCES users(id)
    ON DELETE CASCADE
    ON UPDATE CASCADE,
  CONSTRAINT fk_leave_requests_contract_id
    FOREIGN KEY (contract_id) REFERENCES contracts(id)
    ON DELETE CASCADE
    ON UPDATE CASCADE,
  CONSTRAINT fk_leave_requests_ceo_approved_by_id
    FOREIGN KEY (ceo_approved_by_id) REFERENCES users(id)
    ON DELETE SET NULL
    ON UPDATE CASCADE,
  CONSTRAINT fk_leave_requests_leadership_approved_by_id
    FOREIGN KEY (leadership_approved_by_id) REFERENCES users(id)
    ON DELETE SET NULL
    ON UPDATE CASCADE,
  CONSTRAINT fk_leave_requests_decided_by_id
    FOREIGN KEY (decided_by_id) REFERENCES users(id)
    ON DELETE SET NULL
    ON UPDATE CASCADE,
  CONSTRAINT chk_leave_requests_date_order
    CHECK (end_date IS NULL OR end_date >= start_date)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS working_day_overrides (
  id INT NOT NULL AUTO_INCREMENT,
  day DATE NOT NULL,
  is_working_day TINYINT(1) NOT NULL,
  note VARCHAR(255) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_working_day_overrides_day (day)
) ENGINE=InnoDB;
