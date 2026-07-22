ALTER TABLE contract_leave_limits
  ADD COLUMN previous_limit_days INT NULL AFTER imported,
  ADD COLUMN previous_period_start DATE NULL AFTER previous_limit_days,
  ADD COLUMN previous_period_end DATE NULL AFTER previous_period_start,
  ADD COLUMN previous_imported TINYINT(1) NULL AFTER previous_period_end;
