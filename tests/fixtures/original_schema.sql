
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
 password_hash TEXT NOT NULL, name TEXT NOT NULL, family_phone TEXT NOT NULL DEFAULT '',
 timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata', response_minutes INTEGER NOT NULL DEFAULT 60,
 checkin_time TEXT NOT NULL DEFAULT '10:00', repeat_minutes INTEGER NOT NULL DEFAULT 15,
 repeat_limit INTEGER NOT NULL DEFAULT 1, notifications TEXT NOT NULL DEFAULT 'local',
 secret_version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS patients (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), name TEXT NOT NULL,
 phone TEXT NOT NULL UNIQUE, timezone TEXT NOT NULL, contact_name TEXT NOT NULL DEFAULT '',
 contact_phone TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS medicines (
 id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL REFERENCES patients(id), name TEXT NOT NULL,
 dosage TEXT NOT NULL, instructions TEXT NOT NULL DEFAULT '', start_date TEXT NOT NULL,
 end_date TEXT, times TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active',
 fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(patient_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS doses (
 id INTEGER PRIMARY KEY, medicine_id INTEGER NOT NULL REFERENCES medicines(id),
 scheduled_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', response TEXT,
 responded_at TEXT, source TEXT, notified_at TEXT, expected_at TEXT, is_demo INTEGER NOT NULL DEFAULT 0,
 medicine_name TEXT, dosage_text TEXT, instruction_text TEXT,
 UNIQUE(medicine_id, scheduled_at)
);
CREATE TABLE IF NOT EXISTS dose_events (
 id INTEGER PRIMARY KEY, dose_id INTEGER NOT NULL REFERENCES doses(id), status TEXT NOT NULL,
 response TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkins (
 id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL REFERENCES patients(id), local_date TEXT NOT NULL,
 scheduled_at TEXT NOT NULL, response TEXT, responded_at TEXT, source TEXT,
 notified_at TEXT, expected_at TEXT, UNIQUE(patient_id, local_date)
);
CREATE TABLE IF NOT EXISTS alerts (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), patient_id INTEGER NOT NULL REFERENCES patients(id),
 kind TEXT NOT NULL, entity_key TEXT NOT NULL UNIQUE, text TEXT NOT NULL, created_at TEXT NOT NULL,
 acknowledged_at TEXT, acknowledged_by INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), patient_id INTEGER REFERENCES patients(id),
 dedupe_key TEXT NOT NULL UNIQUE, direction TEXT NOT NULL DEFAULT 'outbound',
 kind TEXT NOT NULL, entity_id INTEGER, recipient TEXT NOT NULL, body TEXT NOT NULL,
 transport TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
 next_at TEXT NOT NULL, created_at TEXT NOT NULL, sent_at TEXT, provider_sid TEXT UNIQUE,
 error_code TEXT, locked_at TEXT, delivery_status TEXT
);
CREATE TABLE IF NOT EXISTS inbound (
 sid TEXT PRIMARY KEY, patient_id INTEGER REFERENCES patients(id), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads (
 id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL REFERENCES patients(id), original_name TEXT NOT NULL,
 storage_name TEXT NOT NULL UNIQUE, size INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
 key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, blocked_until TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leases (name TEXT PRIMARY KEY, expires_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_patient_owner ON patients(user_id);
CREATE INDEX IF NOT EXISTS ix_doses_time ON doses(scheduled_at);
CREATE INDEX IF NOT EXISTS ix_messages_queue ON messages(status,next_at);
CREATE INDEX IF NOT EXISTS ix_alerts_owner ON alerts(user_id,created_at);
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO metadata VALUES ('schema_version','1');
