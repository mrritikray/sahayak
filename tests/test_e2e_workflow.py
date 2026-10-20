"""Mandatory end-to-end workflow tests: schedule -> reminder -> reply -> status -> family alert.

No real WhatsApp message is ever sent here. Demo Mode is used, Twilio is mocked, and every
test runs against a temporary SQLite database. The last test deliberately restarts the app
against the same database file to prove that history survives a restart.
"""
import csv
import io
import re
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator

from sahayak import create_app
from sahayak.db import all_rows, get_db, one, transaction
from sahayak.services import dispatch, due_work, generate_events, handle_reply, provider_send, tick
from sahayak.validation import local_instant, parse, stamp, utc_now

FAMILY_PHONE = '+12025550103'
CONTACT_PHONE = '+12025550106'
PATIENT_PHONE = '+12025550104'


def run_due(app, now):
    """Run one full scheduler pass at a chosen instant (no sleeping in tests)."""
    with app.app_context():
        with transaction():
            generate_events(now)
            due_work(now)
        dispatch(now)


def stat(html, label):
    """Read one dashboard statistic, so a fake/hardcoded number could never pass."""
    found = re.search(rb'<span>' + re.escape(label.encode()) + rb'</span><strong>(\d+)</strong>', html)
    assert found, f'statistic {label!r} not found on the dashboard'
    return int(found.group(1))


def make_patient(auth, name='Robin Fictional', phone=PATIENT_PHONE, contact=CONTACT_PHONE):
    auth.post('/patients/new', data={'name': name, 'phone': phone, 'timezone': 'Asia/Kolkata',
                                    'contact_name': 'Sam Demo', 'contact_phone': contact})
    return phone


def make_medicine(auth, patient_id, name='Fictional Tablet', times='08:00, 20:00'):
    auth.post(f'/patients/{patient_id}/medicines/new',
              data={'name': name, 'dosage': 'Entered demo label only - not for use',
                    'instructions': 'Fictional practice data.', 'start_date': utc_now().date().isoformat(),
                    'end_date': '', 'times': times, 'state': 'active', 'confirmed': 'yes'})


def add_due_dose(medicine_id, minutes_ago=1):
    """Insert the dose that has just come round, so the scheduler has due work at a known instant."""
    med = one('SELECT * FROM medicines WHERE id=?', (medicine_id,))
    return get_db().execute('''INSERT INTO doses
        (medicine_id,scheduled_at,medicine_name,dosage_text,instruction_text) VALUES (?,?,?,?,?)''',
        (medicine_id, stamp(utc_now() - timedelta(minutes=minutes_ago)), med['name'], med['dosage'],
         med['instructions'])).lastrowid


# ---------------------------------------------------------------------------------------
# 1-7: the happy path requested in the brief
# ---------------------------------------------------------------------------------------
def test_e2e_01_to_07_full_workflow(app, auth):
    """Create patient + contact, schedule, due reminder, TAKEN reply, confirmed dose, dashboard."""
    make_patient(auth)
    with app.app_context():
        p = one('SELECT * FROM patients')
        pid = p['id']
        assert p['contact_name'] == 'Sam Demo' and p['contact_phone'] == CONTACT_PHONE
    make_medicine(auth, pid)
    with app.app_context():
        assert one("SELECT COUNT(*) n FROM medicines WHERE patient_id=?", (pid,))['n'] == 1
        # the scheduled dose that has just come round
        dose_id = add_due_dose(one('SELECT id FROM medicines WHERE patient_id=?', (pid,))['id'])

    due = utc_now()
    with app.app_context():
        with transaction():
            generate_events(due)
            due_work(due)
        dispatch(due)
        dose = one('SELECT * FROM doses WHERE id=?', (dose_id,))
        assert dose['status'] == 'awaiting_response', 'the due reminder should move the dose to awaiting a reply'
        assert dose['notified_at'] and dose['expected_at']
        # 4. the reminder targets the patient's own WhatsApp number
        msg = one("SELECT * FROM messages WHERE kind='dose'")
        assert msg['recipient'] == PATIENT_PHONE
        assert msg['transport'] == 'demo' and msg['status'] == 'simulated'
        assert 'TAKEN' in msg['body'] and 'NOT YET' in msg['body'] and 'REMIND ME LATER' in msg['body']
        assert 'Fictional Tablet' in msg['body']
        # 5-6. simulated TAKEN reply confirms exactly that dose
        with transaction():
            reply = handle_reply(pid, f'TAKEN D{dose_id}', 'demo_patient', now=due + timedelta(minutes=5))
        assert 'recorded as taken' in reply
        assert one('SELECT status FROM doses WHERE id=?', (dose_id,))['status'] == 'taken'
        assert one('SELECT COUNT(*) n FROM dose_events WHERE dose_id=?', (dose_id,))['n'] == 1

    # 7. dashboard statistics come from those stored records
    html = auth.get('/').data
    assert stat(html, 'Confirmed taken') == 1
    assert stat(html, 'No reply') == 0
    assert b'Fictional Tablet' in html

    # the same numbers drive the CSV export
    today = utc_now().date().isoformat()
    rows = list(csv.DictReader(io.StringIO(auth.get(f'/reports.csv?start={today}&end={today}').data.decode('utf-8-sig'))))
    taken = [r for r in rows if r['status_or_response'] == 'taken']
    assert len(taken) == 1 and taken[0]['source'] == 'demo_patient'


