import pytest
from werkzeug.security import generate_password_hash
from sahayak import create_app
from sahayak.db import get_db
from sahayak.validation import stamp, utc_now

@pytest.fixture
def app(tmp_path):
    app=create_app({'TESTING':True,'WTF_CSRF_ENABLED':False,'SECRET_KEY':'test-only-not-for-production',
      'DATABASE':str(tmp_path/'test.sqlite3'),'UPLOAD_FOLDER':str(tmp_path/'uploads'),
      'DEMO_MODE':True,'TWILIO_ACCOUNT_SID':'','TWILIO_AUTH_TOKEN':'','TWILIO_WHATSAPP_FROM':'','PUBLIC_BASE_URL':''})
    with app.app_context():
        db=get_db()
        for email,name in [('one@example.test','Family One'),('two@example.test','Family Two')]:
            db.execute('INSERT INTO users (email,name,password_hash,timezone,repeat_limit) VALUES (?,?,?,\'UTC\',0)',
                       (email,name,generate_password_hash('SafeTestPassword!')))
        db.execute("UPDATE users SET notifications='local'")
    return app

@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    import requests.sessions
    def blocked(*args, **kwargs):
        raise AssertionError('External network is forbidden in automated tests')
    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)

@pytest.fixture
def client(app):
    return app.test_client()

@pytest.fixture
def auth(client):
    client.post('/login',data={'email':'one@example.test','password':'SafeTestPassword!'})
    return client

@pytest.fixture
def patient(app):
    with app.app_context():
        return get_db().execute('INSERT INTO patients (user_id,name,phone,timezone,created_at) VALUES (1,?,?,?,?)',
          ('Taylor Fictional','+12025550104','UTC',stamp())).lastrowid

@pytest.fixture
def med(app,patient):
    from sahayak.validation import medicine_values
    data={'name':'Fictional Tablet','dosage':'Fictional label only','start_date':utc_now().date().isoformat(),'times':'08:00, 20:00','state':'active','confirmed':'yes'}
    with app.app_context():
        return get_db().execute('''INSERT INTO medicines (name,dosage,instructions,start_date,end_date,times,state,fingerprint,patient_id,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',medicine_values(data)+(patient,stamp())).lastrowid

@pytest.fixture
def dose(app,med):
    from datetime import timedelta
    with app.app_context():
        return get_db().execute('''INSERT INTO doses (medicine_id,scheduled_at,medicine_name,dosage_text,instruction_text)
           VALUES (?,?,?,?,?)''',(med,stamp(utc_now()-timedelta(minutes=1)),'Fictional Tablet','Fictional label only','')).lastrowid
