import csv
import io
from datetime import timedelta
from PIL import Image
from pypdf import PdfWriter
import pytest
from twilio.request_validator import RequestValidator
from sahayak.db import one, get_db, transaction
from sahayak.validation import stamp, utc_now
from sahayak.services import handle_reply


def image_file():
    data=io.BytesIO()
    Image.new('RGB',(32,32),'white').save(data,format='PNG')
    data.seek(0)
    return data

@pytest.mark.parametrize('kind',['png','pdf'])
def test_upload_and_private_download(app,auth,patient,kind):
    if kind=='png':
        data=image_file()
    else:
        writer=PdfWriter()
        writer.add_blank_page(width=100,height=100)
        data=io.BytesIO()
        writer.write(data)
        data.seek(0)
    r=auth.post('/prescriptions',data={'patient_id':str(patient),'file':(data,'fictional.'+kind)},content_type='multipart/form-data')
    assert r.status_code==302
    assert '/medicines/new' in r.location
    with app.app_context():
        f=one('SELECT * FROM uploads')
        fid=f['id']
        assert len(f['storage_name'].split('.')[0])==32
        assert one('SELECT COUNT(*) n FROM medicines')['n']==0
    response=auth.get(f'/prescriptions/{fid}/download')
    assert response.status_code==200
    assert response.headers['Content-Disposition'].startswith('attachment;')
    with auth.session_transaction() as session:
        session['user_id']=2
    assert auth.get(f'/prescriptions/{fid}/download').status_code==404

@pytest.mark.parametrize('filename,body',[('malware.exe',b'MZbad'),('fake.pdf',b'not pdf'),('fake.png',b'not image'),('vector.svg',b'<svg/>'),('empty.pdf',b'')])
def test_upload_validation(app,auth,patient,filename,body):
    r=auth.post('/prescriptions',data={'patient_id':str(patient),'file':(io.BytesIO(body),filename)},content_type='multipart/form-data')
    assert r.status_code==200
    with app.app_context():
        assert one('SELECT COUNT(*) n FROM uploads')['n']==0

def test_upload_limit_and_path_safety(app,auth,patient):
    r=auth.post('/prescriptions',data={'patient_id':str(patient),'file':(io.BytesIO(b'x'*(8*1024*1024+1)),'large.png')},content_type='multipart/form-data')
    assert r.status_code==413
    r=auth.post('/prescriptions',data={'patient_id':str(patient),'file':(image_file(),'../../outside.png')},content_type='multipart/form-data')
    assert r.status_code==302
    with app.app_context():
        item=one('SELECT * FROM uploads')
        assert '/' not in item['storage_name'] and '/' not in item['original_name']

def test_encrypted_pdf_rejected(app,auth,patient):
    writer=PdfWriter(); writer.add_blank_page(width=100,height=100); writer.encrypt('test')
    data=io.BytesIO(); writer.write(data); data.seek(0)
    r=auth.post('/prescriptions',data={'patient_id':str(patient),'file':(data,'encrypted.pdf')},content_type='multipart/form-data')
    assert b'unencrypted' in r.data
    with app.app_context():
        assert one('SELECT COUNT(*) n FROM uploads')['n']==0


def signature_config(app):
    app.config.update(TWILIO_AUTH_TOKEN='test-token-not-real',PUBLIC_BASE_URL='https://test.example')


def signed_post(client,path,params):
    signature=RequestValidator('test-token-not-real').compute_signature('https://test.example'+path,params)
    return client.post(path,data=params,headers={'X-Twilio-Signature':signature})


def test_webhook_missing_config_and_bad_signature(app,client):
    assert client.post('/webhooks/twilio',data={}).status_code==503
    signature_config(app)
    assert client.post('/webhooks/twilio',data={'Body':'TAKEN'}).status_code==403
    app.config['WTF_CSRF_ENABLED']=True
    # Provider routes bypass browser CSRF but never bypass signature validation.
    assert client.post('/webhooks/twilio/status',data={}).status_code==403


def test_signed_reply_and_duplicate_webhook(app,client,patient,dose):
    signature_config(app)
    app.config['WTF_CSRF_ENABLED']=True
    params={'MessageSid':'SM'+'a'*32,'From':'whatsapp:+12025550104','Body':f'TAKEN D{dose}'}
    first=signed_post(client,'/webhooks/twilio',params)
    assert first.status_code==200 and b'recorded' in first.data
    second=signed_post(client,'/webhooks/twilio',params)
    assert second.status_code==200
    with app.app_context():
        assert one('SELECT status FROM doses WHERE id=?',(dose,))['status']=='taken'
        assert one('SELECT source FROM doses WHERE id=?',(dose,))['source']=='whatsapp'
        assert one('SELECT COUNT(*) n FROM inbound')['n']==1
        assert one('SELECT COUNT(*) n FROM dose_events')['n']==1
        assert one("SELECT COUNT(*) n FROM messages WHERE direction='inbound'")['n']==1