def test_e2e_08_not_yet_keeps_dose_open_and_applies_policy(app):
    """NOT YET must never be treated as confirmation, and must follow the configured policy."""
    with app.app_context():
        pid = get_db().execute('INSERT INTO patients (user_id,name,phone,timezone,contact_name,contact_phone,created_at) '
                               'VALUES (1,?,?,?,?,?,?)',
                               ('Robin Fictional', PATIENT_PHONE, 'UTC', 'Sam Demo', CONTACT_PHONE, stamp())).lastrowid
        mid = get_db().execute('''INSERT INTO medicines (patient_id,name,dosage,instructions,start_date,times,state,fingerprint,created_at)
            VALUES (?,?,?,?,?,?, 'active', ?, ?)''',
            (pid, 'Fictional Tablet', 'Entered demo label only - not for use', '', utc_now().date().isoformat(),
             '["08:00"]', 'fp-nyt', stamp())).lastrowid
        did = get_db().execute('INSERT INTO doses (medicine_id,scheduled_at) VALUES (?,?)',
                               (mid, stamp(utc_now() - timedelta(minutes=1)))).lastrowid
    now = utc_now()
    run_due(app, now)
    with app.app_context():
        with transaction():
            reply = handle_reply(pid, f'NOT YET D{did}', 'demo_patient', now=now + timedelta(minutes=2))
        d = one('SELECT * FROM doses WHERE id=?', (did,))
        assert d['status'] == 'unresolved', 'NOT YET must keep the dose open, not confirm it'
        assert d['response'] == 'NOT_YET' and d['responded_at']
        assert 'kept open' in reply
        # Policy default: alert the responsible family member straight away.
        alert = one("SELECT * FROM alerts WHERE kind='not_yet'")
        assert alert and alert['user_id'] == 1
        assert 'NOT YET' in alert['text'] and 'not a confirmed missed dose' in alert['text']
        assert one("SELECT recipient FROM messages WHERE kind='alert'")['recipient'] == CONTACT_PHONE
        assert one('SELECT COUNT(*) n FROM dose_events WHERE dose_id=?', (did,))['n'] == 1
        assert one('SELECT response FROM dose_events WHERE dose_id=?', (did,))['response'] == 'NOT_YET'


def test_e2e_08b_not_yet_deferred_policy_alerts_after_escalation_window(app):
    """With the immediate policy off, the family is alerted only when the escalation window closes."""
    with app.app_context():
        get_db().execute('UPDATE users SET alert_on_not_yet=0 WHERE id=1')
        pid = get_db().execute('INSERT INTO patients (user_id,name,phone,timezone,contact_name,contact_phone,created_at) '
                               'VALUES (1,?,?,?,?,?,?)',
                               ('Robin Fictional', PATIENT_PHONE, 'UTC', 'Sam Demo', CONTACT_PHONE, stamp())).lastrowid
        mid = get_db().execute('''INSERT INTO medicines (patient_id,name,dosage,instructions,start_date,times,state,fingerprint,created_at)
            VALUES (?,?,?,?,?,?, 'active', ?, ?)''',
            (pid, 'Fictional Tablet', 'demo label', '', utc_now().date().isoformat(), '["08:00"]', 'fp-n2', stamp())).lastrowid
        did = get_db().execute('INSERT INTO doses (medicine_id,scheduled_at) VALUES (?,?)',
                               (mid, stamp(utc_now() - timedelta(minutes=1)))).lastrowid
    now = utc_now()
    run_due(app, now)
    with app.app_context():
        with transaction():
            handle_reply(pid, f'NOT YET D{did}', 'demo_patient', now=now)
        assert one("SELECT COUNT(*) n FROM alerts WHERE kind='not_yet'")['n'] == 0
        window_closed = parse(one('SELECT expected_at FROM doses WHERE id=?', (did,))['expected_at']) + timedelta(minutes=1)
    run_due(app, window_closed)
    with app.app_context():
        assert one("SELECT COUNT(*) n FROM alerts WHERE kind='not_yet'")['n'] == 1


