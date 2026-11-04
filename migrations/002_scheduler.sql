-- Applied by Repository in ebag/db.py, which checks table_info first because
-- ALTER TABLE has no IF NOT EXISTS in SQLite.
CREATE TABLE IF NOT EXISTS scheduled_slots(day TEXT PRIMARY KEY, status TEXT);
ALTER TABLE scheduled_slots ADD COLUMN attempts INTEGER DEFAULT 0;
ALTER TABLE scheduled_slots ADD COLUMN next_attempt_at TEXT;
ALTER TABLE scheduled_slots ADD COLUMN review_key TEXT;
ALTER TABLE scheduled_slots ADD COLUMN detail TEXT;
