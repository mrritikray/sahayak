from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo
import sqlite3
import pytest
from sahayak import create_app
from sahayak.db import get_db, one, all_rows, transaction
from sahayak.services import generate_events, due_work, dispatch, tick, handle_reply, provider_send
from sahayak.validation import stamp, utc_now, parse, local_instant


def test_schedule_generation_deduplicates_timezone(app,patient,med):
    now=utc_now()
    with app.app_context():
        get_db().execute("UPDATE patients SET timezone='Asia/Kolkata' WHERE id=?",(patient,))
        with transaction():
            generate_events(now)
            generate_events(now)
        rows=all_rows('SELECT * FROM doses WHERE medicine_id=?',(med,))
        assert len(rows)==4
        assert len({r['scheduled_at'] for r in rows})==4
        local_hours={parse(r['scheduled_at']).astimezone(ZoneInfo('Asia/Kolkata')).hour for r in rows}
        assert local_hours=={8,20}
        assert one('SELECT COUNT(*) n FROM checkins')['n']==2
        with pytest.raises(sqlite3.IntegrityError):
            get_db().execute('INSERT INTO doses (medicine_id,scheduled_at) VALUES (?,?)',(med,rows[0]['scheduled_at']))

@pytest.mark.parametrize('day,clock,expected',[
  (date(2026,10,18),'08:00','2026-10-18T02:30:00+00:00'),
  (date(2026,3,8),'02:30','2026-03-08T07:30:00+00:00'),
  (date(2026,11,1),'01:30','2026-11-01T05:30:00+00:00')])
def test_dst_and_india_handling(day,clock,expected):
    zone='Asia/Kolkata' if day.month==10 else 'America/New_York'
    assert stamp(local_instant(day,clock,zone))==expected

def test_medicine_dates_and_inactive(app,patient,med):
    with app.app_context():
        tomorrow=utc_now().date()+timedelta(days=1)
        get_db().execute('UPDATE medicines SET start_date=?,end_date=? WHERE id=?',(tomorrow.isoformat(),tomorrow.isoformat(),med))
        with transaction():
            generate_events()
        assert one('SELECT COUNT(*) n FROM doses')['n']==2
        get_db().execute("UPDATE medicines SET state='paused'")
        with transaction():
            generate_events(utc_now()+timedelta(days=1))
        assert one('SELECT COUNT(*) n FROM doses')['n']==2

def test_duplicate_reminders_no_external_calls(app,dose,monkeypatch):
    def forbidden(_):
        raise AssertionError('Demo attempted external send')
    monkeypatch.setattr('sahayak.services.provider_send',forbidden)
    now=utc_now()
    with app.app_context():
        with transaction():
            due_work(now)
            due_work(now)
        assert one("SELECT COUNT(*) n FROM messages WHERE kind='dose'")['n']==1
        dispatch(now)
        dispatch(now)
        d=one('SELECT * FROM doses WHERE id=?',(dose,))
        assert d['notified_at'] and d['expected_at']
        msg=one("SELECT * FROM messages WHERE kind='dose'")
        assert msg['status']=='simulated' and msg['attempts']==1

@pytest.mark.parametrize('body,status',[('TAKEN','taken'),('REMIND','reminder_requested')])
def test_patient_replies_and_persistence(app,patient,dose,body,status):
    with app.app_context():
        with transaction():
            reply=handle_reply(patient,f'{body} D{dose}','demo_patient')
        assert 'recorded' in reply
        d=one('SELECT * FROM doses WHERE id=?',(dose,))
        assert d['status']==status and d['responded_at'] and d['source']=='demo_patient'
        path=app.config['DATABASE']
    # Re-open app against same database: not an in-memory assertion.
    second=create_app({'TESTING':True,'DATABASE':path,'UPLOAD_FOLDER':app.config['UPLOAD_FOLDER'],'DEMO_MODE':True})
    with second.app_context():
        assert one('SELECT status FROM doses WHERE id=?',(dose,))['status']==status