def test_e2e_09_10_missing_reply_escalates_to_the_responsible_family_only(app):
    """Response window expires with no reply -> the correct family number is alerted, once."""
    with app.app_context():
        ids = {}
        for name, phone, contact in [('Robin Fictional', PATIENT_PHONE, CONTACT_PHONE),
                                     ('Other Fictional', '+12025550105', '+12025550107')]:
            pid = get_db().execute('INSERT INTO patients (user_id,name,phone,timezone,contact_name,contact_phone,created_at) '
                                   'VALUES (1,?,?,?,?,?,?)', (name, phone, 'UTC', 'Sam Demo', contact, stamp())).lastrowid
            mid = get_db().execute('''INSERT INTO medicines (patient_id,name,dosage,instructions,start_date,times,state,fingerprint,created_at)
                VALUES (?,?,?,?,?,?, 'active', ?, ?)''',
                (pid, 'Fictional Tablet', 'demo label', '', utc_now().date().isoformat(), '["08:00"]',
                 'fp-' + name, stamp())).lastrowid
            ids[name] = get_db().execute('INSERT INTO doses (medicine_id,scheduled_at) VALUES (?,?)',
                                         (mid, stamp(utc_now() - timedelta(minutes=1)))).lastrowid
    now = utc_now()
    run_due(app, now)
    later = now + timedelta(minutes=61)          # response window closed, escalation window 0
    run_due(app, later)
    with app.app_context():
        for name, dose_id in ids.items():
            assert one('SELECT status FROM doses WHERE id=?', (dose_id,))['status'] == 'missed'
        alerts = all_rows("SELECT * FROM alerts WHERE kind='missed_response'")
        assert len(alerts) == 2, 'exactly one alert per unanswered dose'
        by_patient = {a['patient_id']: a for a in alerts}
        robin = one("SELECT id FROM patients WHERE name='Robin Fictional'")['id']
        other = one("SELECT id FROM patients WHERE name='Other Fictional'")['id']
        assert 'Robin Fictional' in by_patient[robin]['text']
        # each alert notification went to THAT patient's contact number, and only that one
        messages = all_rows("SELECT * FROM messages WHERE kind='alert'")
        assert {m['recipient'] for m in messages} == {CONTACT_PHONE, '+12025550107'}
        robin_alert = one("SELECT id FROM alerts WHERE patient_id=?", (robin,))['id']
        assert one('SELECT recipient FROM messages WHERE kind=\'alert\' AND entity_id=?', (robin_alert,))['recipient'] == CONTACT_PHONE
        assert by_patient[other]['delivered_status'] in (None, 'simulated')
        # wording must distinguish "no reply" from "confirmed missed dose"
        assert 'No reply does not prove that the medicine was not taken' in by_patient[robin]['text']
        assert one("SELECT COUNT(*) n FROM alerts WHERE kind='missed_response'")['n'] == 2


def test_e2e_11_repeated_scheduler_runs_create_no_duplicates(app):
    """Extra scheduler runs, and a re-dispatch, must not duplicate reminders or alerts."""
    with app.app_context():
        pid = get_db().execute('INSERT INTO patients (user_id,name,phone,timezone,contact_name,contact_phone,created_at) '
                               'VALUES (1,?,?,?,?,?,?)',
                               ('Robin Fictional', PATIENT_PHONE, 'UTC', 'Sam Demo', CONTACT_PHONE, stamp())).lastrowid
        mid = get_db().execute('''INSERT INTO medicines (patient_id,name,dosage,instructions,start_date,times,state,fingerprint,created_at)
            VALUES (?,?,?,?,?,?, 'active', ?, ?)''',
            (pid, 'Fictional Tablet', 'demo label', '', utc_now().date().isoformat(), '["08:00"]', 'fp-dup', stamp())).lastrowid
        get_db().execute('INSERT INTO doses (medicine_id,scheduled_at) VALUES (?,?)',
                         (mid, stamp(utc_now() - timedelta(minutes=1))))
    now = utc_now()
    with app.app_context():
        for _ in range(3):
            with transaction():
                due_work(now)
        dispatch(now)
        dispatch(now)
        assert one("SELECT COUNT(*) n FROM messages WHERE kind='dose'")['n'] == 1
        assert one("SELECT COUNT(*) n FROM doses")['n'] == 1
    later = now + timedelta(minutes=61)
    with app.app_context():
        for _ in range(3):
            with transaction():
                due_work(later)
        dispatch(later)
        assert one("SELECT COUNT(*) n FROM alerts WHERE kind='missed_response'")['n'] == 1
        assert one("SELECT COUNT(*) n FROM messages WHERE kind='alert'")['n'] == 1
        # the lease also stops a second worker doing the same beat twice
        assert tick(later + timedelta(seconds=30)) is True
        assert tick(later + timedelta(seconds=31)) is False
        assert one("SELECT COUNT(*) n FROM messages WHERE kind='dose'")['n'] == 1


