from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from twilio.base.exceptions import TwilioRestException
from sahayak.db import get_db, one, transaction
from sahayak.validation import stamp, utc_now
from sahayak.services import due_work, dispatch, tick, provider_send, create_alert


def test_selected_family_confirmation_without_javascript(app,auth,patient,dose):
    assert auth.post('/doses/confirm',data={'dose_id':dose}).status_code==302
    with app.app_context():
        assert one('SELECT status,source FROM doses WHERE id=?',(dose,))['status']=='taken'
        assert one('SELECT source FROM doses WHERE id=?',(dose,))['source']=='family_user:1'
    assert auth.post('/doses/confirm',data={'dose_id':'not-a-number'}).status_code==400


def test_concurrent_worker_lease(app,med):
    now=utc_now()
    def run():
        with app.app_context():
            return tick(now)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _:run(),range(2)))==[False,True]
    with app.app_context():
        assert one('SELECT COUNT(*) n FROM doses')['n']==4


def test_concurrent_outbox_claim(app,dose,monkeypatch):
    now=utc_now()
    sends=[]
    def send(m):
        import time
        sends.append(m['id'])
        time.sleep(.05)
        return 'accepted','SM'+'e'*32,None,False
    monkeypatch.setattr('sahayak.services.provider_send',send)
    with app.app_context():
        with transaction():
            due_work(now)
        get_db().execute("UPDATE messages SET transport='whatsapp'")
    def run():
        with app.app_context():
            dispatch(now)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _:run(),range(2)))
    assert len(sends)==1
    with app.app_context():
        assert one('SELECT attempts FROM messages')['attempts']==1


def test_in_app_fallback_no_credentials(app,patient):
    app.config['DEMO_MODE']=False
    with app.app_context():
        with transaction():
            create_alert(one('SELECT * FROM users WHERE id=1'),one('SELECT * FROM patients WHERE id=?',(patient,)),
                         'test','test-local','Fictional check-on-parent notification.',utc_now())
        dispatch()
        assert one('SELECT transport,status FROM messages')['transport']=='local'
        assert one('SELECT status FROM messages')['status']=='in_app'


def test_twilio_client_success_mocked_only(app,monkeypatch):
    factory=Mock()
    factory.return_value.messages.create.return_value=SimpleNamespace(sid='SM'+'f'*32)
    monkeypatch.setattr('sahayak.services.Client',factory)
    app.config.update(TWILIO_ACCOUNT_SID='AC'+'0'*32,TWILIO_AUTH_TOKEN='fake-token',
        TWILIO_WHATSAPP_FROM='whatsapp:+12025550110',PUBLIC_BASE_URL='https://test.example')
    with app.app_context():
        assert provider_send({'recipient':'+12025550104','body':'Fictional test message'})[:2]==('accepted','SM'+'f'*32)
    sent=factory.return_value.messages.create.call_args.kwargs
    assert sent['to']=='whatsapp:+12025550104' and sent['status_callback']=='https://test.example/webhooks/twilio/status'


def test_twilio_outbound_does_not_require_public_webhook_url(app,monkeypatch):
    factory=Mock()
    factory.return_value.messages.create.return_value=SimpleNamespace(sid='SM'+'f'*32)
    monkeypatch.setattr('sahayak.services.Client',factory)
    app.config.update(TWILIO_ACCOUNT_SID='AC'+'0'*32,TWILIO_AUTH_TOKEN='fake-token',
        TWILIO_WHATSAPP_FROM='whatsapp:+12025550110',PUBLIC_BASE_URL='')
    with app.app_context():
        assert provider_send({'recipient':'+12025550104','body':'Fictional test message'})[:2]==('accepted','SM'+'f'*32)
    sent=factory.return_value.messages.create.call_args.kwargs
    assert sent['from_']=='whatsapp:+12025550110'
    assert sent['to']=='whatsapp:+12025550104'
    assert 'status_callback' not in sent


def test_invalid_twilio_sender_is_rejected_without_api_call(app,monkeypatch):
    factory=Mock()
    monkeypatch.setattr('sahayak.services.Client',factory)
    app.config.update(TWILIO_ACCOUNT_SID='AC'+'0'*32,TWILIO_AUTH_TOKEN='fake-token',
        TWILIO_WHATSAPP_FROM='+12025550110',PUBLIC_BASE_URL='')
    with app.app_context():
        assert provider_send({'recipient':'+12025550104','body':'Fictional'}) == ('failed',None,'not_configured',False)
    factory.assert_not_called()


def test_twilio_client_timeout_and_rejection_mocked(app,monkeypatch,caplog):
    factory=Mock()
    monkeypatch.setattr('sahayak.services.Client',factory)
    app.config.update(TWILIO_ACCOUNT_SID='AC'+'0'*32,TWILIO_AUTH_TOKEN='fake-token',
        TWILIO_WHATSAPP_FROM='whatsapp:+12025550110',PUBLIC_BASE_URL='https://test.example')
    with app.app_context():
        factory.return_value.messages.create.side_effect=TimeoutError('not logged')
        assert provider_send({'recipient':'+12025550104','body':'Fictional'})[0]=='unknown'
        factory.return_value.messages.create.side_effect=TwilioRestException(429,'uri',msg='not logged',code=20429)
        result=provider_send({'recipient':'+12025550104','body':'Fictional'})
        assert result[0]=='queued' and result[3] is True
    assert 'HTTP status 429' in caplog.text and 'error code 20429' in caplog.text
    assert 'fake-token' not in caplog.text and 'Fictional' not in caplog.text


def test_dashboard_has_no_cross_family_message_content(app,auth,patient):
    with app.app_context():
        other=get_db().execute('INSERT INTO patients (user_id,name,phone,timezone,created_at) VALUES (2,?,?,?,?)',
            ('INVISIBLE OTHER FAMILY','+12025550111','UTC',stamp())).lastrowid
        get_db().execute('INSERT INTO alerts (user_id,patient_id,kind,entity_key,text,created_at) VALUES (2,?,?,?,?,?)',
            (other,'test','cross-family','PRIVATE ALERT',stamp()))
    for path in ['/','/patients','/reports','/alerts','/messages','/prescriptions','/demo']:
        data=auth.get(path).data
        assert b'INVISIBLE OTHER FAMILY' not in data and b'PRIVATE ALERT' not in data


def test_demo_seed_restart_idempotence(tmp_path):
    from sahayak import create_app
    from sahayak.seed import seed_demo
    config={'TESTING':True,'DATABASE':str(tmp_path/'seed.sqlite3'),'UPLOAD_FOLDER':str(tmp_path/'files'),'DEMO_MODE':True}
    app=create_app(config)
    with app.app_context():
        seed_demo()
        first=one('SELECT COUNT(*) n FROM doses')['n']
        seed_demo()
        assert one('SELECT COUNT(*) n FROM doses')['n']==first
        assert one('SELECT COUNT(*) n FROM patients')['n']==2
        assert one('SELECT COUNT(*) n FROM doses WHERE medicine_name IS NULL')['n']==0
