import csv
import hashlib
import io
import json
import re
import sqlite3
import uuid
from datetime import date, timedelta
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect,
                   render_template, request, send_file, session, url_for)
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from . import alerts as alert_policy
from .db import all_rows, get_db, one, transaction
from .providers import FAILED_STATES, provider_ready, webhook_ready
from .services import dispatch, due_work, generate_events, handle_reply, tick
from .validation import (ValidationError, clock, integer, medicine_values, parse,
                         patient_values, phone, settings_values, stamp, text, tz, utc_now)

bp = Blueprint('web', __name__)

# Every state a dose can be in, with the wording shown to the family. "missed" is about the
# reply, never about the medicine: no reply is not proof that a dose was not taken.
DOSE_LABELS = {
    'pending': ('scheduled', 'Reminder not sent yet'),
    'awaiting_response': ('awaiting_response', 'Reminder sent, waiting for reply'),
    'reminder_requested': ('reminder_requested', 'Patient asked to be reminded later'),
    'unresolved': ('unresolved', 'Patient replied NOT YET, still open'),
    'taken': ('taken', 'Patient-confirmed taken'),
    'missed': ('missed', 'No reply within the response window'),
}
OPEN_STATUSES = ('pending', 'awaiting_response', 'reminder_requested', 'unresolved')


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not g.user:
            return redirect(url_for('web.login'))
        return fn(*args, **kwargs)
    return wrapped


def patient_owned(ident):
    p = one('SELECT * FROM patients WHERE id=? AND user_id=?', (ident, g.user['id']))
    if not p:
        abort(404)
    return p


def owned_dose(ident):
    d = one('''SELECT d.*,m.patient_id FROM doses d JOIN medicines m ON m.id=d.medicine_id
      JOIN patients p ON p.id=m.patient_id WHERE d.id=? AND p.user_id=?''', (ident, g.user['id']))
    if not d:
        abort(404)
    return d


def validate_account(email, name, password):
    email = text(email, 'Email', 254).lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise ValidationError('Enter a valid email address.')
    name = text(name, 'Family member name')
    if len(password or '') < 12 or len(password) > 128:
        raise ValidationError('Password must contain 12 to 128 characters.')
    return email, name, password


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()[:254]
        key = hashlib.sha256((email + '|' + (request.remote_addr or '')).encode()).hexdigest()
        attempt = one('SELECT * FROM login_attempts WHERE key=?', (key,))
        now = utc_now()
        if attempt and attempt['attempts'] >= 5 and attempt['blocked_until'] > stamp(now):
            abort(429, description='Too many sign-in attempts. Wait 15 minutes.')
        user = one('SELECT * FROM users WHERE email=?', (email,))
        # Always calculate a hash even if the account is unknown.
        valid = check_password_hash(user['password_hash'] if user else generate_password_hash('not-a-real-password'),
                                    request.form.get('password', '')[:129])
        if user and valid:
            session.clear()
            session['user_id'] = user['id']
            session.permanent = True
            get_db().execute('DELETE FROM login_attempts WHERE key=?', (key,))
            return redirect(url_for('web.dashboard'))
        count = (attempt['attempts'] if attempt and attempt['blocked_until'] > stamp(now) else 0) + 1
        get_db().execute('INSERT OR REPLACE INTO login_attempts VALUES (?,?,?)',
                         (key, count, stamp(now + timedelta(minutes=15))))
        flash('Email or password is incorrect.', 'error')
    return render_template('login.html')


@bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        try:
            email, name, password = validate_account(request.form.get('email'), request.form.get('name'),
                                                    request.form.get('password'))
            with transaction():
                get_db().execute('INSERT INTO users (email,name,password_hash) VALUES (?,?,?)',
                                 (email, name, generate_password_hash(password)))
            flash('Your family account is ready. Sign in to continue.', 'success')
            return redirect(url_for('web.login'))
        except ValidationError as e:
            flash(str(e), 'error')
        except sqlite3.IntegrityError:
            flash('That email cannot be used. Sign in or choose another.', 'error')
    return render_template('register.html')


@bp.post('/logout')
@login_required
def logout():
    session.clear()
    return redirect(url_for('web.login'))


def refresh_schedule():
    with transaction():
        generate_events(user_id=g.user['id'])


