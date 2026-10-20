"""Safety/edge regression tests found during review of the existing project."""
from datetime import timedelta, datetime, timezone
import sqlite3
from pathlib import Path
import pytest
from sahayak import create_app
from sahayak.db import get_db, one, all_rows, transaction
from sahayak.services import due_work, dispatch, handle_reply
from sahayak.validation import utc_now, stamp, parse
from sahayak.replies import parse_reply

@pytest.mark.parametrize('body', [
    'I have not taken it', 'TAKEN NOT YET D1', 'TAKEN D1 D2',
    'NOT TAKEN TAKEN', 'yes TAKEN maybe', 'TAKEN D0', 'TAKEN D123456789',
    'do not mark TAKEN', 'NOT YET TAKEN D1', 'TAKEN C1', 'TAKEN D1 C1'])
def test_conflicting_or_negated_text_is_never_confirmation(body):
    assert parse_reply(body).get('error')

@pytest.mark.parametrize('body,action,delay', [
    ('TAKEN D23','TAKEN',None), ('not yet d23','NOT_YET',None),
    ('NOT TAKEN D23','NOT_YET',None), ('REMIND ME LATER D23','LATER',None),
    ('REMIND ME LATER 45 D23','LATER',45), ('REMIND D23','LATER',None),
    ('REMIND ME LATER 9999 D23','LATER',None)])
def test_explicit_reply_grammar(body,action,delay):
    result=parse_reply(body)
    assert result['action']==action and result['code']==23 and result['delay']==delay


def send(app,dose,now):
    with transaction():
        due_work(now)
    dispatch(now)
    return one('SELECT * FROM doses WHERE id=?',(dose,))


def test_nonzero_escalation_survives_separate_scheduler_beats(app,patient,dose):
    now=utc_now()
    with app.app_context():
        db=get_db()
        db.execute('UPDATE users SET response_minutes=5,escalation_minutes=10,missed_followup=1 WHERE id=1')
        db.execute("UPDATE patients SET contact_phone='+12025550106' WHERE id=?",(patient,))
        send(app,dose,now)
        send(app,dose,now+timedelta(minutes=6))
        assert one('SELECT status FROM doses WHERE id=?',(dose,))['status']=='missed'
        assert one("SELECT COUNT(*) n FROM messages WHERE kind='missed_followup'")['n']==1
        assert one('SELECT COUNT(*) n FROM alerts')['n']==0
        send(app,dose,now+timedelta(minutes=16))
        send(app,dose,now+timedelta(minutes=17))
        assert one('SELECT COUNT(*) n FROM alerts')['n']==1
        assert one("SELECT COUNT(*) n FROM messages WHERE kind='alert'")['n']==1
        assert one("SELECT recipient FROM messages WHERE kind='alert'")['recipient']=='+12025550106'
        assert one("SELECT COUNT(*) n FROM dose_events WHERE source='response_window_elapsed'")['n']==1


def test_requested_delay_outlives_response_window(app,patient,dose):
    now=utc_now()
    with app.app_context():
        db=get_db();db.execute('UPDATE users SET response_minutes=5 WHERE id=1')
        send(app,dose,now)
        with transaction():
            handle_reply(patient,f'REMIND ME LATER 45 D{dose}',now=now)
        send(app,dose,now+timedelta(minutes=6))
        assert one('SELECT status FROM doses WHERE id=?',(dose,))['status']=='reminder_requested'
        send(app,dose,now+timedelta(minutes=46))
        assert one("SELECT status FROM messages WHERE dedupe_key=?",(f'dose:{dose}:requested',))['status']=='simulated'
        send(app,dose,now+timedelta(minutes=51))
        assert one('SELECT status FROM doses WHERE id=?',(dose,))['status']=='missed'


def test_taken_resolves_but_does_not_forge_family_acknowledgement(app,patient,dose):
    now=utc_now()
    with app.app_context():
        send(app,dose,now)
        with transaction():
            handle_reply(patient,f'NOT YET D{dose}',now=now)
            handle_reply(patient,f'TAKEN D{dose}',now=now)
        alert=one('SELECT * FROM alerts')
        assert alert['resolved_at']
        assert alert['acknowledged_at'] is None and alert['acknowledged_by'] is None
        assert one('SELECT COUNT(*) n FROM dose_events')['n']==2


def test_settings_form_displays_health_without_fake_delivery(app,auth):
    text=auth.get('/settings').data
    assert b'not detected' in text and b'not configured' in text
    assert b'TWILIO_AUTH_TOKEN=' not in text


def test_required_family_contact_and_normalized_phone(app,auth):
    data={'name':'Fictional','phone':'+1 202 555 0150','timezone':'UTC','contact_name':'Sam','contact_phone':''}
    assert auth.post('/patients/new',data=data).status_code==200
    with app.app_context():assert one('SELECT COUNT(*) n FROM patients')['n']==0
    data['contact_phone']='+1 202 555 0151'
    assert auth.post('/patients/new',data=data).status_code==302
    with app.app_context():
        p=one('SELECT * FROM patients');assert p['phone']=='+12025550150' and p['contact_phone']=='+12025550151'


def test_live_manual_scheduler_denied(app,auth):
    app.config['DEMO_MODE']=False
    assert auth.post('/scheduler/tick').status_code==403


def test_original_uploaded_schema_migrates_without_losing_records(tmp_path):
    # The original schema string was saved directly from the uploaded ZIP by the audit step.
    source=Path(__file__).parent/'fixtures'/'original_schema.sql'
    database=tmp_path/'legacy.sqlite3'
    with sqlite3.connect(database) as db:
        db.executescript(source.read_text())
        db.execute("INSERT INTO users (email,name,password_hash) VALUES ('legacy@example.test','Legacy','unused')")
        db.execute("INSERT INTO patients (user_id,name,phone,timezone,created_at) VALUES (1,'Preserved','+12025550155','UTC','2026-10-01T00:00:00+00:00')")
    app=create_app({'TESTING':True,'DATABASE':str(database),'UPLOAD_FOLDER':str(tmp_path/'up'),'DEMO_MODE':True})
    with app.app_context():
        assert one('SELECT name FROM patients')['name']=='Preserved'
        assert one('SELECT COUNT(*) n FROM users')['n']==1
        assert one('SELECT escalation_minutes FROM users')['escalation_minutes']==0
        assert one("SELECT value FROM metadata WHERE key='schema_version'")['value']=='3'


def test_repeat_later_after_followup_sent_does_not_promise_another(app,patient,dose):
    now=utc_now()
    with app.app_context():
        send(app,dose,now)
        with transaction():handle_reply(patient,f'REMIND ME LATER 5 D{dose}',now=now)
        dispatch(now+timedelta(minutes=6))
        with transaction():reply=handle_reply(patient,f'REMIND ME LATER 5 D{dose}',now=now+timedelta(minutes=7))
        assert 'No additional reminder was scheduled' in reply
        assert one("SELECT COUNT(*) n FROM messages WHERE dedupe_key=?",(f'dose:{dose}:requested',))['n']==1
        assert one("SELECT body FROM messages WHERE dedupe_key=?",(f'dose:{dose}:requested',))['body'].startswith('SAHAYAK requested follow-up')
