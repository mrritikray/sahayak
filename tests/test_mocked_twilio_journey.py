"""Real provider code path with a mocked Twilio client: NO external messages or network."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from twilio.request_validator import RequestValidator
from sahayak.db import get_db, one, all_rows, transaction
from sahayak.services import due_work, dispatch, handle_reply
from sahayak.validation import utc_now, stamp

TOKEN='fictional-test-token'
BASE='https://test.example'


def signed(client,path,params):
    sig=RequestValidator(TOKEN).compute_signature(BASE+path,params)
    return client.post(path,data=params,headers={'X-Twilio-Signature':sig})


def configure(app,monkeypatch):
    app.config.update(DEMO_MODE=False,TWILIO_ACCOUNT_SID='AC'+'1'*32,TWILIO_AUTH_TOKEN=TOKEN,
                      TWILIO_WHATSAPP_FROM='whatsapp:+12025550199',PUBLIC_BASE_URL=BASE)
    factory=Mock()
    count=[0]
    def response(**kwargs):
        count[0]+=1
        return SimpleNamespace(sid='SM'+f'{count[0]:032x}')
    factory.return_value.messages.create.side_effect=response
    monkeypatch.setattr('sahayak.services.Client',factory)
    return factory


def create_dose(med,now,offset):
    return get_db().execute('INSERT INTO doses (medicine_id,scheduled_at,medicine_name,dosage_text,instruction_text) VALUES (?,?,?,?,?)',
                            (med,stamp(now-timedelta(seconds=offset)),'Fictional Tablet','Fictional label','Confirmed instructions')).lastrowid


def process(now):
    with transaction():due_work(now)
    dispatch(now)


def test_mocked_twilio_entire_reminder_reply_family_alert_chain(app,client,auth,patient,med,monkeypatch):
    factory=configure(app,monkeypatch)
    now=utc_now()
    with app.app_context():
        db=get_db()
        db.execute("UPDATE users SET notifications='whatsapp',response_minutes=5,escalation_minutes=5,missed_followup=1 WHERE id=1")
        db.execute("UPDATE patients SET contact_name='Responsible family',contact_phone='+12025550106' WHERE id=?",(patient,))
        did=create_dose(med,now,30)
        process(now)
        first=factory.return_value.messages.create.call_args.kwargs
        assert first['to']=='whatsapp:+12025550104'
        assert 'Fictional Tablet' in first['body'] and 'Confirmed instructions' in first['body']
        assert one('SELECT status FROM messages')['status']=='accepted'
    params={'MessageSid':'SM'+'a'*32,'From':'whatsapp:+12025550104','Body':f'TAKEN D{did}'}
    assert b'recorded as taken' in signed(client,'/webhooks/twilio',params).data
    assert signed(client,'/webhooks/twilio',params).status_code==200
    with app.app_context():
        assert one('SELECT status FROM doses WHERE id=?',(did,))['status']=='taken'
        assert one('SELECT COUNT(*) n FROM dose_events WHERE dose_id=?',(did,))['n']==1
    assert b'Confirmed taken</span><strong>1' in auth.get('/').data
    # A second medicine-dose: NOT YET -> correct family alert through the actual provider adapter.
    with app.app_context():
        did2=create_dose(med,now,29)
        process(now)
    params.update(MessageSid='SM'+'b'*32,Body=f'NOT YET D{did2}')
    assert signed(client,'/webhooks/twilio',params).status_code==200
    with app.app_context():
        dispatch(now)
        alert=one("SELECT * FROM messages WHERE kind='alert'")
        assert alert['recipient']=='+12025550106' and alert['transport']=='whatsapp'
        assert 'NOT YET' in alert['body'] and 'not a confirmed missed dose' in alert['body']
        sid=alert['provider_sid']
        assert any(c.kwargs['to']=='whatsapp:+12025550106' for c in factory.return_value.messages.create.call_args_list)
    # Actual status endpoint with a signed mocked delivery receipt.
    assert signed(client,'/webhooks/twilio/status',{'MessageSid':sid,'MessageStatus':'delivered'}).status_code==204
    with app.app_context():
        assert one("SELECT delivered_status FROM alerts WHERE kind='not_yet'")['delivered_status']=='delivered'
        did3=create_dose(med,now,28)
        process(now)
        process(now+timedelta(minutes=6))
        assert one('SELECT status FROM doses WHERE id=?',(did3,))['status']=='missed'
        assert one("SELECT COUNT(*) n FROM alerts WHERE kind='missed_response'")['n']==0
        process(now+timedelta(minutes=11))
        missing=one("SELECT m.* FROM messages m JOIN alerts a ON a.id=m.entity_id WHERE m.kind='alert' AND a.kind='missed_response'")
        assert missing['recipient']=='+12025550106'
        assert 'Taylor Fictional' in missing['body'] and 'Fictional Tablet' in missing['body']
        assert 'scheduled for' in missing['body'] and 'has not responded' in missing['body']
        before=one('SELECT COUNT(*) n FROM messages')['n']
        calls=factory.return_value.messages.create.call_count
        process(now+timedelta(minutes=12))
        assert one('SELECT COUNT(*) n FROM messages')['n']==before
        assert factory.return_value.messages.create.call_count==calls
        assert one('SELECT COUNT(*) n FROM message_attempts')['n']==calls


def test_callback_before_rest_response_is_reconciled(app,client,patient,dose,monkeypatch):
    factory=configure(app,monkeypatch)
    sid='SM'+'e'*32
    def send(**kwargs):
        # Provider callback races the REST response while the outbox has no SID yet.
        assert signed(client,'/webhooks/twilio/status',{'MessageSid':sid,'MessageStatus':'delivered'}).status_code==204
        return SimpleNamespace(sid=sid)
    factory.return_value.messages.create.side_effect=send
    with app.app_context():
        process(utc_now())
        assert one('SELECT delivery_status FROM messages')['delivery_status']=='delivered'
        assert one('SELECT COUNT(*) n FROM provider_callbacks')['n']==1
    signed(client,'/webhooks/twilio/status',{'MessageSid':sid,'MessageStatus':'sent'})
    signed(client,'/webhooks/twilio/status',{'MessageSid':sid,'MessageStatus':'delivered'})
    with app.app_context():
        assert one('SELECT delivery_status FROM messages')['delivery_status']=='delivered'
        assert one('SELECT COUNT(*) n FROM provider_callbacks')['n']==2


def test_unsent_alert_uses_current_patient_contact(app,patient,dose):
    now=utc_now()
    with app.app_context():
        process(now)
        with transaction():handle_reply(patient,f'NOT YET D{dose}',now=now)
        get_db().execute("UPDATE patients SET contact_phone='+12025550166' WHERE id=?",(patient,))
        dispatch(now)
        assert one("SELECT recipient FROM messages WHERE kind='alert'")['recipient']=='+12025550166'


def test_resolved_alert_not_sent_after_confirmation(app,patient,dose):
    now=utc_now()
    with app.app_context():
        process(now)
        with transaction():
            handle_reply(patient,f'NOT YET D{dose}',now=now)
            handle_reply(patient,f'TAKEN D{dose}',now=now)
        dispatch(now)
        assert one("SELECT status FROM messages WHERE kind='alert'")['status']=='cancelled'
        assert one('SELECT delivered_status FROM alerts')['delivered_status']=='cancelled'


def test_pause_prevents_queued_reminder_dispatch(app,patient,dose,med):
    now=utc_now()
    with app.app_context():
        with transaction():due_work(now)
        get_db().execute("UPDATE medicines SET state='paused' WHERE id=?",(med,))
        dispatch(now)
        assert one('SELECT status FROM messages')['status']=='cancelled'
        assert one('SELECT notified_at FROM doses WHERE id=?',(dose,))['notified_at'] is None
