"""SQLite persistence. Connections are request/worker scoped; UTC ISO text sorts chronologically.

Every new column is added by an idempotent migration, so an existing SAHAYAK database keeps its
records and only gains what is missing. Nothing is dropped or rewritten.
"""
import sqlite3
from contextlib import contextmanager

from flask import current_app, g

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
 password_hash TEXT NOT NULL, name TEXT NOT NULL, family_phone TEXT NOT NULL DEFAULT '',
 timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata', response_minutes INTEGER NOT NULL DEFAULT 60,
 checkin_time TEXT NOT NULL DEFAULT '10:00', repeat_minutes INTEGER NOT NULL DEFAULT 15,
 repeat_limit INTEGER NOT NULL DEFAULT 1, notifications TEXT NOT NULL DEFAULT 'whatsapp',
 secret_version INTEGER NOT NULL DEFAULT 1,
 -- Family alert / escalation policy (reminder and escalation windows).
 escalation_minutes INTEGER NOT NULL DEFAULT 0,
 reply_delay_minutes INTEGER NOT NULL DEFAULT 30,
 missed_followup INTEGER NOT NULL DEFAULT 0,
 alert_on_not_yet INTEGER NOT NULL DEFAULT 1
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
-- One row per medicine per scheduled time. UNIQUE(medicine_id, scheduled_at) is what makes
-- a repeated scheduler run unable to create a duplicate dose.
-- status: pending | awaiting_response | reminder_requested | unresolved | taken | missed
CREATE TABLE IF NOT EXISTS doses (
 id INTEGER PRIMARY KEY, medicine_id INTEGER NOT NULL REFERENCES medicines(id),
 scheduled_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', response TEXT,
 responded_at TEXT, source TEXT, notified_at TEXT, expected_at TEXT, is_demo INTEGER NOT NULL DEFAULT 0,
 medicine_name TEXT, dosage_text TEXT, instruction_text TEXT,
 missed_at TEXT, missed_followup_at TEXT, escalated_at TEXT,
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
-- entity_key is unique: the same missed dose or reply can never raise two alerts.
CREATE TABLE IF NOT EXISTS alerts (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), patient_id INTEGER NOT NULL REFERENCES patients(id),
 kind TEXT NOT NULL, entity_key TEXT NOT NULL UNIQUE, text TEXT NOT NULL, created_at TEXT NOT NULL,
 acknowledged_at TEXT, acknowledged_by INTEGER REFERENCES users(id),
 reason TEXT, delivered_status TEXT, notified_at TEXT, resolved_at TEXT, attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), patient_id INTEGER REFERENCES patients(id),
 dedupe_key TEXT NOT NULL UNIQUE, direction TEXT NOT NULL DEFAULT 'outbound',
 kind TEXT NOT NULL, entity_id INTEGER, recipient TEXT NOT NULL, body TEXT NOT NULL,
 transport TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
 next_at TEXT NOT NULL, created_at TEXT NOT NULL, sent_at TEXT, provider_sid TEXT UNIQUE,
 error_code TEXT, locked_at TEXT, delivery_status TEXT
);
CREATE TABLE IF NOT EXISTS message_attempts (
 id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL REFERENCES messages(id),
 attempt INTEGER NOT NULL, status TEXT NOT NULL, error_code TEXT, provider_sid TEXT, created_at TEXT NOT NULL,
 UNIQUE(message_id,attempt)
);
CREATE TABLE IF NOT EXISTS provider_callbacks (
 sid TEXT NOT NULL, state TEXT NOT NULL, error_code TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
 PRIMARY KEY(sid,state,error_code)
);
CREATE TABLE IF NOT EXISTS inbound (
 sid TEXT PRIMARY KEY, patient_id INTEGER REFERENCES patients(id), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads (
 id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL REFERENCES patients(id), original_name TEXT NOT NULL,
 storage_name TEXT NOT NULL UNIQUE, size INTEGER NOT NULL, created_at TEXT NOT NULL
);
-- Audit trail for every patient reply, including ones that could not be matched.
CREATE TABLE IF NOT EXISTS reply_events (
 id INTEGER PRIMARY KEY, patient_id INTEGER REFERENCES patients(id), dose_id INTEGER REFERENCES doses(id),
 body TEXT NOT NULL, outcome TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
 key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, blocked_until TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leases (name TEXT PRIMARY KEY, expires_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_patient_owner ON patients(user_id);
CREATE INDEX IF NOT EXISTS ix_doses_time ON doses(scheduled_at);
CREATE INDEX IF NOT EXISTS ix_messages_queue ON messages(status,next_at);
CREATE INDEX IF NOT EXISTS ix_alerts_owner ON alerts(user_id,created_at);
CREATE INDEX IF NOT EXISTS ix_reply_events ON reply_events(patient_id,created_at);
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO metadata VALUES ('schema_version','1');
'''

# table -> {column: definition}. Applied with ALTER TABLE only when the column is missing.
MIGRATIONS = {
    'users': {
        'escalation_minutes': "INTEGER NOT NULL DEFAULT 0",
        'reply_delay_minutes': "INTEGER NOT NULL DEFAULT 30",
        'missed_followup': "INTEGER NOT NULL DEFAULT 0",
        'alert_on_not_yet': "INTEGER NOT NULL DEFAULT 1",
    },
    'doses': {
        'medicine_name': 'TEXT', 'dosage_text': 'TEXT', 'instruction_text': 'TEXT',
        'missed_at': 'TEXT', 'missed_followup_at': 'TEXT', 'escalated_at': 'TEXT',
    },
    'alerts': {
        'reason': 'TEXT', 'delivered_status': 'TEXT', 'notified_at': 'TEXT', 'resolved_at': 'TEXT',
        'attempts': 'INTEGER NOT NULL DEFAULT 0',
    },
}


def connect(path):
    c = sqlite3.connect(path, timeout=20, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA busy_timeout=20000')
    return c


def get_db():
    if 'db' not in g:
        g.db = connect(current_app.config['DATABASE'])
    return g.db


def close_db(_=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(SCHEMA)
    # Additive migration only; an existing database keeps every stored record.
    for table, columns in MIGRATIONS.items():
        present = {r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
        for column, definition in columns.items():
            if column not in present:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
    # Older builds stored dose-free text on the dose itself; backfill any missing snapshot.
    db.execute('''UPDATE doses SET
      medicine_name=COALESCE(medicine_name,(SELECT name FROM medicines WHERE id=medicine_id)),
      dosage_text=COALESCE(dosage_text,(SELECT dosage FROM medicines WHERE id=medicine_id)),
      instruction_text=COALESCE(instruction_text,(SELECT instructions FROM medicines WHERE id=medicine_id))
      WHERE medicine_name IS NULL OR dosage_text IS NULL OR instruction_text IS NULL''')
    # A dose whose reminder already went out is "awaiting a reply", not "pending".
    db.execute("UPDATE doses SET status='awaiting_response' WHERE status='pending' AND notified_at IS NOT NULL")
    # The one follow-up that used to be automatic is now opt-in; keep existing rows honest.
    db.execute("UPDATE doses SET missed_followup_at=notified_at WHERE missed_followup_at IS NULL "
               "AND status IN ('missed','unresolved') AND EXISTS (SELECT 1 FROM messages "
               "WHERE kind='missed_followup' AND entity_id=doses.id)")
    db.execute("UPDATE metadata SET value='3' WHERE key='schema_version'")


@contextmanager
def transaction(db=None):
    db = db or get_db()
    db.execute('BEGIN IMMEDIATE')
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise


def one(sql, args=()):
    return get_db().execute(sql, args).fetchone()


def all_rows(sql, args=()):
    return get_db().execute(sql, args).fetchall()