def test_signed_invalid_reply_unknown_phone_and_sid(app,client,patient,dose):
    signature_config(app)
    params={'MessageSid':'SM'+'b'*32,'From':'whatsapp:+12025550104','Body':'bad reply'}
    assert b'Please reply' in signed_post(client,'/webhooks/twilio',params).data
    params.update(MessageSid='SM'+'c'*32,From='whatsapp:+12025550199')
    assert b'cannot match' in signed_post(client,'/webhooks/twilio',params).data
    params.update(MessageSid='not-a-sid')
    assert signed_post(client,'/webhooks/twilio',params).status_code==400
    with app.app_context():
        assert one('SELECT status FROM doses WHERE id=?',(dose,))['status']=='pending'


def test_webhook_status_callback_no_downgrade(app,client,patient):
    signature_config(app)
    sid='SM'+'d'*32
    with app.app_context():
        get_db().execute('''INSERT INTO messages (user_id,patient_id,dedupe_key,kind,recipient,body,transport,status,next_at,created_at,provider_sid)
          VALUES (1,?,'test-status','dose','+12025550104','Fictional','whatsapp','accepted',?,?,?)''',(patient,stamp(),stamp(),sid))
    assert signed_post(client,'/webhooks/twilio/status',{'MessageSid':sid,'MessageStatus':'delivered'}).status_code==204
    assert signed_post(client,'/webhooks/twilio/status',{'MessageSid':sid,'MessageStatus':'sent'}).status_code==204
    with app.app_context():
        assert one('SELECT delivery_status FROM messages')['delivery_status']=='delivered'
    assert signed_post(client,'/webhooks/twilio/status',{'MessageSid':sid,'MessageStatus':'arbitrary'}).status_code==400


def test_reports_csv_filters_and_dashboard_actual_data(app,auth,patient,dose):
    with app.app_context():
        with transaction():
            handle_reply(patient,f'TAKEN D{dose}','family_user:1')
    r=auth.get('/')
    assert b'Taylor Fictional' in r.data
    assert b'Confirmed taken' in r.data
    today=utc_now().date().isoformat()
    r=auth.get(f'/reports.csv?start={today}&end={today}&patient_id={patient}')
    assert r.status_code==200
    rows=list(csv.DictReader(io.StringIO(r.data.decode('utf-8-sig'))))
    confirmed=[r for r in rows if r['type']=='dose' and r['status_or_response']=='taken']
    assert len(confirmed)==1 and confirmed[0]['patient']=='Taylor Fictional'
    assert confirmed[0]['source']=='family_user:1'
    assert 'text/csv' in r.content_type
    assert auth.get('/reports.csv?start=invalid').status_code==400
    assert auth.get('/reports?start=invalid').status_code==302


def test_csv_formula_injection(app,auth,patient,dose):
    with app.app_context():
        get_db().execute('UPDATE patients SET name=? WHERE id=?',('=DANGEROUS()',patient))
    r=auth.get('/reports.csv')
    rows=list(csv.DictReader(io.StringIO(r.data.decode('utf-8-sig'))))
    assert rows[0]['patient'].startswith("'=")


def test_settings_validation_and_no_secrets(app,auth):
    data=dict(name='Family Updated',family_phone='+12025550109',timezone='Europe/London',response_minutes='90',
        checkin_time='09:15',repeat_minutes='10',repeat_limit='2',notifications='local')
    assert auth.post('/settings',data=data).status_code==302
    with app.app_context():
        u=one('SELECT * FROM users WHERE id=1')
        assert u['response_minutes']==90 and u['checkin_time']=='09:15' and u['timezone']=='Europe/London'
    data['response_minutes']='-1'
    assert auth.post('/settings',data=data).status_code==200
    app.config['TWILIO_AUTH_TOKEN']='MUST-NOT-APPEAR'
    assert b'MUST-NOT-APPEAR' not in auth.get('/settings').data


def test_full_demo_journey(app,auth,patient,med):
    assert auth.post('/demo',data={'action':'reminder','patient_id':patient}).status_code==302
    with app.app_context():
        d=one('SELECT * FROM doses WHERE is_demo=1')
        did=d['id']
        assert d['notified_at']
    assert auth.post('/demo',data={'action':'expire','patient_id':patient,'dose_id':did}).status_code==302
    with app.app_context():
        assert one('SELECT status FROM doses WHERE id=?',(did,))['status']=='missed'
        assert one('SELECT COUNT(*) n FROM alerts')['n']>=1
    auth.post('/demo',data={'action':'reply','patient_id':patient,'body':f'TAKEN D{did}'})
    auth.post('/demo',data={'action':'checkin','patient_id':patient})
    with app.app_context():
        c=one('SELECT * FROM checkins WHERE notified_at IS NOT NULL ORDER BY id DESC')
        cid=c['id']
    auth.post('/demo',data={'action':'reply','patient_id':patient,'body':f'NEED HELP C{cid}'})
    with app.app_context():
        assert one('SELECT status FROM doses WHERE id=?',(did,))['status']=='taken'
        assert one("SELECT COUNT(*) n FROM alerts WHERE kind='concerning_health'")['n']==1
        assert one("SELECT COUNT(*) n FROM messages WHERE transport!='demo'")['n']==0
    assert auth.get('/').status_code==200
    assert auth.get('/reports.csv').status_code==200
    app.config['DEMO_MODE']=False
    assert auth.get('/demo').status_code==404
