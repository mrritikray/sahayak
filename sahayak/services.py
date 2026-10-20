"""Care orchestration: schedule generation, outbox, delivery, replies, family alerts.

Every outbox row has a globally unique dedupe_key, so re-running the scheduler or
re-delivering a Twilio webhook can never produce a duplicate reminder or alert.
"""
import json
from datetime import timedelta
from zoneinfo import ZoneInfo

from flask import current_app
from twilio.base.exceptions import TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client

from . import messages as message_text
from . import alerts as alert_policy
from .db import all_rows, get_db, one, transaction
from .providers import (DELIVERED_STATES, provider_ready, resolve_transport,
                        simulated_state, webhook_ready)
from .replies import DELAY_HIGH, DELAY_LOW, HELP_TEXT, parse_reply
from .validation import local_instant, parse, stamp, utc_now

# A dose stays open for a reply while it is in one of these states.
OPEN_DOSE_STATUSES = ('pending', 'awaiting_response', 'reminder_requested', 'unresolved')


# --------------------------------------------------------------------------------------
# Outbox
# --------------------------------------------------------------------------------------
def queue(user, patient, key, kind, entity_id, recipient, body, now, family=False):
    """Insert one outbox row. The unique dedupe_key makes repeats a no-op."""
    transport = resolve_transport(current_app.config['DEMO_MODE'], user['notifications'], family)
    get_db().execute('''INSERT OR IGNORE INTO messages
      (user_id,patient_id,dedupe_key,kind,entity_id,recipient,body,transport,next_at,created_at)
      VALUES (?,?,?,?,?,?,?,?,?,?)''',
      (user['id'], patient['id'], key, kind, entity_id, recipient, body, transport,
       stamp(now), stamp(now)))


def create_alert(user, patient, kind, key, message, now, reason=None):
    """Create the family alert once, then queue exactly one outbound notification."""
    db = get_db()
    cur = db.execute('''INSERT OR IGNORE INTO alerts
      (user_id,patient_id,kind,entity_key,text,reason,created_at) VALUES (?,?,?,?,?,?,?)''',
      (user['id'], patient['id'], kind, key, message, reason or alert_policy.LABELS.get(kind, ''), stamp(now)))
    if not cur.rowcount:
        return one('SELECT * FROM alerts WHERE entity_key=?', (key,))
    alert_id = cur.lastrowid
    # The responsible family contact for THIS patient is always the recipient. When no number
    # is on file the notification is still recorded, so the failure is visible rather than silent.
    recipient = alert_policy.recipient_for(user, patient)
    queue(user, patient, 'alert:' + key, 'alert', alert_id, recipient, message, now, family=True)
    return one('SELECT * FROM alerts WHERE id=?', (alert_id,))


def patient_user(patient_id):
    patient = one('SELECT * FROM patients WHERE id=?', (patient_id,))
    return patient, one('SELECT * FROM users WHERE id=?', (patient['user_id'],))


# --------------------------------------------------------------------------------------
# Schedule generation
# --------------------------------------------------------------------------------------
def generate_events(now=None, user_id=None):
    """Called inside a write transaction. Generates current and next local day's events."""
    now = now or utc_now()
    patients = all_rows('SELECT * FROM patients WHERE active=1' + (' AND user_id=?' if user_id else ''),
                        (user_id,) if user_id else ())
    db = get_db()
    for p in patients:
        u = one('SELECT * FROM users WHERE id=?', (p['user_id'],))
        today = now.astimezone(ZoneInfo(p['timezone'])).date()
        for offset in (0, 1):
            day = today + timedelta(days=offset)
            for m in all_rows("SELECT * FROM medicines WHERE patient_id=? AND state='active'", (p['id'],)):
                if day.isoformat() < m['start_date'] or (m['end_date'] and day.isoformat() > m['end_date']):
                    continue
                for at in json.loads(m['times']):
                    db.execute('''INSERT OR IGNORE INTO doses
                      (medicine_id,scheduled_at,medicine_name,dosage_text,instruction_text) VALUES (?,?,?,?,?)''',
                      (m['id'], stamp(local_instant(day, at, p['timezone'])), m['name'], m['dosage'], m['instructions']))
            db.execute('INSERT OR IGNORE INTO checkins (patient_id,local_date,scheduled_at) VALUES (?,?,?)',
                       (p['id'], day.isoformat(), stamp(local_instant(day, u['checkin_time'], p['timezone']))))


