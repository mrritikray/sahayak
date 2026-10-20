from datetime import timedelta
import pytest
from sahayak.db import get_db, one
from sahayak.validation import stamp, utc_now


def patient_form(**changes):
    data=dict(name='Robin Fictional',phone='+12025550105',timezone='Asia/Kolkata',contact_name='Sam Demo',contact_phone='+12025550106')
    data.update(changes)
    return data

def med_form(**changes):
    data=dict(name='Fictional Medicine B',dosage='Entered demo label — not for use',instructions='Fictional only',
      start_date=utc_now().date().isoformat(),end_date='',times='08:00, 20:00',state='active',confirmed='yes')
    data.update(changes)
    return data

def test_startup_without_credentials(client,auth):
    assert client.get('/health').json['status']=='ok'
    assert client.get('/health').json['demo_mode'] is True
    for route in ['/','/patients','/medicines','/prescriptions','/reports','/alerts','/messages','/settings','/demo']:
        response=auth.get(route)
        assert response.status_code==200,route
        assert b'LOCAL DEMO' in response.data

def test_authentication_password_hash_logout(app,client):
    assert client.get('/patients').status_code==302
    assert client.post('/login',data={'email':'one@example.test','password':'wrong'}).status_code==200
    assert client.post('/register',data={'email':'new@example.test','name':'New Family','password':'SecurePassword123!'}).status_code==302
    with app.app_context():
        assert one('SELECT password_hash FROM users WHERE email=?',('new@example.test',))['password_hash']!='SecurePassword123!'
    assert client.post('/login',data={'email':'new@example.test','password':'SecurePassword123!'}).status_code==302
    assert client.get('/').status_code==200
    assert client.post('/logout').status_code==302
    assert client.get('/settings').status_code==302

def test_login_throttling(client):
    for _ in range(5):
        assert client.post('/login',data={'email':'one@example.test','password':'bad'}).status_code==200
    assert client.post('/login',data={'email':'one@example.test','password':'bad'}).status_code==429

def test_patient_create_edit_deactivate(app,auth):
    r=auth.post('/patients/new',data=patient_form())
    assert r.status_code==302
    with app.app_context():
        p=one('SELECT * FROM patients')
        pid=p['id']
        assert p['user_id']==1 and p['timezone']=='Asia/Kolkata'
    assert auth.post(f'/patients/{pid}/edit',data=patient_form(name='Robin Updated',timezone='UTC')).status_code==302
    with app.app_context():
        assert one('SELECT name FROM patients WHERE id=?',(pid,))['name']=='Robin Updated'
    auth.post(f'/patients/{pid}/active')
    with app.app_context():
        assert one('SELECT active FROM patients WHERE id=?',(pid,))['active']==0
    assert auth.get(f'/patients/{pid}').status_code==200

@pytest.mark.parametrize('changes',[{'name':''},{'phone':'not a phone'},{'timezone':'Wrong/Zone'},{'contact_phone':'123'}])
def test_patient_validation(app,auth,changes):
    assert auth.post('/patients/new',data=patient_form(**changes)).status_code==200
    with app.app_context():
        assert one('SELECT COUNT(*) n FROM patients')['n']==0

def test_duplicate_phone_and_isolation(app,auth,patient):
    r=auth.post('/patients/new',data=patient_form(phone='+12025550104'))
    assert b'already registered' in r.data
    with app.app_context():
        other=get_db().execute('INSERT INTO patients (user_id,name,phone,timezone,created_at) VALUES (2,?,?,?,?)',
            ('PRIVATE FICTIONAL PROFILE','+12025550107','UTC',stamp())).lastrowid
    assert auth.get(f'/patients/{other}').status_code==404
    assert auth.post(f'/patients/{other}/edit',data=patient_form()).status_code==404
    assert auth.post(f'/patients/{other}/active').status_code==404
    assert auth.get(f'/patients/{other}/medicines/new').status_code==404
    assert b'PRIVATE FICTIONAL PROFILE' not in auth.get('/patients').data
    assert auth.get(f'/reports?patient_id={other}').status_code==404

def test_medicine_create_edit_pause_duplicate(app,auth,patient):
    path=f'/patients/{patient}/medicines/new'
    assert auth.post(path,data=med_form()).status_code==302
    with app.app_context():
        m=one('SELECT * FROM medicines')
        mid=m['id']
        assert m['times']=='["08:00", "20:00"]'
    assert b'overlapping schedule' in auth.post(path,data=med_form()).data
    assert auth.post(f'/patients/{patient}/medicines/{mid}/edit',data=med_form(state='paused')).status_code==302
    with app.app_context():
        assert one('SELECT state FROM medicines')['state']=='paused'
    assert auth.post(f'/patients/{patient}/medicines/{mid}/edit',data=med_form(state='inactive')).status_code==302

@pytest.mark.parametrize('changes',[
  {'times':'25:00'}, {'times':'08:00,08:00'}, {'times':'8:00'}, {'times':''},
  {'times':','.join(f'{h:02}:00' for h in range(9))}, {'end_date':'2000-01-01'},
  {'start_date':'2026-99-99'}, {'confirmed':''}, {'dosage':''}, {'state':'unsafe'}])
def test_schedule_validation(app,auth,patient,changes):
    r=auth.post(f'/patients/{patient}/medicines/new',data=med_form(**changes))
    assert r.status_code==200
    with app.app_context():
        assert one('SELECT COUNT(*) n FROM medicines')['n']==0

def test_csrf_and_security_headers(app,client):
    app.config['WTF_CSRF_ENABLED']=True
    assert client.post('/login',data={'email':'one@example.test','password':'SafeTestPassword!'}).status_code==400
    r=client.get('/login')
    assert r.headers['X-Frame-Options']=='DENY'
    assert 'frame-ancestors' in r.headers['Content-Security-Policy']
    import re
    token=re.search(rb'name="csrf_token"[^>]*value="([^"]+)"',r.data).group(1).decode()
    assert client.post('/login',data={'csrf_token':token,'email':'one@example.test','password':'SafeTestPassword!'}).status_code==302

def test_patient_input_is_escaped(auth):
    auth.post('/patients/new',data=patient_form(name='<script>alert(1)</script>'))
    r=auth.get('/patients')
    assert b'<script>alert(1)</script>' not in r.data
    assert b'&lt;script&gt;' in r.data

def test_medicine_edit_preserves_snapshot(app,auth,patient,dose,med):
    r=auth.post(f'/patients/{patient}/medicines/{med}/edit',data=med_form(name='Changed Fictional Medicine'))
    assert r.status_code==302
    with app.app_context():
        d=one('SELECT * FROM doses WHERE id=?',(dose,))
        assert d['medicine_name']=='Fictional Tablet'
        assert d['source']=='schedule_changed_no_reminder'