def records(start=None, end=None, patient_id=None, statuses=None):
    """All dose rows for this family, newest first, optionally filtered."""
    where = 'p.user_id=?'
    args = [g.user['id']]
    if patient_id:
        patient_owned(patient_id)
        where += ' AND p.id=?'
        args.append(patient_id)
    rows = all_rows('''SELECT d.*,COALESCE(d.medicine_name,m.name) medicine,
        COALESCE(d.dosage_text,m.dosage) dosage,p.name patient,p.id patient_id,p.timezone patient_timezone
      FROM doses d JOIN medicines m ON m.id=d.medicine_id JOIN patients p ON p.id=m.patient_id
      WHERE ''' + where + ' ORDER BY d.scheduled_at DESC', args)
    zone = ZoneInfo(g.user['timezone'])
    if start:
        rows = [r for r in rows if start <= parse(r['scheduled_at']).astimezone(zone).date() <= end]
    if statuses:
        rows = [r for r in rows if r['status'] in statuses]
    return rows


def alert_rows(patient_id=None, unresolved_only=False, limit=None):
    where = 'a.user_id=?'
    args = [g.user['id']]
    if patient_id:
        where += ' AND a.patient_id=?'
        args.append(patient_id)
    if unresolved_only:
        where += ' AND a.acknowledged_at IS NULL AND a.resolved_at IS NULL'
    sql = ('SELECT a.*,p.name patient FROM alerts a JOIN patients p ON p.id=a.patient_id WHERE '
           + where + ' ORDER BY a.id DESC')
    if limit:
        sql += f' LIMIT {int(limit)}'
    return all_rows(sql, args)


@bp.get('/')
@login_required
def dashboard():
    refresh_schedule()
    today = utc_now().astimezone(ZoneInfo(g.user['timezone'])).date()
    doses = records(today, today)
    stats = {
        'patients': one('SELECT COUNT(*) n FROM patients WHERE user_id=?', (g.user['id'],))['n'],
        'active_patients': one('SELECT COUNT(*) n FROM patients WHERE user_id=? AND active=1', (g.user['id'],))['n'],
        'scheduled': len(doses),
        'taken': sum(d['status'] == 'taken' for d in doses),
        'patient_taken': sum(d['status'] == 'taken' and not (d['source'] or '').startswith('family_user:') for d in doses),
        'pending': sum(d['status'] in ('pending', 'awaiting_response') for d in doses),
        'reminder_requested': sum(d['status'] == 'reminder_requested' for d in doses),
        'unresolved': sum(d['status'] == 'unresolved' for d in doses),
        'missed': sum(d['status'] == 'missed' for d in doses),
        'unresolved_alerts': one('SELECT COUNT(*) n FROM alerts WHERE user_id=? AND acknowledged_at IS NULL AND resolved_at IS NULL',
                                 (g.user['id'],))['n'],
    }
    week = records(today - timedelta(days=6), today)
    replies = all_rows('''SELECT r.*,p.name patient FROM reply_events r JOIN patients p ON p.id=r.patient_id
        WHERE p.user_id=? ORDER BY r.id DESC LIMIT 8''', (g.user['id'],))
    checkins = all_rows('''SELECT c.*,p.name patient FROM checkins c JOIN patients p ON p.id=c.patient_id
        WHERE p.user_id=? AND c.responded_at IS NOT NULL ORDER BY c.responded_at DESC LIMIT 5''',
        (g.user['id'],))
    messages = all_rows('''SELECT m.*,p.name patient FROM messages m LEFT JOIN patients p ON p.id=m.patient_id
        WHERE m.user_id=? AND m.direction='outbound' ORDER BY m.id DESC LIMIT 8''', (g.user['id'],))
    failures = one("SELECT COUNT(*) n FROM messages WHERE user_id=? AND (status IN ('failed','unknown') OR delivery_status IN ('failed','undelivered','canceled'))",
                   (g.user['id'],))['n']
    return render_template('dashboard.html', stats=stats, doses=doses[:10], today=today,
                           weekly=week, trend=trend_data(week, today - timedelta(days=6), today),
                           alerts=alert_rows(unresolved_only=True, limit=5), replies=replies, checkins=checkins,
                           messages=messages, failures=failures, dose_labels=DOSE_LABELS)


@bp.get('/patients')
@login_required
def patients():
    rows = all_rows('''SELECT p.*,
        (SELECT COUNT(*) FROM medicines m WHERE m.patient_id=p.id AND m.state='active') medicine_count,
        (SELECT COUNT(*) FROM alerts a WHERE a.patient_id=p.id AND a.acknowledged_at IS NULL) open_alerts,
        (SELECT COUNT(*) FROM doses d JOIN medicines m ON m.id=d.medicine_id WHERE m.patient_id=p.id
           AND d.status='taken') taken_count
      FROM patients p WHERE p.user_id=? ORDER BY p.active DESC,p.name''', (g.user['id'],))
    return render_template('patients.html', patients=rows)