# --------------------------------------------------------------------------------------
# Scheduler steps
# --------------------------------------------------------------------------------------
def _queue_due_reminders(now):
    """Step 1: the dose is due -> queue the medicine reminder for the patient's number."""
    for d in all_rows('''SELECT d.*,m.patient_id,m.name,m.dosage,m.instructions FROM doses d
        JOIN medicines m ON m.id=d.medicine_id JOIN patients p ON p.id=m.patient_id
        WHERE d.scheduled_at<=? AND d.status IN ('pending','reminder_requested') AND d.notified_at IS NULL
        AND COALESCE(d.source,'')!='schedule_changed_no_reminder' AND m.state='active' AND p.active=1''',
        (stamp(now),)):
        p, u = patient_user(d['patient_id'])
        queue(u, p, f'dose:{d["id"]}:0', 'dose', d['id'], p['phone'], message_text.dose_reminder(p, d), now)


def _queue_checkins(now):
    for c in all_rows('''SELECT c.* FROM checkins c JOIN patients p ON p.id=c.patient_id
        WHERE c.scheduled_at<=? AND c.notified_at IS NULL AND c.response IS NULL
        AND COALESCE(c.source,'')!='schedule_changed_no_reminder' AND p.active=1''', (stamp(now),)):
        p, u = patient_user(c['patient_id'])
        queue(u, p, f'checkin:{c["id"]}', 'checkin', c['id'], p['phone'],
              message_text.checkin_question(p, c), now)


def _expire_response_windows(now):
    """Step 2: the response window closed with no reply.

    The dose becomes 'missed' (meaning: no confirmation was received - it is never described
    as a missed dose), the patient optionally gets one follow-up, and the responsible family
    member is alerted either now (escalation window 0) or when that window closes.
    """
    db = get_db()
    rows = all_rows('''SELECT d.*,m.patient_id,m.name,m.dosage,m.instructions,m.state,p.active AS patient_active
        FROM doses d JOIN medicines m ON m.id=d.medicine_id JOIN patients p ON p.id=m.patient_id
        WHERE d.expected_at<=? AND d.notified_at IS NOT NULL AND d.status IN
        ('awaiting_response','pending','reminder_requested','missed') AND p.active=1''', (stamp(now),))
    for d in rows:
        p, u = patient_user(d['patient_id'])
        if d['status'] != 'missed':
            db.execute("UPDATE doses SET status='missed',missed_at=? WHERE id=?", (stamp(now), d['id']))
            db.execute('''INSERT INTO dose_events (dose_id,status,response,source,created_at) VALUES (?,?,?,?,?)''',
                       (d['id'], 'missed', 'No confirmation received within the response window',
                        'response_window_elapsed', stamp(now)))
            if u['missed_followup']:
                queue(u, p, f'dose:{d["id"]}:missed', 'missed_followup', d['id'], p['phone'],
                      message_text.missed_followup(p, d), now)
        alert_due = parse(d['expected_at']) + timedelta(minutes=u['escalation_minutes'])
        if now >= alert_due and not d['escalated_at']:
            db.execute('UPDATE doses SET escalated_at=? WHERE id=?', (stamp(now), d['id']))
            create_alert(u, p, alert_policy.MISSED_RESPONSE, alert_policy.alert_key(alert_policy.MISSED_RESPONSE, d['id']),
                         message_text.family_missed_alert(p, d), now)
    # A patient who replied NOT YET left the dose unresolved: escalate it once, if the
    # account is configured to wait instead of alerting at reply time.
    unresolved = all_rows('''SELECT d.*,m.patient_id,m.name,m.dosage,m.instructions FROM doses d
        JOIN medicines m ON m.id=d.medicine_id JOIN patients p ON p.id=m.patient_id
        WHERE d.status='unresolved' AND d.expected_at IS NOT NULL AND p.active=1''')
    for d in unresolved:
        p, u = patient_user(d['patient_id'])
        if alert_policy.should_alert_on_not_yet(u):
            continue
        if now >= parse(d['expected_at']) + timedelta(minutes=u['escalation_minutes']):
            create_alert(u, p, alert_policy.NOT_YET, alert_policy.alert_key(alert_policy.NOT_YET, d['id']),
                         message_text.family_not_yet_alert(p, d), now)


    # A check-in whose reply window closed without an answer is also a family alert.
    for c in all_rows('''SELECT c.* FROM checkins c JOIN patients p ON p.id=c.patient_id
        WHERE c.expected_at<=? AND c.notified_at IS NOT NULL AND c.response IS NULL AND p.active=1''',
        (stamp(now),)):
        p, u = patient_user(c['patient_id'])
        create_alert(u, p, alert_policy.MISSED_CHECKIN, alert_policy.alert_key(alert_policy.MISSED_CHECKIN, c['id']),
                     message_text.family_checkin_alert(p, c), now)