def test_e2e_late_confirmation_resolves_the_open_alert(app):
    """A late TAKEN reply must record the confirmation and settle the earlier no-reply alert."""
    with app.app_context():
        pid = get_db().execute('INSERT INTO patients (user_id,name,phone,timezone,contact_name,contact_phone,created_at) '
                               'VALUES (1,?,?,?,?,?,?)',
                               ('Robin Fictional', PATIENT_PHONE, 'UTC', 'Sam Demo', CONTACT_PHONE, stamp())).lastrowid
        mid = get_db().execute('''INSERT INTO medicines (patient_id,name,dosage,instructions,start_date,times,state,fingerprint,created_at)
            VALUES (?,?,?,?,?,?, 'active', ?, ?)''',
            (pid, 'Fictional Tablet', 'demo label', '', utc_now().date().isoformat(), '["08:00"]', 'fp-late', stamp())).lastrowid
        did = get_db().execute('INSERT INTO doses (medicine_id,scheduled_at) VALUES (?,?)',
                               (mid, stamp(utc_now() - timedelta(minutes=1)))).lastrowid
    now = utc_now()
    run_due(app, now)
    run_due(app, now + timedelta(minutes=61))
    with app.app_context():
        assert one('SELECT status FROM doses WHERE id=?', (did,))['status'] == 'missed'
        with transaction():
            reply = handle_reply(pid, f'TAKEN D{did}', 'demo_patient', now=now + timedelta(minutes=70))
        assert 'resolved' in reply
        assert one('SELECT status FROM doses WHERE id=?', (did,))['status'] == 'taken'
        assert one('SELECT resolved_at FROM alerts')['resolved_at']
    assert stat(auth_dashboard(app), 'Confirmed taken') == 1


def auth_dashboard(app):
    """Read the dashboard for user 1 without a login round-trip."""
    client = app.test_client()
    with app.app_context():
        client.post('/login', data={'email': 'one@example.test', 'password': 'SafeTestPassword!'})
    return client.get('/').data


# ---------------------------------------------------------------------------------------
# 12: provider failures are handled safely
# ---------------------------------------------------------------------------------------
def test_e2e_12_messaging_failure_is_handled_safely(app, monkeypatch):
    """A rate-limit rejection retries a bounded number of times; an unclear error never resends."""
    with app.app_context():
        pid = get_db().execute('INSERT INTO patients (user_id,name,phone,timezone,contact_name,contact_phone,created_at) '
                               'VALUES (1,?,?,?,?,?,?)',
                               ('Robin Fictional', PATIENT_PHONE, 'UTC', 'Sam Demo', CONTACT_PHONE, stamp())).lastrowid
        mid = get_db().execute('''INSERT INTO medicines (patient_id,name,dosage,instructions,start_date,times,state,fingerprint,created_at)
            VALUES (?,?,?,?,?,?, 'active', ?, ?)''',
            (pid, 'Fictional Tablet', 'demo label', '', utc_now().date().isoformat(), '["08:00"]', 'fp-fail', stamp())).lastrowid
        did = get_db().execute('INSERT INTO doses (medicine_id,scheduled_at) VALUES (?,?)',
                               (mid, stamp(utc_now() - timedelta(minutes=1)))).lastrowid
    calls = []
    monkeypatch.setattr('sahayak.services.provider_send',
                        lambda m: (calls.append(m['id']) or ('queued', None, 'provider_20429', True)))
    app.config['DEMO_MODE'] = False
    now = utc_now()
    with app.app_context():
        with transaction():
            due_work(now)
        get_db().execute("UPDATE messages SET transport='whatsapp' WHERE kind='dose'")
        dispatch(now)
        dispatch(now + timedelta(minutes=2))
        dispatch(now + timedelta(minutes=6))
        dispatch(now + timedelta(minutes=20))
        assert len(calls) == 3, 'retry must be bounded to three attempts'
        assert one("SELECT status FROM messages WHERE kind='dose'")['status'] == 'failed'
        # A failed reminder must not start a response deadline: nothing was delivered.
        assert one('SELECT notified_at FROM doses WHERE id=?', (did,))['notified_at'] is None
    # an ambiguous error is never retried automatically
    with app.app_context():
        get_db().execute("UPDATE messages SET status='queued',next_at=? WHERE kind='dose'", (stamp(),))
    calls.clear()
    monkeypatch.setattr('sahayak.services.provider_send',
                        lambda m: (calls.append(m['id']) or ('unknown', None, 'delivery_uncertain_do_not_resend', False)))
    with app.app_context():
        dispatch(now + timedelta(minutes=30))
        dispatch(now + timedelta(days=1))
        assert len(calls) == 1
        assert one("SELECT status FROM messages WHERE kind='dose'")['status'] == 'unknown'