@bp.route('/patients/new', methods=['GET', 'POST'])
@bp.route('/patients/<int:ident>/edit', methods=['GET', 'POST'])
@login_required
def patient_form(ident=None):
    p = patient_owned(ident) if ident else None
    if request.method == 'POST':
        try:
            values = patient_values(request.form)
            with transaction():
                if p:
                    get_db().execute('''UPDATE patients SET name=?,phone=?,timezone=?,contact_name=?,
                        contact_phone=? WHERE id=?''', values + (ident,))
                    # Unnotified future events are safe to regenerate after a timezone edit.
                    if values[2] != p['timezone']:
                        cancel_future(ident)
                else:
                    ident = get_db().execute('''INSERT INTO patients
                        (name,phone,timezone,contact_name,contact_phone,user_id,created_at)
                        VALUES (?,?,?,?,?,?,?)''', values + (g.user['id'], stamp())).lastrowid
            flash('Patient profile saved and linked to the responsible family contact.', 'success')
            return redirect(url_for('web.patient_detail', ident=ident))
        except ValidationError as e:
            flash(str(e), 'error')
        except sqlite3.IntegrityError:
            flash('This phone number is already registered. Use a different number.', 'error')
    form = request.form if request.method == 'POST' else (
        dict(p) if p else {'timezone': g.user['timezone'], 'contact_name': g.user['name'],
                           'contact_phone': g.user['family_phone']})
    return render_template('patient_form.html', patient=p, form=form)


def cancel_future(patient_id, medicine_id=None):
    db = get_db()
    meds = [medicine_id] if medicine_id else [m['id'] for m in all_rows('SELECT id FROM medicines WHERE patient_id=?', (patient_id,))]
    for mid in meds:
        db.execute('''DELETE FROM doses WHERE medicine_id=? AND scheduled_at>? AND notified_at IS NULL
            AND id NOT IN (SELECT entity_id FROM messages WHERE kind IN ('dose','followup','missed_followup'))''',
            (mid, stamp()))
    if not medicine_id:
        db.execute('''DELETE FROM checkins WHERE patient_id=? AND scheduled_at>? AND notified_at IS NULL
            AND id NOT IN (SELECT entity_id FROM messages WHERE kind='checkin')''', (patient_id, stamp()))


@bp.post('/patients/<int:ident>/active')
@login_required
def patient_active(ident):
    p = patient_owned(ident)
    with transaction():
        get_db().execute('UPDATE patients SET active=? WHERE id=?', (0 if p['active'] else 1, ident))
        if p['active']:
            get_db().execute("UPDATE doses SET source='schedule_changed_no_reminder' WHERE medicine_id IN "
                             "(SELECT id FROM medicines WHERE patient_id=?) AND notified_at IS NULL", (ident,))
            get_db().execute("UPDATE checkins SET source='schedule_changed_no_reminder' WHERE patient_id=? "
                             "AND notified_at IS NULL", (ident,))
            cancel_future(ident)
    flash('Profile status updated. Inactive profiles receive no new messages.', 'success')
    return redirect(url_for('web.patient_detail', ident=ident))


@bp.get('/patients/<int:ident>')
@login_required
def patient_detail(ident):
    p = patient_owned(ident)
    refresh_schedule()
    meds = all_rows('SELECT * FROM medicines WHERE patient_id=? ORDER BY id DESC', (ident,))
    checkins = all_rows('SELECT * FROM checkins WHERE patient_id=? ORDER BY scheduled_at DESC LIMIT 30', (ident,))
    replies = all_rows('SELECT * FROM reply_events WHERE patient_id=? ORDER BY id DESC LIMIT 30', (ident,))
    files = all_rows('SELECT * FROM uploads WHERE patient_id=? ORDER BY id DESC', (ident,))
    return render_template('patient_detail.html', patient=p, medicines=meds,
                           doses=records(patient_id=ident)[:100], checkins=checkins,
                           alerts=alert_rows(patient_id=ident, limit=30), replies=replies, files=files,
                           dose_labels=DOSE_LABELS)


@bp.get('/medicines')
@login_required
def medicines():
    rows = all_rows('''SELECT m.*,p.name patient FROM medicines m JOIN patients p ON p.id=m.patient_id
        WHERE p.user_id=? ORDER BY m.id DESC''', (g.user['id'],))
    return render_template('medicines.html', medicines=rows)