def _queue_automatic_followups(now):
    """Optional bounded follow-ups to the patient while the response window is still open."""
    for d in all_rows('''SELECT d.*,m.patient_id,m.name,m.dosage,m.instructions FROM doses d
        JOIN medicines m ON m.id=d.medicine_id JOIN patients p ON p.id=m.patient_id
        WHERE d.notified_at IS NOT NULL AND d.expected_at>? AND d.status IN
        ('awaiting_response','pending','reminder_requested') AND p.active=1 AND m.state='active' ''',
        (stamp(now),)):
        p, u = patient_user(d['patient_id'])
        for n in range(1, u['repeat_limit'] + 1):
            due = parse(d['notified_at']) + timedelta(minutes=u['repeat_minutes'] * n)
            if now >= due:
                queue(u, p, f'dose:{d["id"]}:{n}', 'followup', d['id'], p['phone'],
                      message_text.missed_followup(p, d), now)


def due_work(now=None):
    """One read/write pass over due work. Must be called inside a write transaction."""
    now = now or utc_now()
    _queue_due_reminders(now)
    _queue_checkins(now)
    _expire_response_windows(now)
    _queue_automatic_followups(now)


# --------------------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------------------
def provider_send(message):
    """Real WhatsApp delivery through Twilio. Returns (state, provider_sid, error, retry_safe).

    Nothing here is called in Demo Mode, and the application starts without credentials.
    """
    config = current_app.config
    if not provider_ready(config):
        return 'failed', None, 'not_configured', False
    if not message['recipient']:
        return 'failed', None, 'missing_recipient', False
    try:
        client = Client(config['TWILIO_ACCOUNT_SID'].strip(), config['TWILIO_AUTH_TOKEN'].strip(),
                        http_client=TwilioHttpClient(timeout=10))
        request_args = dict(
            from_=config['TWILIO_WHATSAPP_FROM'].strip(),
            to='whatsapp:' + message['recipient'], body=message['body'],
        )
        if webhook_ready(config):
            request_args['status_callback'] = config['PUBLIC_BASE_URL'].strip().rstrip('/') + '/webhooks/twilio/status'
        result = client.messages.create(**request_args)
        return 'accepted', result.sid, None, False
    except TwilioRestException as exc:
        # Explicit rate-limit rejection is safe to retry. Ambiguous errors need human review.
        current_app.logger.warning(
            'Twilio message request failed (HTTP status %s, error code %s).',
            exc.status if exc.status is not None else 'unknown',
            exc.code if exc.code is not None else 'unknown')
        return ('queued' if exc.status == 429 else 'failed'), None, 'provider_' + str(exc.code or exc.status or 'unknown'), exc.status == 429
    except Exception as exc:
        current_app.logger.error(
            'Twilio message request failed unexpectedly (%s); details omitted for privacy.',
            type(exc).__name__)
        return 'unknown', None, 'delivery_uncertain_do_not_resend', False


