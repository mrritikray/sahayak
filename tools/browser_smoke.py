"""Optional real-browser smoke test. Run ONLY against a local DEMO instance.
Install: python -m pip install playwright && python -m playwright install chromium
Start web server first, then: python tools/browser_smoke.py
"""
import json
import re
import uuid
from datetime import date
from pathlib import Path
from urllib.request import urlopen
from playwright.sync_api import sync_playwright

BASE='http://127.0.0.1:5000'
with urlopen(BASE+'/health',timeout=10) as response:
    assert json.load(response)['demo_mode'] is True, 'Browser test is only safe in local demo mode.'

out=Path('browser-artifacts');out.mkdir(exist_ok=True)
with sync_playwright() as pw:
    browser=pw.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1000})
    errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto(BASE+'/login')
    page.get_by_label('Email',exact=True).fill('demo@sahayak.local')
    page.get_by_label('Password',exact=True).fill('SahayakDemo!2026')
    page.get_by_role('button',name='Sign in →',exact=True).click()
    page.wait_for_url(BASE+'/')
    assert page.get_by_role('heading',name='A clearer picture of everyday care.').is_visible()
    page.screenshot(path=str(out/'desktop-dashboard.png'),full_page=True)
    print('PASS: real-browser sign-in and desktop dashboard')

    page.goto(BASE+'/patients')
    existing=page.locator('main').inner_text()
    fictional_phone=next('+120255501'+str(n).zfill(2) for n in range(100) if '+120255501'+str(n).zfill(2) not in existing)
    patient_name='Browser Journey Fictional '+uuid.uuid4().hex[:6]
    page.goto(BASE+'/patients/new')
    page.get_by_label('Patient name',exact=True).fill(patient_name)
    page.get_by_label('Patient WhatsApp number',exact=False).fill(fictional_phone)
    page.get_by_label('Patient timezone',exact=False).fill('UTC')
    page.get_by_label('Responsible family member',exact=False).fill('Browser Family Demo')
    page.get_by_label('Family WhatsApp number',exact=False).fill('+12025550199')
    page.get_by_role('button',name='Save patient',exact=True).click()
    page.wait_for_url(re.compile(r'/patients/\d+$'))
    patient_url=page.url
    pid=int(patient_url.rsplit('/',1)[1])
    page.get_by_role('link',name='＋ Add medicine',exact=True).click()
    page.get_by_label('Medicine name',exact=True).fill('Browser Fictional Tablet')
    page.get_by_label('Prescribed dosage text',exact=False).fill('Fictional demonstration label only')
    page.get_by_label('Start date',exact=True).fill(date.today().isoformat())
    page.get_by_label('Reminder times',exact=False).fill('00:01, 23:59')
    page.locator('input[name="confirmed"]').check()
    page.get_by_role('button',name='Save reviewed schedule',exact=True).click()
    page.wait_for_url(patient_url)
    print('PASS: patient and reviewed multi-dose medicine creation in browser')

    page.goto(BASE+'/demo')
    send_form=page.locator('form').filter(has=page.locator('button[value="reminder"]'))
    send_form.locator('select').select_option(str(pid))
    send_form.get_by_role('button',name='Simulate medicine reminder',exact=True).click()
    page.wait_for_load_state('networkidle')
    # Find the explicitly created extra practice event via the server-rendered table.
    rows=page.locator('.feed-item').filter(has_text=patient_name)
    target=rows.filter(has=page.locator('button[value="timeout"]')).first
    dose_id=target.locator('input[name="dose_id"]').input_value()
    target.get_by_role('button',name='Expire window → family alert',exact=True).click()
    page.wait_for_load_state('networkidle')
    assert page.get_by_role('status').filter(has_text='SIMULATED timeout').count()==1
    print('PASS: simulated medicine reminder and missed-response family escalation')

    reply_form=page.locator('form').filter(has=page.locator('input[name="action"][value="reply"]'))
    reply_form.locator('select').select_option(str(pid))
    reply_form.locator('input[name="body"]').fill('TAKEN D'+dose_id)
    reply_form.get_by_role('button',name='Submit simulated reply →',exact=True).click()
    page.wait_for_load_state('networkidle')
    assert page.get_by_role('status').filter(has_text='recorded as taken').count()==1
    send_form=page.locator('form').filter(has=page.locator('button[value="reminder"]'))
    send_form.locator('select').select_option(str(pid))
    send_form.get_by_role('button',name='Simulate daily check-in',exact=True).click()
    page.wait_for_load_state('networkidle')
    check=page.locator('.feed-item').filter(has_text=patient_name).filter(has_text='NEED HELP').first
    cid=re.search(r'C(\d+)',check.inner_text()).group(1)
    reply_form=page.locator('form').filter(has=page.locator('input[name="action"][value="reply"]'))
    reply_form.locator('select').select_option(str(pid))
    reply_form.locator('input[name="body"]').fill('NEED HELP C'+cid)
    reply_form.get_by_role('button',name='Submit simulated reply →',exact=True).click()
    page.wait_for_load_state('networkidle')
    page.goto(BASE+'/alerts')
    target=page.locator('.alert-row').filter(has_text=patient_name).filter(has_text='reported feeling worse').first
    target.get_by_role('button',name='Acknowledge',exact=True).click()
    page.locator('#confirm-yes').click()
    page.wait_for_load_state('networkidle')
    print('PASS: late confirmation, concerning check-in and family acknowledgement')

    page.goto(BASE+'/reports?patient_id='+str(pid))
    with page.expect_download() as download:
        page.get_by_role('link',name='↓ Export CSV',exact=True).click()
    assert download.value.suggested_filename=='sahayak-history.csv'
    assert b'Browser Journey Fictional' in Path(download.value.path()).read_bytes()
    print('PASS: CSV download through browser')
    for route in ['/patients','/medicines','/prescriptions','/reports','/alerts','/messages','/settings','/demo']:
        response=page.goto(BASE+route)
        assert response.status==200,route
    for width,height,label in [(768,1024,'tablet'),(390,844,'mobile')]:
        page.set_viewport_size({'width':width,'height':height})
        page.goto(BASE+'/')
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'),label+' layout overflow'
        if width==390:
            page.get_by_role('button',name='Toggle navigation',exact=True).click()
            assert page.locator('.sidebar').evaluate("e => e.classList.contains('open')")
            page.get_by_role('button',name='Close navigation',exact=True).click()
            assert not page.locator('.sidebar').evaluate("e => e.classList.contains('open')")
            # Locator.evaluate uses a function, avoiding CSP-unsafe string evaluation.
            for _ in range(40):
                if page.locator('.sidebar').evaluate('e => e.getBoundingClientRect().right') <= 0:
                    break
                page.wait_for_timeout(50)
            assert page.locator('.sidebar').evaluate('e => e.getBoundingClientRect().right') <= 0
        page.screenshot(path=str(out/(label+'-dashboard.png')),full_page=True)
        print('PASS: '+label+' responsive layout and navigation')
    assert not errors,errors
    print('PASS: no browser JavaScript exceptions')
    browser.close()
print('BROWSER SMOKE PASSED: all journey assertions completed; no real WhatsApp messages sent.')
