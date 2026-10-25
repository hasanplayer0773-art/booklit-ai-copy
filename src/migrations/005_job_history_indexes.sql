-- migrations/005_job_history_indexes.sql
-- Corresponds to 004_job_history_indexes.sql in plan/STEP-6.9.md §3 / AC-6.9.5,
-- sequenced as 005 because 004 was assigned to 004_question_occurrences.sql in STEP-6.7.
-- Required for Dashboard query performance at print-shop-realistic
-- volumes (thousands of jobs over time) — without these, aggregate
-- queries scan the full table on every Dashboard view/refresh.

CREATE INDEX IF NOT EXISTS idx_job_history_started_at ON job_history(started_at);
CREATE INDEX IF NOT EXISTS idx_job_history_status ON job_history(status);
CREATE INDEX IF NOT EXISTS idx_job_history_status_started_at ON job_history(status, started_at);