def _still_relevant(msg):
    """Guard against sending a reminder for a dose the patient already answered."""
    db = get_db()
    if msg['kind'] in ('dose', 'followup', 'missed_followup'):
        row = one('''SELECT d.status,d.source,m.state,p.active FROM doses d JOIN medicines m ON m.id=d.medicine_id
            JOIN patients p ON p.id=m.patient_id WHERE d.id=?''', (msg['entity_id'],))
        if not row:
            return False
        if not row['active'] or row['state'] != 'active' or row['source'] == 'schedule_changed_no_reminder':
            return False
        if msg['kind'] == 'missed_followup':
            return row['status'] == 'missed'
        return row['status'] in OPEN_DOSE_STATUSES or (msg['kind'] == 'followup' and row['status'] == 'missed')
    if msg['kind'] == 'checkin':
        checkin = one('SELECT response FROM checkins WHERE id=?', (msg['entity_id'],))
        return bool(checkin) and not checkin['response']
    if msg['kind'] == 'alert':
        alert = one('SELECT resolved_at FROM alerts WHERE id=?', (msg['entity_id'],))
        return bool(alert) and not alert['resolved_at']
    if msg['patient_id'] and msg['kind'] != 'alert':
        return bool(one('SELECT id FROM patients WHERE id=? AND active=1', (msg['patient_id'],)))
    return True


def _after_delivery(msg, state, now):
    """Record what a delivery attempt means for the dose, check-in or family alert."""
    db = get_db()
    if state in DELIVERED_STATES and msg['kind'] in ('dose', 'checkin'):
        p, u = patient_user(msg['patient_id'])
        table = 'doses' if msg['kind'] == 'dose' else 'checkins'
        db.execute(f'''UPDATE {table} SET notified_at=COALESCE(notified_at,?),expected_at=COALESCE(expected_at,?)
            WHERE id=?''', (stamp(now), stamp(now + timedelta(minutes=u['response_minutes'])), msg['entity_id']))
        if msg['kind'] == 'dose':
            # The dose is now waiting for a reply, which is not the same as "pending".
            db.execute("UPDATE doses SET status='awaiting_response' WHERE id=? AND status='pending'", (msg['entity_id'],))
    elif msg['kind'] == 'missed_followup' and state in DELIVERED_STATES:
        db.execute('UPDATE doses SET missed_followup_at=COALESCE(missed_followup_at,?) WHERE id=?',
                   (stamp(now), msg['entity_id']))
    if msg['kind'] == 'alert':
        alert = one('SELECT * FROM alerts WHERE id=?', (msg['entity_id'],))
        if alert:
            db.execute('''UPDATE alerts SET delivered_status=?,attempts=attempts+1,
                notified_at=COALESCE(notified_at,?) WHERE id=?''',
                (state, stamp(now) if state in DELIVERED_STATES else None, alert['id']))
            if alert['entity_key'].startswith('dose:') and state in DELIVERED_STATES:
                try:
                    dose_id = int(alert['entity_key'].split(':')[1])
                except (IndexError, ValueError):
                    dose_id = None
                if dose_id:
                    db.execute('UPDATE doses SET escalated_at=COALESCE(escalated_at,?) WHERE id=?',
                               (stamp(now), dose_id))