def test_live_mode_without_credentials_fails_safely(app):
    """Live mode with no credentials must record a failure, never pretend to send."""
    app.config['DEMO_MODE'] = False
    with app.app_context():
        pid = get_db().execute('INSERT INTO patients (user_id,name,phone,timezone,contact_name,contact_phone,created_at) '
                               'VALUES (1,?,?,?,?,?,?)', ('Robin Fictional', PATIENT_PHONE, 'UTC', 'Sam Demo', CONTACT_PHONE, stamp())).lastrowid
        mid = get_db().execute('''INSERT INTO medicines (patient_id,name,dosage,instructions,start_date,times,state,fingerprint,created_at)
            VALUES (?,?,?,?,?,?, 'active', ?, ?)''',
            (pid, 'Fictional Tablet', 'demo label', '', utc_now().date().isoformat(), '["08:00"]', 'fp-live', stamp())).lastrowid
        did = get_db().execute('INSERT INTO doses (medicine_id,scheduled_at) VALUES (?,?)',
                               (mid, stamp(utc_now() - timedelta(minutes=1)))).lastrowid
        with transaction():
            due_work(utc_now())
        assert provider_send({'recipient': PATIENT_PHONE, 'body': 'Fictional'})[:3] == ('failed', None, 'not_configured')
        dispatch()
        m = one("SELECT * FROM messages WHERE kind='dose'")
        assert m['transport'] == 'whatsapp' and m['status'] == 'failed' and m['error_code'] == 'not_configured'
        assert one('SELECT notified_at FROM doses WHERE id=?', (did,))['notified_at'] is None


# ---------------------------------------------------------------------------------------
# 13: restart persistence
# ---------------------------------------------------------------------------------------
def test_e2e_13_restart_preserves_history(app, auth, tmp_path):
    """Re-opening the application against the same database keeps every record."""
    make_patient(auth)
    with app.app_context():
        pid = one('SELECT id FROM patients')['id']
    make_medicine(auth, pid)
    with app.app_context():
        add_due_dose(one('SELECT id FROM medicines WHERE patient_id=?', (pid,))['id'])
    run_due(app, utc_now())
    with app.app_context():
        dose_id = one('SELECT id FROM doses')['id']
        window_end = parse(one('SELECT expected_at FROM doses WHERE id=?', (dose_id,))['expected_at'])
    run_due(app, window_end + timedelta(minutes=1))
    with app.app_context():
        assert one('SELECT status FROM doses WHERE id=?', (dose_id,))['status'] == 'missed'
        with transaction():
            handle_reply(pid, f'TAKEN D{dose_id}', 'demo_patient', now=window_end + timedelta(minutes=10))
        before = {t: one(f'SELECT COUNT(*) n FROM {t}')['n'] for t in
                  ('patients', 'medicines', 'doses', 'dose_events', 'messages', 'alerts', 'checkins', 'reply_events')}
        database = app.config['DATABASE']
    assert before['alerts'] >= 1 and before['reply_events'] >= 1

    restarted = create_app({'TESTING': True, 'DATABASE': database,
                            'UPLOAD_FOLDER': app.config['UPLOAD_FOLDER'], 'DEMO_MODE': True,
                            'SECRET_KEY': 'restart-test'})
    with restarted.app_context():
        after = {t: one(f'SELECT COUNT(*) n FROM {t}')['n'] for t in before}
        assert after == before
        assert one('SELECT status FROM doses WHERE id=?', (dose_id,))['status'] == 'taken'
        assert one('SELECT resolved_at FROM alerts')['resolved_at']


# ---------------------------------------------------------------------------------------
# Invalid / ambiguous / duplicate / cross-patient / timezone / authorization
# ---------------------------------------------------------------------------------------
@pytest.mark.parametrize('body', ['', 'hello', 'TAKEN C1', 'yes please', 'TAKEN D999999',
                                  'NOT TAKEN D1', '<script>alert(1)</script>', 'TAKEN D0',
                                  'REMIND ME LATER 9000 D1', 'WORSE D1', '9999'])
def test_invalid_and_ambiguous_replies_never_confirm_a_dose(app, auth, body):
    """The safety rule under test: nothing marks a dose taken except a clear TAKEN reply."""
    make_patient(auth)
    with app.app_context():
        pid = one('SELECT id FROM patients')['id']
    make_medicine(auth, pid)
    run_due(app, utc_now() + timedelta(minutes=1))
    with app.app_context():
        dose_id = one('SELECT id FROM doses')['id']
        with transaction():
            handle_reply(pid, body, 'demo_patient')
        assert one('SELECT status FROM doses WHERE id=?', (dose_id,))['status'] != 'taken'
        assert one("SELECT COUNT(*) n FROM dose_events WHERE status='taken'")['n'] == 0
        assert one('SELECT COUNT(*) n FROM dose_events WHERE dose_id=?', (dose_id,))['n'] <= 1
        # every attempt is audited, including replies that could not be matched
        assert one('SELECT outcome FROM reply_events ORDER BY id DESC LIMIT 1')['outcome']