@bp.route('/patients/<int:patient_id>/medicines/new', methods=['GET', 'POST'])
@bp.route('/patients/<int:patient_id>/medicines/<int:ident>/edit', methods=['GET', 'POST'])
@login_required
def medicine_form(patient_id, ident=None):
    p = patient_owned(patient_id)
    m = one('SELECT * FROM medicines WHERE id=? AND patient_id=?', (ident, patient_id)) if ident else None
    if ident and not m:
        abort(404)
    if request.method == 'POST':
        try:
            values = medicine_values(request.form)
            with transaction():
                # Do not allow two overlapping schedules for the same entered medicine/dosage/time.
                new_times = set(json.loads(values[5]))
                for other in all_rows("SELECT * FROM medicines WHERE patient_id=? AND id!=? AND state!='inactive'",
                                      (patient_id, ident or 0)):
                    overlap = values[3] <= (other['end_date'] or '9999-12-31') and other['start_date'] <= (values[4] or '9999-12-31')
                    if (values[6] != 'inactive' and other['name'].casefold() == values[0].casefold()
                            and other['dosage'].casefold() == values[1].casefold() and overlap
                            and new_times.intersection(json.loads(other['times']))):
                        raise ValidationError('An overlapping schedule already exists for this medicine and '
                                              'dose time. Edit that schedule instead.')
                if m:
                    # Preserve what was entered when earlier dose events were generated.
                    get_db().execute('''UPDATE doses SET medicine_name=COALESCE(medicine_name,?),
                      dosage_text=COALESCE(dosage_text,?),instruction_text=COALESCE(instruction_text,?)
                      WHERE medicine_id=?''', (m['name'], m['dosage'], m['instructions'], ident))
                    get_db().execute("UPDATE doses SET source='schedule_changed_no_reminder' WHERE medicine_id=? "
                                     "AND scheduled_at<=? AND notified_at IS NULL", (ident, stamp()))
                    get_db().execute('''UPDATE medicines SET name=?,dosage=?,instructions=?,start_date=?,
                        end_date=?,times=?,state=?,fingerprint=? WHERE id=?''', values + (ident,))
                    cancel_future(patient_id, ident)
                else:
                    get_db().execute('''INSERT INTO medicines
                        (name,dosage,instructions,start_date,end_date,times,state,fingerprint,patient_id,created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)''', values + (patient_id, stamp()))
            flash('Reviewed medicine schedule saved.', 'success')
            return redirect(url_for('web.patient_detail', ident=patient_id))
        except ValidationError as e:
            flash(str(e), 'error')
        except sqlite3.IntegrityError:
            flash('An identical medicine schedule already exists. Edit the existing schedule.', 'error')
    form = request.form if request.method == 'POST' else (
        dict(m) if m else {'start_date': utc_now().astimezone(ZoneInfo(p['timezone'])).date().isoformat(), 'state': 'active'})
    if m and request.method == 'GET':
        form['times'] = ', '.join(json.loads(m['times']))
    return render_template('medicine_form.html', patient=p, medicine=m, form=form)


@bp.post('/doses/confirm')
@login_required
def confirm_selected_dose():
    try:
        ident = int(request.form.get('dose_id', ''))
    except ValueError:
        abort(400, description='Select a dose to confirm.')
    return confirm_dose(ident)


@bp.post('/doses/<int:ident>/confirm')
@login_required
def confirm_dose(ident):
    d = owned_dose(ident)
    with transaction():
        result = handle_reply(d['patient_id'], f'TAKEN D{ident}', f'family_user:{g.user["id"]}')
    flash(result, 'success')
    return redirect(url_for('web.patient_detail', ident=d['patient_id']))


@bp.get('/alerts')
@login_required
def alerts():
    rows = all_rows('''SELECT a.*,p.name patient FROM alerts a JOIN patients p ON p.id=a.patient_id
        WHERE a.user_id=? ORDER BY a.acknowledged_at IS NOT NULL, a.id DESC''', (g.user['id'],))
    return render_template('alerts.html', alerts=rows, labels=alert_policy.LABELS)


@bp.post('/alerts/<int:ident>/acknowledge')
@login_required
def acknowledge(ident):
    if not one('SELECT id FROM alerts WHERE id=? AND user_id=?', (ident, g.user['id'])):
        abort(404)
    get_db().execute('''UPDATE alerts SET acknowledged_at=COALESCE(acknowledged_at,?),
        acknowledged_by=COALESCE(acknowledged_by,?) WHERE id=?''', (stamp(), g.user['id'], ident))
    flash('Alert acknowledged. This records acknowledgement, not a clinical resolution.', 'success')
    return redirect(url_for('web.alerts'))