def dispatch(now=None):
    """Claim under the SQLite write lock, release before network IO; one delivery per row.

    A crashed or uncertain send is never retried automatically, because the provider gives no
    exactly-once guarantee and a blind resend could duplicate a medicine reminder.
    """
    now = now or utc_now()
    db = get_db()
    with transaction():
        db.execute("UPDATE messages SET status='unknown',error_code='worker_interrupted_review_required' "
                   "WHERE status='sending' AND locked_at<?", (stamp(now - timedelta(minutes=2)),))
    for _ in range(100):
        with transaction():
            msg = one("SELECT * FROM messages WHERE status='queued' AND next_at<=? ORDER BY id LIMIT 1", (stamp(now),))
            if not msg:
                return
            if not _still_relevant(msg):
                db.execute("UPDATE messages SET status='cancelled' WHERE id=?", (msg['id'],))
                if msg['kind'] == 'alert':
                    db.execute("UPDATE alerts SET delivered_status='cancelled' WHERE id=?", (msg['entity_id'],))
                continue
            if msg['patient_id']:
                patient, user = patient_user(msg['patient_id'])
                if msg['kind'] == 'alert':
                    recipient = alert_policy.recipient_for(user, patient)
                else:
                    recipient = patient['phone']
                db.execute('UPDATE messages SET recipient=? WHERE id=?', (recipient, msg['id']))
                msg = one('SELECT * FROM messages WHERE id=?', (msg['id'],))
            db.execute("UPDATE messages SET status='sending',attempts=attempts+1,locked_at=? WHERE id=?",
                       (stamp(now), msg['id']))
        if msg['transport'] in ('demo', 'local'):
            state, sid, error, retry = simulated_state(msg['transport']), None, None, False
        else:
            state, sid, error, retry = provider_send(msg)
        attempts = msg['attempts'] + 1
        if retry and attempts >= 3:
            state = 'failed'
        with transaction():
            db.execute('UPDATE messages SET status=?,provider_sid=?,error_code=?,next_at=?,sent_at=? WHERE id=?',
                       (state, sid, error, stamp(now + timedelta(seconds=30 * 2 ** attempts)),
                        stamp(now) if state in DELIVERED_STATES else None, msg['id']))
            db.execute('''INSERT OR IGNORE INTO message_attempts
                (message_id,attempt,status,error_code,provider_sid,created_at) VALUES (?,?,?,?,?,?)''',
                (msg['id'],attempts,state,error,sid,stamp(now)))
            _after_delivery(msg, state, now)
            if sid:
                from .callbacks import apply_callbacks
                apply_callbacks(sid)


def tick(now=None):
    """One scheduler beat. The database lease means only one worker does the work."""
    now = now or utc_now()
    db = get_db()
    with transaction():
        lease = one("SELECT * FROM leases WHERE name='scheduler'")
        if lease and lease['expires_at'] > stamp(now):
            return False
        db.execute("INSERT OR REPLACE INTO leases VALUES ('scheduler',?)", (stamp(now + timedelta(seconds=12)),))
        generate_events(now)
        due_work(now)
    dispatch(now)
    return True


# --------------------------------------------------------------------------------------
# Patient replies
# --------------------------------------------------------------------------------------
def _audit(patient, body, outcome, source, now, dose_id=None):
    get_db().execute('''INSERT INTO reply_events (patient_id,dose_id,body,outcome,source,created_at)
        VALUES (?,?,?,?,?,?)''', (patient['id'], dose_id, (body or '')[:500], outcome, source, stamp(now)))


def handle_reply(patient_id, body, source='whatsapp', now=None):
    """Match a patient reply to the right dose. Requires an existing write transaction.

    A dose is only ever marked taken by an explicit confirmation. Invalid, ambiguous,
    duplicate, late and cross-patient replies are recorded and answered safely.
    """
    now = now or utc_now()
    p, u = patient_user(patient_id)
    if not p['active']:
        _audit(p, body, 'inactive_profile', source, now)
        return 'This care profile is inactive. Please contact your family.'
    parsed = parse_reply(body)
    if parsed.get('error') == 'conflict':
        _audit(p, body, 'conflicting_code', source, now)
        return HELP_TEXT
    if parsed.get('error'):
        _audit(p, body, 'unrecognised', source, now)
        return HELP_TEXT
    if parsed['category'] == 'checkin':
        return _checkin_reply(p, u, parsed, source, now, body)
    return _dose_reply(p, u, parsed, source, now, body)