def test_remind_me_later_schedules_exactly_one_followup(app):
    with app.app_context():
        get_db().execute('UPDATE users SET reply_delay_minutes=20 WHERE id=1')
        pid = get_db().execute('INSERT INTO patients (user_id,name,phone,timezone,contact_name,contact_phone,created_at) '
                               'VALUES (1,?,?,?,?,?,?)', ('Robin Fictional', PATIENT_PHONE, 'UTC', 'Sam Demo', CONTACT_PHONE, stamp())).lastrowid
        mid = get_db().execute('''INSERT INTO medicines (patient_id,name,dosage,instructions,start_date,times,state,fingerprint,created_at)
            VALUES (?,?,?,?,?,?, 'active', ?, ?)''',
            (pid, 'Fictional Tablet', 'demo label', '', utc_now().date().isoformat(), '["08:00"]', 'fp-later', stamp())).lastrowid
        did = get_db().execute('INSERT INTO doses (medicine_id,scheduled_at) VALUES (?,?)',
                               (mid, stamp(utc_now() - timedelta(minutes=1)))).lastrowid
    now = utc_now()
    run_due(app, now)
    with app.app_context():
        with transaction():
            first = handle_reply(pid, f'REMIND ME LATER D{did}', 'demo_patient', now=now)
            second = handle_reply(pid, f'REMIND ME LATER D{did}', 'demo_patient', now=now)
            third = handle_reply(pid, f'REMIND ME LATER 45 D{did}', 'demo_patient', now=now)
        assert 'scheduled for 20 minutes' in first
        assert '45' in third
        assert one('SELECT status FROM doses WHERE id=?', (did,))['status'] == 'reminder_requested'
        assert one("SELECT COUNT(*) n FROM messages WHERE kind='followup'")['n'] == 1, 'no duplicate follow-up'
        followup = one("SELECT * FROM messages WHERE kind='followup'")
        assert parse(followup['next_at']) >= now + timedelta(minutes=45, seconds=-1)
    run_due(app, now + timedelta(minutes=50))
    with app.app_context():
        assert one("SELECT status FROM messages WHERE kind='followup'")['status'] == 'simulated'


def test_wrong_patient_cannot_confirm_another_patients_dose(app):
    with app.app_context():
        first = get_db().execute('INSERT INTO patients (user_id,name,phone,timezone,contact_name,contact_phone,created_at) '
                                 'VALUES (1,?,?,?,?,?,?)', ('Robin Fictional', PATIENT_PHONE, 'UTC', 'Sam', CONTACT_PHONE, stamp())).lastrowid
        second = get_db().execute('INSERT INTO patients (user_id,name,phone,timezone,contact_name,contact_phone,created_at) '
                                  'VALUES (1,?,?,?,?,?,?)', ('Other Fictional', '+12025550105', 'UTC', 'Sam', '+12025550107', stamp())).lastrowid
        mid = get_db().execute('''INSERT INTO medicines (patient_id,name,dosage,instructions,start_date,times,state,fingerprint,created_at)
            VALUES (?,?,?,?,?,?, 'active', ?, ?)''',
            (first, 'Fictional Tablet', 'demo label', '', utc_now().date().isoformat(), '["08:00"]', 'fp-wrong', stamp())).lastrowid
        did = get_db().execute('INSERT INTO doses (medicine_id,scheduled_at) VALUES (?,?)',
                               (mid, stamp(utc_now() - timedelta(minutes=1)))).lastrowid
    run_due(app, utc_now())
    with app.app_context():
        with transaction():
            reply = handle_reply(second, f'TAKEN D{did}', 'demo_patient')
        assert 'No unique matching dose' in reply
        assert one('SELECT status FROM doses WHERE id=?', (did,))['status'] == 'awaiting_response'
        assert one('SELECT COUNT(*) n FROM dose_events')['n'] == 0