@bp.route('/prescriptions', methods=['GET', 'POST'])
@login_required
def prescriptions():
    patients = all_rows('SELECT * FROM patients WHERE user_id=? AND active=1 ORDER BY name', (g.user['id'],))
    if request.method == 'POST':
        try:
            try:
                pid = int(request.form.get('patient_id', '0'))
            except ValueError:
                raise ValidationError('Choose a patient.')
            patient_owned(pid)
            f = request.files.get('file')
            if not f or not f.filename:
                raise ValidationError('Choose a PDF, JPEG or PNG file.')
            name = secure_filename(f.filename)[:180] or 'prescription'
            ext = Path(name).suffix.lower()
            content = f.read(8 * 1024 * 1024 + 1)
            if not content or len(content) > 8 * 1024 * 1024:
                raise ValidationError('File must be non-empty and no larger than 8 MB.')
            if ext in ('.png', '.jpg', '.jpeg'):
                try:
                    with Image.open(io.BytesIO(content)) as image:
                        if image.format not in ('PNG', 'JPEG') or image.width * image.height > 25_000_000:
                            raise ValidationError('Use a PNG/JPEG image under 25 megapixels.')
                        image.verify()
                except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
                    raise ValidationError('The image is invalid or unsafe.')
            elif ext == '.pdf':
                try:
                    if not content.startswith(b'%PDF-'):
                        raise ValueError()
                    pdf = PdfReader(io.BytesIO(content), strict=True)
                    if pdf.is_encrypted or not 1 <= len(pdf.pages) <= 100:
                        raise ValueError()
                except Exception:
                    raise ValidationError('Use a readable, unencrypted PDF with 1–100 pages.')
            else:
                raise ValidationError('Only PDF, PNG and JPEG files are accepted.')
            storage = uuid.uuid4().hex + ext
            target = Path(current_app.config['UPLOAD_FOLDER']) / storage
            try:
                target.write_bytes(content)
                with transaction():
                    get_db().execute('''INSERT INTO uploads (patient_id,original_name,storage_name,size,created_at)
                        VALUES (?,?,?,?,?)''', (pid, name, storage, len(content), stamp()))
            except BaseException:
                target.unlink(missing_ok=True)
                raise
            flash('Stored privately. No OCR or AI is configured; enter and review the prescription manually. '
                  'Nothing has been prescribed or scheduled automatically.', 'success')
            return redirect(url_for('web.medicine_form', patient_id=pid))
        except ValidationError as e:
            flash(str(e), 'error')
    files = all_rows('''SELECT u.*,p.name patient FROM uploads u JOIN patients p ON p.id=u.patient_id
        WHERE p.user_id=? ORDER BY u.id DESC''', (g.user['id'],))
    return render_template('prescriptions.html', patients=patients, files=files)


@bp.get('/prescriptions/<int:ident>/download')
@login_required
def download_upload(ident):
    item = one('SELECT u.* FROM uploads u JOIN patients p ON p.id=u.patient_id WHERE u.id=? AND p.user_id=?',
               (ident, g.user['id']))
    if not item:
        abort(404)
    # UUID filename only; never use any user-supplied path.
    return send_file(Path(current_app.config['UPLOAD_FOLDER']) / item['storage_name'], as_attachment=True,
                     download_name=item['original_name'])


def filter_dates():
    today = utc_now().astimezone(ZoneInfo(g.user['timezone'])).date()
    try:
        start = date.fromisoformat(request.args.get('start', (today - timedelta(days=6)).isoformat()))
        end = date.fromisoformat(request.args.get('end', today.isoformat()))
        if end < start or (end - start).days > 366:
            raise ValueError()
        pid = int(request.args['patient_id']) if request.args.get('patient_id') else None
    except ValueError:
        raise ValidationError('Choose a valid date range up to 366 days and a valid patient.')
    return start, end, pid


def trend_data(rows, start, end):
    zone = ZoneInfo(g.user['timezone'])
    result = []
    day = start
    while day <= end:
        ds = [d for d in rows if parse(d['scheduled_at']).astimezone(zone).date() == day]
        result.append({'day': day.strftime('%d %b'), 'taken': sum(d['status'] == 'taken' for d in ds),
                       'total': len(ds), 'missing': sum(d['status'] == 'missed' for d in ds)})
        day += timedelta(days=1)
    return result