def _dose_reply(p, u, parsed, source, now, body):
    db = get_db()
    action, code = parsed['action'], parsed['code']
    if code:
        d = one('''SELECT d.*,m.patient_id,m.name,m.dosage,m.instructions FROM doses d
            JOIN medicines m ON m.id=d.medicine_id WHERE d.id=? AND m.patient_id=? AND d.scheduled_at<=?''',
            (code, p['id'], stamp(now)))
        if not d:
            owner = one('''SELECT m.patient_id FROM doses d JOIN medicines m ON m.id=d.medicine_id WHERE d.id=?''', (code,))
            if owner and owner['patient_id'] != p['id']:
                _audit(p, body, 'wrong_patient_code', source, now)
                return ('No unique matching dose. That dose code belongs to another care profile, '
                        'so nothing was recorded. Please use the code from your own reminder.')
            future = one('''SELECT d.id FROM doses d JOIN medicines m ON m.id=d.medicine_id
                WHERE d.id=? AND m.patient_id=? AND d.scheduled_at>?''', (code, p['id'], stamp(now)))
            _audit(p, body, 'future_dose' if future else 'unknown_dose', source, now)
            if future:
                return 'Future doses cannot be confirmed early. Please reply after the reminder arrives.'
            return 'No unique matching dose. Please include the D code from your reminder.'
    else:
        candidates = all_rows('''SELECT d.*,m.patient_id,m.name,m.dosage,m.instructions FROM doses d
            JOIN medicines m ON m.id=d.medicine_id WHERE m.patient_id=? AND d.notified_at IS NOT NULL
            AND d.status IN ('pending','awaiting_response','reminder_requested','unresolved','missed')
            AND d.scheduled_at<=?''', (p['id'], stamp(now)))
        d = candidates[0] if len(candidates) == 1 else None
        if not d:
            _audit(p, body, 'ambiguous' if candidates else 'no_open_dose', source, now)
            return 'No unique matching dose. Please include the D code from your reminder.'

    identified = 'D%d' % d['id']
    if d['status'] == 'taken':
        _audit(p, body, 'duplicate_confirmation', source, now, d['id'])
        return f'Dose {identified} is already recorded as taken. No duplicate record was added.'

    if action == 'TAKEN':
        late = d['status'] == 'missed'
        db.execute('UPDATE doses SET status=?,response=?,responded_at=?,source=? WHERE id=?',
                   ('taken', 'TAKEN', stamp(now), source, d['id']))
        db.execute('INSERT INTO dose_events (dose_id,status,response,source,created_at) VALUES (?,?,?,?,?)',
                   (d['id'], 'taken', 'TAKEN', source, stamp(now)))
        # A late confirmation settles the open no-reply alert: it is no longer unresolved.
        db.execute('''UPDATE alerts SET resolved_at=COALESCE(resolved_at,?),reason=COALESCE(reason,'') || ' (resolved by TAKEN confirmation)'
            WHERE entity_key IN (?,?) AND resolved_at IS NULL''',
                   (stamp(now), f'dose:{d["id"]}', f'dose:{d["id"]}:not_yet'))
        _audit(p, body, 'taken' + ('_late' if late else ''), source, now, d['id'])
        extra = (' The earlier no-reply alert is now marked resolved by this confirmation.' if late else '')
        return f'Thank you. Dose {identified} recorded as taken.{extra}'

    if action == 'NOT_YET':
        db.execute('UPDATE doses SET status=?,response=?,responded_at=?,source=? WHERE id=?',
                   ('unresolved', 'NOT_YET', stamp(now), source, d['id']))
        db.execute('INSERT INTO dose_events (dose_id,status,response,source,created_at) VALUES (?,?,?,?,?)',
                   (d['id'], 'unresolved', 'NOT_YET', source, stamp(now)))
        alerted = ''
        if alert_policy.should_alert_on_not_yet(u):
            create_alert(u, p, alert_policy.NOT_YET, alert_policy.alert_key(alert_policy.NOT_YET, d['id']),
                         message_text.family_not_yet_alert(p, d), now)
            alerted = ' A family notification has been queued for this unresolved dose.'
        _audit(p, body, 'not_yet', source, now, d['id'])
        return (f'Thank you. Dose {identified} recorded as not taken yet and kept open. '
                f'Please reply TAKEN once you have taken it.{alerted}')

    # REMIND ME LATER
    requested = one('SELECT status FROM messages WHERE dedupe_key=?', (f'dose:{d["id"]}:requested',))
    if requested and requested['status'] != 'queued':
        _audit(p, body, 'followup_already_processed', source, now, d['id'])
        return (f'The one requested follow-up for {identified} has already been processed. '
                'No additional reminder was scheduled. Please reply TAKEN only if taken, '
                'or NOT YET and ask your family for help.')
    if d['status'] == 'missed' and not d['missed_followup_at']:
        db.execute('UPDATE doses SET missed_followup_at=COALESCE(missed_followup_at,?) WHERE id=?',
                   (stamp(now), d['id']))
    delay = parsed['delay'] or u['reply_delay_minutes']
    queue(u, p, f'dose:{d["id"]}:requested', 'followup', d['id'], p['phone'],
          message_text.requested_reminder(p, d, delay), now + timedelta(minutes=delay))
    # Exactly one follow-up per dose: a repeated request moves that single reminder instead of
    # creating a second message.
    db.execute("UPDATE messages SET next_at=?,body=? WHERE dedupe_key=? AND status='queued'",
               (stamp(now + timedelta(minutes=delay)), message_text.requested_reminder(p, d, delay),
                f'dose:{d["id"]}:requested'))
    deadline = now + timedelta(minutes=delay + u['response_minutes'])
    db.execute('UPDATE doses SET status=?,response=?,responded_at=?,source=?,expected_at=? WHERE id=?',
               ('reminder_requested', 'REMIND_LATER', stamp(now), source,
                max(d['expected_at'] or '', stamp(deadline)), d['id']))
    db.execute('INSERT INTO dose_events (dose_id,status,response,source,created_at) VALUES (?,?,?,?,?)',
               (d['id'], 'reminder_requested', 'REMIND_LATER', source, stamp(now)))
    note = (f' Please use a delay between {DELAY_LOW} and {DELAY_HIGH} minutes.'
            if parsed['delay_invalid'] else '')
    _audit(p, body, 'remind_later', source, now, d['id'])
    return (f'Thank you. Dose {identified} recorded as reminder requested. '
            f'One follow-up reminder was scheduled for {delay} minutes from now.{note}')


