"""Fictional, local-only demo seed. No medical recommendations and no live messages.

The seed writes complete history (confirmed doses, one unconfirmed dose and its family alert,
daily check-in replies) so the dashboard is meaningful on first run. It runs once; restarting
the app never duplicates it.
"""
import json
from datetime import timedelta
from zoneinfo import ZoneInfo

from werkzeug.security import generate_password_hash

from .db import get_db, one, transaction
from .services import create_alert, generate_events
from .validation import local_instant, stamp, utc_now

DEMO_EMAIL = 'demo@sahayak.local'
DEMO_PASSWORD = 'SahayakDemo!2026'
DEMO_TIMEZONE = 'Asia/Kolkata'
DEMO_PATIENT_PHONE = '+12025550101'
DEMO_CONTACT_PHONE = '+12025550103'


def seed_demo():
    db = get_db()
    if one("SELECT value FROM metadata WHERE key='demo_seeded'"):
        return
    with transaction():
        if one("SELECT value FROM metadata WHERE key='demo_seeded'"):
            return
        now = utc_now()
        local_today = now.astimezone(ZoneInfo(DEMO_TIMEZONE)).date()
        uid = db.execute(
            'INSERT INTO users (email,name,password_hash,family_phone,timezone) VALUES (?,?,?,?,?)',
            (DEMO_EMAIL, 'Alex (demo family)', generate_password_hash(DEMO_PASSWORD),
             DEMO_CONTACT_PHONE, DEMO_TIMEZONE)).lastrowid
        for n, (name, number) in enumerate([('Maya Demo', DEMO_PATIENT_PHONE),
                                            ('Arun Demo', '+12025550102')]):
            pid = db.execute('''INSERT INTO patients
                (user_id,name,phone,timezone,contact_name,contact_phone,created_at)
                VALUES (?,?,?,?,?,?,?)''',
                (uid, name, number, DEMO_TIMEZONE, 'Alex (demo family)', DEMO_CONTACT_PHONE,
                 stamp(now))).lastrowid
            start = (local_today - timedelta(days=6)).isoformat()
            mid = db.execute('''INSERT INTO medicines
                 (patient_id,name,dosage,instructions,start_date,times,fingerprint,created_at)
                 VALUES (?,?,?,?,?,?,?,?)''',
                 (pid, 'Fictional Care Tablet ' + ('A' if n == 0 else 'B'),
                  'Demo label only - not a real prescription', 'Practice data. Do not use for treatment.',
                  start, json.dumps(['08:00', '20:00']), f'seed:{pid}', stamp(now))).lastrowid
            p = one('SELECT * FROM patients WHERE id=?', (pid,))
            u = one('SELECT * FROM users WHERE id=?', (uid,))
            for days in range(1, 7):
                day = local_today - timedelta(days=days)
                for at in ('08:00', '20:00'):
                    scheduled = local_instant(day, at, DEMO_TIMEZONE)
                    no_reply = (days == 3 and at == '20:00')
                    did = db.execute('''INSERT INTO doses
                        (medicine_id,scheduled_at,status,response,responded_at,source,notified_at,expected_at,is_demo,medicine_name,dosage_text,instruction_text,missed_at)
                        VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?)''',
                        (mid, stamp(scheduled), 'missed' if no_reply else 'taken',
                         None if no_reply else 'TAKEN',
                         None if no_reply else stamp(scheduled + timedelta(minutes=5)),
                         'response_window_elapsed' if no_reply else 'fictional_demo_seed',
                         stamp(scheduled), stamp(scheduled + timedelta(hours=1)),
                         'Fictional Care Tablet ' + ('A' if n == 0 else 'B'),
                         'Demo label only - not a real prescription',
                         'Practice data. Do not use for treatment.',
                         stamp(scheduled + timedelta(hours=1)) if no_reply else None)).lastrowid
                    db.execute('INSERT INTO dose_events (dose_id,status,response,source,created_at) VALUES (?,?,?,?,?)',
                               (did, 'missed' if no_reply else 'taken',
                                'No confirmation received within the response window' if no_reply else 'TAKEN',
                                'fictional_demo_seed', stamp(scheduled)))
                    if no_reply:
                        create_alert(u, p, 'missed_response', f'dose:{did}',
                                     f'SAHAYAK Alert: {name} has not responded to the Fictional Care Tablet '
                                     f'reminder scheduled for {day.strftime("%d %b %Y")} 20:00. Please check '
                                     f'with them. (Dose code D{did}. No reply does not prove the medicine was '
                                     f'not taken, and this is not confirmation of an emergency.)',
                                     scheduled + timedelta(hours=1))
            # A recorded daily well-being reply, so the check-in panel is not empty.
            cid = db.execute('INSERT INTO checkins (patient_id,local_date,scheduled_at,response,responded_at,source,notified_at,expected_at) '
                             'VALUES (?,?,?,?,?,?,?,?)',
                             (pid, (local_today - timedelta(days=1)).isoformat(),
                              stamp(local_instant(local_today - timedelta(days=1), '10:00', DEMO_TIMEZONE)),
                              'BETTER', stamp(local_instant(local_today - timedelta(days=1), '10:12', DEMO_TIMEZONE)),
                              'fictional_demo_seed',
                              stamp(local_instant(local_today - timedelta(days=1), '10:00', DEMO_TIMEZONE)),
                              stamp(local_instant(local_today - timedelta(days=1), '11:00', DEMO_TIMEZONE)))).lastrowid
            db.execute('INSERT INTO reply_events (patient_id,dose_id,body,outcome,source,created_at) VALUES (?,?,?,?,?,?)',
                       (pid, None, 'BETTER C%d' % cid, 'checkin_better', 'fictional_demo_seed', stamp(now)))
        generate_events(now, uid)
        db.execute("INSERT INTO metadata VALUES ('demo_seeded','1')")