def filtered_checkins(start, end, pid):
    rows = all_rows('''SELECT c.*,p.name patient FROM checkins c JOIN patients p ON p.id=c.patient_id
        WHERE p.user_id=? ORDER BY c.scheduled_at DESC''', (g.user['id'],))
    zone = ZoneInfo(g.user['timezone'])
    return [c for c in rows
            if (not pid or c['patient_id'] == pid) and start <= parse(c['scheduled_at']).astimezone(zone).date() <= end]


@bp.get('/reports')
@login_required
def reports():
    refresh_schedule()
    try:
        start, end, pid = filter_dates()
    except ValidationError as e:
        flash(str(e), 'error')
        return redirect(url_for('web.reports'))
    rows = records(start, end, pid)
    stats = {s: sum(d['status'] == s for d in rows) for s in DOSE_LABELS}
    return render_template('reports.html', doses=rows, stats=stats, trend=trend_data(rows, start, end),
                           start=start, end=end, pid=pid, dose_labels=DOSE_LABELS,
                           patients=all_rows('SELECT * FROM patients WHERE user_id=?', (g.user['id'],)),
                           checkins=filtered_checkins(start, end, pid))


@bp.get('/reports.csv')
@login_required
def export_csv():
    try:
        start, end, pid = filter_dates()
    except ValidationError as e:
        abort(400, description=str(e))
    rows = records(start, end, pid)
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['type', 'patient', 'medicine', 'scheduled_utc', 'status_or_response', 'status_meaning',
                     'responded_utc', 'source'])

    def safe(v):
        s = str(v or '')
        return "'" + s if s.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else s

    for d in rows:
        writer.writerow([safe(v) for v in [
            'dose', d['patient'], d['medicine'], d['scheduled_at'], d['status'],
            ('Authorized family confirmation' if d['status']=='taken' and (d['source'] or '').startswith('family_user:') else DOSE_LABELS.get(d['status'], ('', ''))[1]), d['responded_at'], d['source']]])
    for c in filtered_checkins(start, end, pid):
        writer.writerow([safe(v) for v in ['checkin', c['patient'], '', c['scheduled_at'],
                                           c['response'] or 'no_reply', 'Recorded reply' if c['response']
                                           else 'No recorded reply', c['responded_at'], c['source']]])
    return Response('\ufeff' + output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename="sahayak-history.csv"'})


@bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        try:
            values = settings_values(request.form)
            alert_on_not_yet = 1 if request.form.get('alert_on_not_yet') else 0
            missed_followup = 1 if request.form.get('missed_followup') else 0
            with transaction():
                get_db().execute('''UPDATE users SET name=?,family_phone=?,timezone=?,response_minutes=?,
                    checkin_time=?,repeat_minutes=?,repeat_limit=?,escalation_minutes=?,reply_delay_minutes=?,
                    notifications=?,alert_on_not_yet=?,missed_followup=? WHERE id=?''',
                    values + (alert_on_not_yet, missed_followup, g.user['id']))
                for p in all_rows('SELECT id FROM patients WHERE user_id=?', (g.user['id'],)):
                    cancel_future(p['id'])
            flash('Settings saved. Existing sent messages keep their original response deadline.', 'success')
            return redirect(url_for('web.settings'))
        except ValidationError as e:
            flash(str(e), 'error')
    lease = one("SELECT * FROM leases WHERE name='scheduler'")
    worker_ok = bool(lease and parse(lease['expires_at']) >= utc_now() - timedelta(seconds=35))
    configured = provider_ready(current_app.config)
    return render_template('settings.html', configured=configured, worker_ok=worker_ok,
                           webhook_configured=webhook_ready(current_app.config),
                           in_app=current_app.config['SCHEDULER_IN_APP'],
                           form=request.form if request.method == 'POST' else dict(g.user))


@bp.get('/messages')
@login_required
def messages():
    """Message activity with patient, date and status filters (delivery failures included)."""
    where = ['m.user_id=?']
    args = [g.user['id']]
    pid = request.args.get('patient_id')
    if pid:
        if not one('SELECT id FROM patients WHERE id=? AND user_id=?', (pid, g.user['id'])):
            abort(404)
        where.append('m.patient_id=?')
        args.append(pid)
    state = request.args.get('status', '')
    if state == 'failed':
        where.append("(m.status IN ('failed','unknown') OR m.delivery_status IN ('failed','undelivered','canceled'))")
    elif state in ('simulated', 'accepted', 'in_app', 'queued', 'cancelled', 'sending', 'received', 'twiml_response'):
        where.append('m.status=?')
        args.append(state)
    zone = ZoneInfo(g.user['timezone'])
    start = end = None
    try:
        if request.args.get('start'):
            start = date.fromisoformat(request.args['start'])
        if request.args.get('end'):
            end = date.fromisoformat(request.args['end'])
    except ValueError:
        flash('Use valid filter dates.', 'error')
    rows = all_rows('''SELECT m.*,p.name patient FROM messages m LEFT JOIN patients p ON p.id=m.patient_id
        WHERE ''' + ' AND '.join(where) + ' ORDER BY m.id DESC LIMIT 400', args)
    if start:
        rows = [m for m in rows if parse(m['created_at']).astimezone(zone).date() >= start]
    if end:
        rows = [m for m in rows if parse(m['created_at']).astimezone(zone).date() <= end]
    return render_template('messages.html', messages=rows[:300], patients=all_rows(
        'SELECT * FROM patients WHERE user_id=? ORDER BY name', (g.user['id'],)),
        pid=pid, state=state, start=request.args.get('start', ''), end=request.args.get('end', ''),
        failures=one("SELECT COUNT(*) n FROM messages WHERE user_id=? AND (status IN ('failed','unknown') OR delivery_status IN ('failed','undelivered','canceled'))",
                     (g.user['id'],))['n'])


@bp.route('/demo', methods=['GET', 'POST'])
@login_required
def demo_page():
    """Local Demo Mode: every action writes real records, but no message ever leaves the PC."""
    if not current_app.config['DEMO_MODE']:
        abort(404)
    if request.method == 'POST':
        action = request.form.get('action')
        try:
            pid = int(request.form.get('patient_id', '0'))
            p = patient_owned(pid)
            if not p['active']:
                raise ValidationError('Reactivate this patient before simulating events.')
            if action == 'reminder':
                med = one("SELECT * FROM medicines WHERE patient_id=? AND state='active' ORDER BY id LIMIT 1", (pid,))
                if not med:
                    raise ValidationError('Add an active medicine schedule first.')
                with transaction():
                    # Explicit extra practice event; never changes an entered prescription.
                    # A free timestamp is chosen so repeated practice reminders cannot collide
                    # with the UNIQUE(medicine_id, scheduled_at) rule.
                    at = utc_now()
                    while one('SELECT id FROM doses WHERE medicine_id=? AND scheduled_at=?',
                              (med['id'], stamp(at))):
                        at = at - timedelta(seconds=1)
                    get_db().execute('''INSERT OR IGNORE INTO doses
                        (medicine_id,scheduled_at,is_demo,medicine_name,dosage_text,instruction_text)
                        VALUES (?,?,1,?,?,?)''', (med['id'], stamp(at), med['name'], med['dosage'], med['instructions']))
                    due_work(utc_now())
                dispatch()
                flash('Practice dose created and a SIMULATED reminder recorded. No real WhatsApp message was sent.',
                      'success')
            elif action == 'checkin':
                with transaction():
                    generate_events(user_id=g.user['id'])
                    c = one('SELECT * FROM checkins WHERE patient_id=? AND local_date=?',
                            (pid, utc_now().astimezone(ZoneInfo(p['timezone'])).date().isoformat()))
                    if c['response']:
                        raise ValidationError('Today’s check-in was already answered; its history is preserved.')
                    get_db().execute('UPDATE checkins SET scheduled_at=? WHERE id=?', (stamp(), c['id']))
                    due_work(utc_now())
                dispatch()
                flash('Today’s check-in simulated. Use its C code below.', 'success')
            elif action == 'reply':
                with transaction():
                    reply = handle_reply(pid, request.form.get('body', '')[:500], 'demo_patient')
                dispatch()
                flash(reply, 'success')
            elif action in ('taken', 'not_yet', 'later'):
                # One-click reply buttons: the code is taken from the chosen dose.
                ident = int(request.form.get('dose_id', '0'))
                d = owned_dose(ident)
                if d['patient_id'] != pid:
                    raise ValidationError('Choose a dose that belongs to this patient.')
                body = {'taken': f'TAKEN D{ident}', 'not_yet': f'NOT YET D{ident}',
                        'later': f'REMIND ME LATER D{ident}'}[action]
                with transaction():
                    reply = handle_reply(pid, body, 'demo_patient')
                dispatch()
                flash(reply, 'success')
            elif action in ('timeout', 'expire'):
                ident = int(request.form.get('dose_id', '0'))
                d = owned_dose(ident)
                if d['patient_id'] != pid or d['status'] == 'taken':
                    raise ValidationError('Choose an unanswered dose belonging to this patient.')
                if not d['notified_at']:
                    raise ValidationError('Send the reminder first, then expire its response window.')
                previous = one('SELECT * FROM users WHERE id=?', (g.user['id'],))
                with transaction():
                    # Move the deadline and the escalation clock into the past, then run the scheduler.
                    g_db = get_db()
                    g_db.execute('UPDATE doses SET expected_at=?,escalated_at=NULL WHERE id=?',
                                 (stamp(utc_now() - timedelta(minutes=1) - timedelta(minutes=previous['escalation_minutes'])),
                                  ident))
                    due_work(utc_now())
                dispatch()
                flash('SIMULATED timeout: the response window closed with no reply, so the family alert '
                      'was created and its notification was simulated. No reply is not proof the medicine '
                      'was not taken.', 'success')
            elif action == 'escalate':
                ident = int(request.form.get('dose_id', '0'))
                d = owned_dose(ident)
                if d['patient_id'] != pid:
                    raise ValidationError('Choose a dose belonging to this patient.')
                previous = one('SELECT * FROM users WHERE id=?', (g.user['id'],))
                with transaction():
                    get_db().execute('UPDATE doses SET expected_at=? WHERE id=?',
                                     (stamp(utc_now() - timedelta(minutes=1) - timedelta(minutes=previous['escalation_minutes'])),
                                      ident))
                    due_work(utc_now())
                dispatch()
                flash('SIMULATED escalation window: the responsible family alert was created and simulated.',
                      'success')
            elif action == 'failure':
                ident = int(request.form.get('dose_id', '0'))
                d = owned_dose(ident)
                if d['patient_id'] != pid:
                    raise ValidationError('Choose a dose belonging to this patient.')
                with transaction():
                    now = utc_now()
                    get_db().execute("""INSERT OR IGNORE INTO messages
                        (user_id,patient_id,dedupe_key,kind,entity_id,recipient,body,transport,status,attempts,
                         next_at,created_at,error_code)
                        VALUES (?,?,?,'demo_failure',?,?,?,'demo','failed',1,?,?,'simulated_provider_failure')""",
                        (g.user['id'], pid, f'failure-demo:{pid}:{stamp(now)}', d['id'], p['phone'],
                         'SIMULATED provider failure, not a real WhatsApp send. Original dose is unchanged.',
                         stamp(now), stamp(now)))
                flash('SIMULATED provider failure recorded. Review the failed message in Message activity.', 'success')
            else:
                raise ValidationError('Choose a valid simulation action.')
        except (ValidationError, ValueError) as e:
            flash(str(e), 'error')
        return redirect(url_for('web.demo_page'))
    refresh_schedule()
    now_open = [d for d in records() if d['scheduled_at'] <= stamp() and d['status'] != 'taken']
    return render_template('demo.html',
                           patients=all_rows('SELECT * FROM patients WHERE user_id=? AND active=1', (g.user['id'],)),
                           doses=now_open[:30], dose_labels=DOSE_LABELS,
                           checkins=all_rows('''SELECT c.*,p.name patient FROM checkins c JOIN patients p ON p.id=c.patient_id
                               WHERE p.user_id=? AND c.notified_at IS NOT NULL ORDER BY c.id DESC LIMIT 20''',
                               (g.user['id'],)))


@bp.post('/scheduler/tick')
@login_required
def manual_tick():
    """Run one scheduler beat now, so a demo never depends on a background process."""
    if not current_app.config['DEMO_MODE']:
        abort(403, description='Live scheduling runs in the dedicated worker, not through a browser.')
    changed = tick()
    if changed:
        flash('Scheduler beat completed: due reminders, windows and alerts were processed.', 'success')
    else:
        flash('Scheduler beat skipped because another worker holds the lease (this prevents duplicates).', 'success')
    return redirect(url_for('web.dashboard'))


@bp.get('/health')
def health():
    one('SELECT 1')
    lease = one("SELECT * FROM leases WHERE name='scheduler'")
    return {'status': 'ok', 'database': 'reachable', 'demo_mode': current_app.config['DEMO_MODE'],
            'scheduler_recent': bool(lease and parse(lease['expires_at']) >= utc_now() - timedelta(seconds=35))}