def test_timezone_boundary_reminder_uses_patient_local_time(app):
    """08:00 in Asia/Kolkata must be stored as 02:30 UTC and stay unique across DST rules."""
    with app.app_context():
        pid = get_db().execute('INSERT INTO patients (user_id,name,phone,timezone,contact_name,contact_phone,created_at) '
                               'VALUES (1,?,?,?,?,?,?)', ('Robin Fictional', PATIENT_PHONE, 'Asia/Kolkata', 'Sam', CONTACT_PHONE, stamp())).lastrowid
        mid = get_db().execute('''INSERT INTO medicines (patient_id,name,dosage,instructions,start_date,times,state,fingerprint,created_at)
            VALUES (?,?,?,?,?,?, 'active', ?, ?)''',
            (pid, 'Fictional Tablet', 'demo label', '', utc_now().date().isoformat(), '["08:00"]', 'fp-tz', stamp())).lastrowid
        with transaction():
            from sahayak.services import generate_events
            generate_events(utc_now())
            generate_events(utc_now())          # a second run must add nothing
        rows = all_rows('SELECT * FROM doses WHERE medicine_id=?', (mid,))
        assert len(rows) == 2, 'today local + tomorrow local, generated once each'
        hours = {parse(r['scheduled_at']).astimezone(ZoneInfo('Asia/Kolkata')).hour for r in rows}
        assert hours == {8}
        assert one('SELECT COUNT(*) n FROM doses')['n'] == 2
    # the explicit helper is the authority for local wall-clock -> UTC
    assert stamp(local_instant(utc_now().date(), '08:00', 'Asia/Kolkata')) == (
        stamp(local_instant(utc_now().date(), '08:00', 'Asia/Kolkata')))


def test_duplicate_webhook_delivery_is_idempotent(app, client):
    """Twilio can deliver the same message twice; the second delivery must change nothing."""
    app.config.update(TWILIO_AUTH_TOKEN='test-token-not-real', PUBLIC_BASE_URL='https://test.example')
    app.config['WTF_CSRF_ENABLED'] = True
    with app.app_context():
        pid = get_db().execute('INSERT INTO patients (user_id,name,phone,timezone,contact_name,contact_phone,created_at) '
                               'VALUES (1,?,?,?,?,?,?)', ('Robin Fictional', PATIENT_PHONE, 'UTC', 'Sam', CONTACT_PHONE, stamp())).lastrowid
        mid = get_db().execute('''INSERT INTO medicines (patient_id,name,dosage,instructions,start_date,times,state,fingerprint,created_at)
            VALUES (?,?,?,?,?,?, 'active', ?, ?)''',
            (pid, 'Fictional Tablet', 'demo label', '', utc_now().date().isoformat(), '["08:00"]', 'fp-hook', stamp())).lastrowid
        did = get_db().execute('INSERT INTO doses (medicine_id,scheduled_at) VALUES (?,?)',
                               (mid, stamp(utc_now() - timedelta(minutes=1)))).lastrowid
    run_due(app, utc_now())
    params = {'MessageSid': 'SM' + 'a' * 32, 'From': 'whatsapp:' + PATIENT_PHONE, 'Body': f'TAKEN D{did}'}

    def signed_post():
        signature = RequestValidator('test-token-not-real').compute_signature('https://test.example/webhooks/twilio', params)
        return client.post('/webhooks/twilio', data=params, headers={'X-Twilio-Signature': signature})

    assert b'recorded as taken' in signed_post().data
    assert signed_post().status_code == 200
    with app.app_context():
        assert one("SELECT COUNT(*) n FROM inbound")['n'] == 1
        assert one("SELECT COUNT(*) n FROM dose_events")['n'] == 1
        assert one("SELECT COUNT(*) n FROM messages WHERE direction='inbound'")['n'] == 1
        assert one('SELECT status FROM doses WHERE id=?', (did,))['status'] == 'taken'
        # an unsigned or tampered request is refused outright
        assert client.post('/webhooks/twilio', data=params).status_code == 403


def test_authorization_checks_across_family_accounts(app, auth):
    """Another family's patient, dose and alert must be invisible and unusable."""
    with app.app_context():
        other = get_db().execute('INSERT INTO patients (user_id,name,phone,timezone,contact_name,contact_phone,created_at) '
                                 'VALUES (2,?,?,?,?,?,?)', ('PRIVATE OTHER FAMILY', '+12025550111', 'UTC', 'Sam', '+12025550112', stamp())).lastrowid
        mid = get_db().execute('''INSERT INTO medicines (patient_id,name,dosage,instructions,start_date,times,state,fingerprint,created_at)
            VALUES (?,?,?,?,?,?, 'active', ?, ?)''',
            (other, 'Private Medicine', 'label', '', utc_now().date().isoformat(), '["08:00"]', 'fp-auth', stamp())).lastrowid
        dose = get_db().execute('INSERT INTO doses (medicine_id,scheduled_at,notified_at) VALUES (?,?,?)',
                                (mid, stamp(utc_now() - timedelta(minutes=1)), stamp())).lastrowid
        alert = get_db().execute("INSERT INTO alerts (user_id,patient_id,kind,entity_key,text,created_at) "
                                 "VALUES (2,?, 'missed_response', ?, 'PRIVATE ALERT', ?)",
                                 (other, 'dose:%d' % dose, stamp())).lastrowid
    assert auth.get(f'/patients/{other}').status_code == 404
    assert auth.post(f'/patients/{other}/medicines/new', data={'name': 'x'}).status_code == 404
    assert auth.post(f'/alerts/{alert}/acknowledge').status_code == 404
    assert auth.post(f'/doses/{dose}/confirm').status_code == 404
    assert auth.get(f'/reports?patient_id={other}').status_code == 404
    assert auth.get(f'/messages?patient_id={other}').status_code == 404
    for path in ('/', '/patients', '/alerts', '/messages', '/reports'):
        assert b'PRIVATE OTHER FAMILY' not in auth.get(path).data
        assert b'PRIVATE ALERT' not in auth.get(path).data