@pytest.mark.parametrize('body',['hello','yes D1','TAKEN C1','WORSE D1','TAKEN D999999','<script>bad</script>'])
def test_invalid_replies_do_not_mark_taken(app,patient,dose,body):
    with app.app_context():
        with transaction():
            reply=handle_reply(patient,body)
        assert reply
        assert one('SELECT status FROM doses WHERE id=?',(dose,))['status']=='pending'

def test_future_and_cross_patient_reply_protection(app,patient,dose,med):
    with app.app_context():
        other=get_db().execute('INSERT INTO patients (user_id,name,phone,timezone,created_at) VALUES (2,?,?,?,?)',
          ('Other Demo','+12025550108','UTC',stamp())).lastrowid
        with transaction():
            assert 'No unique matching' in handle_reply(other,f'TAKEN D{dose}')
        future=get_db().execute('INSERT INTO doses (medicine_id,scheduled_at) VALUES (?,?)',(med,stamp(utc_now()+timedelta(days=1)))).lastrowid
        with transaction():
            assert 'Future doses' in handle_reply(patient,f'TAKEN D{future}')
        assert one('SELECT COUNT(*) n FROM doses WHERE status=\'taken\'')['n']==0

def test_duplicate_confirmation_and_ambiguous_plain_reply(app,patient,dose,med):
    with app.app_context():
        now=utc_now()
        with transaction():
            due_work(now)
        dispatch(now)
        second=get_db().execute('INSERT INTO doses (medicine_id,scheduled_at,notified_at) VALUES (?,?,?)',
          (med,stamp(now-timedelta(minutes=3)),stamp(now))).lastrowid
        with transaction():
            assert 'No unique matching' in handle_reply(patient,'TAKEN')
            handle_reply(patient,f'TAKEN D{dose}')
            assert 'already recorded' in handle_reply(patient,f'TAKEN D{dose}')
        assert one('SELECT COUNT(*) n FROM dose_events WHERE dose_id=?',(dose,))['n']==1
        assert one('SELECT status FROM doses WHERE id=?',(second,))['status']=='pending'

def test_missed_response_alert_dedup_ack_late_confirmation(app,auth,patient,dose):
    now=utc_now()
    with app.app_context():
        with transaction():
            due_work(now)
        dispatch(now)
        later=now+timedelta(minutes=61)
        with transaction():
            due_work(later)
            due_work(later)
        assert one('SELECT status FROM doses WHERE id=?',(dose,))['status']=='missed'
        assert one('SELECT COUNT(*) n FROM alerts')['n']==1
        a=one('SELECT * FROM alerts')
        assert 'does not prove' in a['text']
        aid=a['id']
        dispatch(later)
        assert one("SELECT status FROM messages WHERE kind='alert'")['status']=='simulated'
        with transaction():
            handle_reply(patient,f'TAKEN D{dose}',now=later)
        assert one('SELECT status FROM doses WHERE id=?',(dose,))['status']=='taken'
        assert one('SELECT COUNT(*) n FROM dose_events WHERE dose_id=?',(dose,))['n']==2
    assert auth.post(f'/alerts/{aid}/acknowledge').status_code==302
    with app.app_context():
        assert one('SELECT acknowledged_at FROM alerts')['acknowledged_at']
    with auth.session_transaction() as session:
        session['user_id']=2
    assert auth.post(f'/alerts/{aid}/acknowledge').status_code==404
    assert auth.post(f'/doses/{dose}/confirm').status_code==404

@pytest.mark.parametrize('response,alert_count',[('BETTER',0),('SAME',0),('WORSE',1),('NEED HELP',1)])
def test_daily_checkins_concerning_and_duplicates(app,patient,response,alert_count):
    now=utc_now()
    with app.app_context():
        cid=get_db().execute('INSERT INTO checkins (patient_id,local_date,scheduled_at) VALUES (?,?,?)',
            (patient,now.date().isoformat(),stamp(now-timedelta(minutes=1)))).lastrowid
        with transaction():
            due_work(now)
        dispatch(now)
        with transaction():
            assert 'recorded' in handle_reply(patient,f'{response} C{cid}')
            assert 'already recorded' in handle_reply(patient,f'{response} C{cid}')
        assert one('SELECT response FROM checkins')['response']==response
        assert one('SELECT COUNT(*) n FROM alerts')['n']==alert_count