def _checkin_reply(p, u, parsed, source, now, body):
    db = get_db()
    action, code = parsed['action'], parsed['code']
    if code:
        c = one('SELECT * FROM checkins WHERE id=? AND patient_id=? AND scheduled_at<=?',
                (code, p['id'], stamp(now)))
    else:
        candidates = all_rows('SELECT * FROM checkins WHERE patient_id=? AND response IS NULL '
                              'AND notified_at IS NOT NULL ORDER BY id DESC', (p['id'],))
        c = candidates[0] if len(candidates) == 1 else None
    if not c:
        _audit(p, body, 'no_matching_checkin', source, now)
        return 'No unique matching check-in. Please include the C code from your check-in message.'
    if c['response']:
        _audit(p, body, 'duplicate_checkin', source, now)
        return f'Check-in C{c["id"]} was already recorded. Please contact your family for changes.'
    # Stored the way the patient wrote it, so history reads naturally.
    label = 'NEED HELP' if action == 'NEED_HELP' else action
    db.execute('UPDATE checkins SET response=?,responded_at=?,source=? WHERE id=?',
               (label, stamp(now), source, c['id']))
    _audit(p, body, 'checkin_' + action.lower(), source, now)
    if action in ('WORSE', 'NEED_HELP'):
        create_alert(u, p, alert_policy.CONCERNING_HEALTH, alert_policy.alert_key(alert_policy.CONCERNING_HEALTH, c['id']),
                     message_text.family_concerning_alert(p, c, label), now)
        return ('Thank you. Your well-being reply was recorded. Your family notification has been '
                'queued. For urgent help, contact your local emergency number now.')
    return 'Thank you. Your well-being reply was recorded.'