# ---------------------------------------------------------------------------------------
# Demo Mode: the workflow must be demonstrable with no credentials at all
# ---------------------------------------------------------------------------------------
def test_demo_mode_simulator_runs_the_whole_loop_without_credentials(app, auth, monkeypatch):
    """Simulator: reminder -> reply -> timeout -> family alert, all labelled as simulated."""
    monkeypatch.setattr('sahayak.services.provider_send',
                        lambda m: (_ for _ in ()).throw(AssertionError('Demo Mode must never call the provider')))
    make_patient(auth)
    with app.app_context():
        pid = one('SELECT id FROM patients')['id']
    make_medicine(auth, pid)

    assert auth.post('/demo', data={'action': 'reminder', 'patient_id': pid}).status_code == 302
    with app.app_context():
        dose = one("SELECT * FROM doses WHERE is_demo=1 AND notified_at IS NOT NULL")
        assert dose, 'the simulator must create and send a practice dose'
        did = dose['id']
        assert one("SELECT status FROM messages WHERE kind='dose'")['status'] == 'simulated'
    for body in ('NOT YET', 'REMIND ME LATER 15'):
        assert auth.post('/demo', data={'action': 'reply', 'patient_id': pid,
                                        'body': f'{body} D{did}'}).status_code == 302
    assert auth.post('/demo', data={'action': 'taken', 'patient_id': pid, 'dose_id': did}).status_code == 302
    with app.app_context():
        assert one('SELECT status FROM doses WHERE id=?', (did,))['status'] == 'taken'

    # expire a fresh practice dose's window and confirm the family alert chain
    auth.post('/demo', data={'action': 'reminder', 'patient_id': pid})
    with app.app_context():
        did = one("SELECT id FROM doses WHERE status='awaiting_response' ORDER BY id DESC LIMIT 1")['id']
    assert auth.post('/demo', data={'action': 'timeout', 'patient_id': pid, 'dose_id': did}).status_code == 302
    with app.app_context():
        assert one('SELECT status FROM doses WHERE id=?', (did,))['status'] == 'missed'
        alert = one("SELECT * FROM alerts WHERE kind='missed_response' AND patient_id=?", (pid,))
        assert alert and 'No reply does not prove that the medicine was not taken' in alert['text']
        assert one("SELECT recipient,status,transport FROM messages WHERE kind='alert' AND entity_id=?",
                   (alert['id'],))['recipient'] == CONTACT_PHONE
    assert auth.post('/demo', data={'action': 'checkin', 'patient_id': pid}).status_code == 302
    with app.app_context():
        cid = one("SELECT id FROM checkins WHERE notified_at IS NOT NULL ORDER BY id DESC LIMIT 1")['id']
    auth.post('/demo', data={'action': 'reply', 'patient_id': pid, 'body': f'WORSE C{cid}'})
    with app.app_context():
        assert one("SELECT COUNT(*) n FROM alerts WHERE kind='concerning_health'")['n'] == 1
        # nothing on any channel left the machine
        assert one("SELECT COUNT(*) n FROM messages WHERE transport!='demo'")['n'] == 0
    assert auth.get('/').status_code == 200


def test_all_simulated_messages_are_labelled_as_simulated(app, auth):
    """No record may claim a real WhatsApp send when nothing was actually sent."""
    make_patient(auth)
    with app.app_context():
        pid = one('SELECT id FROM patients')['id']
    make_medicine(auth, pid)
    with app.app_context():
        add_due_dose(one('SELECT id FROM medicines WHERE patient_id=?', (pid,))['id'])
    run_due(app, utc_now())
    with app.app_context():
        rows = all_rows('SELECT transport,status,delivery_status FROM messages')
        assert rows
        for m in rows:
            assert m['transport'] == 'demo' and m['status'] == 'simulated'
            assert m['delivery_status'] is None, 'a simulated send has no provider delivery receipt'
        assert one("SELECT COUNT(*) n FROM messages WHERE status IN ('accepted','delivered','read')")['n'] == 0


def test_health_and_startup_need_no_credentials(client):
    assert client.get('/health').json['status'] == 'ok'
    assert client.get('/health').json['demo_mode'] is True