def test_missed_checkin_once(app,patient):
    now=utc_now()
    with app.app_context():
        get_db().execute('INSERT INTO checkins (patient_id,local_date,scheduled_at) VALUES (?,?,?)',(patient,now.date().isoformat(),stamp(now)))
        with transaction():
            due_work(now)
        dispatch(now)
        with transaction():
            due_work(now+timedelta(hours=2))
            due_work(now+timedelta(hours=2))
        assert one('SELECT COUNT(*) n FROM alerts')['n']==1
        assert one('SELECT response FROM checkins')['response'] is None

def test_followup_limit_and_patient_requested_reminder(app,patient,dose):
    now=utc_now()
    with app.app_context():
        get_db().execute('UPDATE users SET repeat_limit=1,repeat_minutes=5,reply_delay_minutes=5 WHERE id=1')
        with transaction():
            due_work(now)
        dispatch(now)
        with transaction():
            due_work(now+timedelta(minutes=6))
            due_work(now+timedelta(minutes=7))
            handle_reply(patient,f'REMIND D{dose}',now=now)
            handle_reply(patient,f'REMIND D{dose}',now=now)
        assert one("SELECT COUNT(*) n FROM messages WHERE kind='followup'")['n']==2
        dispatch(now+timedelta(minutes=8))
        assert one("SELECT COUNT(*) n FROM messages WHERE kind='followup' AND attempts=1")['n']==2

def test_worker_lease_and_pause_suppression(app,dose):
    now=utc_now()
    with app.app_context():
        assert tick(now) is True
        assert tick(now) is False
        get_db().execute("UPDATE medicines SET state='paused'")
        assert tick(now+timedelta(seconds=16)) is True
        # Lease is DB-backed, not a process-local flag.
        assert one("SELECT expires_at FROM leases WHERE name='scheduler'")

def test_deactivate_cancels_queued_messages(app,patient,dose):
    with app.app_context():
        with transaction():
            due_work(utc_now())
        get_db().execute('UPDATE patients SET active=0 WHERE id=?',(patient,))
        dispatch()
        assert one('SELECT status FROM messages')['status']=='cancelled'

def test_rate_limit_retries_bounded(app,dose,monkeypatch):
    now=utc_now()
    calls=[]
    monkeypatch.setattr('sahayak.services.provider_send',lambda m: (calls.append(m['id']) or ('queued',None,'provider_20429',True)))
    with app.app_context():
        with transaction():
            due_work(now)
        get_db().execute("UPDATE messages SET transport='whatsapp'")
        dispatch(now)
        dispatch(now+timedelta(minutes=2))
        dispatch(now+timedelta(minutes=5))
        dispatch(now+timedelta(minutes=10))
        assert len(calls)==3
        assert one('SELECT status,attempts FROM messages')['status']=='failed'
        assert one('SELECT attempts FROM messages')['attempts']==3
        assert one('SELECT notified_at FROM doses WHERE id=?',(dose,))['notified_at'] is None

def test_uncertain_send_no_blind_retry(app,dose,monkeypatch):
    calls=[]
    monkeypatch.setattr('sahayak.services.provider_send',lambda m: (calls.append(1) or ('unknown',None,'delivery_uncertain_do_not_resend',False)))
    with app.app_context():
        with transaction():
            due_work(utc_now())
        get_db().execute("UPDATE messages SET transport='whatsapp'")
        dispatch()
        dispatch(utc_now()+timedelta(days=1))
        assert len(calls)==1
        assert one('SELECT status FROM messages')['status']=='unknown'

def test_live_missing_credentials_fail_safely(app,dose):
    app.config['DEMO_MODE']=False
    with app.app_context():
        with transaction():
            due_work(utc_now())
        dispatch()
        m=one('SELECT * FROM messages')
        assert m['transport']=='whatsapp' and m['status']=='failed' and m['error_code']=='not_configured'
        assert one('SELECT status FROM doses WHERE id=?',(dose,))['status']=='pending'
