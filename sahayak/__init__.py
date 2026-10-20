import os
import secrets
from datetime import timedelta
from pathlib import Path

import click
from dotenv import load_dotenv
from flask import Flask, g, render_template, session
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash

from .db import close_db, get_db, init_db, one, transaction
from .validation import stamp

csrf = CSRFProtect()
SCHEDULER_TICK_SECONDS = 15


def create_app(test_config=None):
    load_dotenv()
    app = Flask(__name__, instance_relative_config=True)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    app.config.from_mapping(
        DATABASE=os.getenv('DATABASE_PATH', str(Path(app.instance_path) / 'sahayak.sqlite3')),
        UPLOAD_FOLDER=str(Path(app.instance_path) / 'uploads'),
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
        DEMO_MODE=os.getenv('DEMO_MODE', 'true').lower() == 'true',
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=os.getenv('COOKIE_SECURE', 'false').lower() == 'true',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        TWILIO_ACCOUNT_SID=os.getenv('TWILIO_ACCOUNT_SID', ''),
        TWILIO_AUTH_TOKEN=os.getenv('TWILIO_AUTH_TOKEN', ''),
        TWILIO_WHATSAPP_FROM=os.getenv('TWILIO_WHATSAPP_FROM', ''),
        PUBLIC_BASE_URL=os.getenv('PUBLIC_BASE_URL', '').rstrip('/'),
        WTF_CSRF_TIME_LIMIT=3600,
        # Off by default: run the scheduler as its own process so a web worker restart can
        # never create a second scheduler. Set to true only if you must run a single window.
        SCHEDULER_IN_APP=os.getenv('SCHEDULER_IN_APP', 'false').lower() == 'true',
        SCHEDULER_TICK_SECONDS=int(os.getenv('SCHEDULER_TICK_SECONDS', SCHEDULER_TICK_SECONDS)),
    )
    if test_config:
        app.config.update(test_config)
    secret = os.getenv('SECRET_KEY')
    if not secret:
        secret_path = Path(app.instance_path) / 'secret.key'
        try:
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(secrets.token_hex(32))
        except FileExistsError:
            pass
        secret = secret_path.read_text().strip()
    app.config.setdefault('SECRET_KEY', secret)
    if not app.config['SECRET_KEY']:
        app.config['SECRET_KEY'] = secret
    Path(app.config['UPLOAD_FOLDER']).mkdir(parents=True, exist_ok=True)
    csrf.init_app(app)
    app.teardown_appcontext(close_db)

    @app.before_request
    def load_user():
        g.user = one('SELECT * FROM users WHERE id=?', (session['user_id'],)) if session.get('user_id') else None

    @app.context_processor
    def context():
        return dict(demo=app.config['DEMO_MODE'])

    @app.template_filter('localtime')
    def localtime(value, zone=None):
        if not value:
            return '—'
        from zoneinfo import ZoneInfo
        from .validation import parse
        zone = zone or (g.user['timezone'] if getattr(g, 'user', None) else 'UTC')
        return parse(value).astimezone(ZoneInfo(zone)).strftime('%d %b %Y, %H:%M')

    @app.template_filter('justtime')
    def justtime(value, zone=None):
        if not value:
            return '—'
        from zoneinfo import ZoneInfo
        from .validation import parse
        zone = zone or (g.user['timezone'] if getattr(g, 'user', None) else 'UTC')
        return parse(value).astimezone(ZoneInfo(zone)).strftime('%H:%M')

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'none'; object-src 'none'")
        response.headers['Cache-Control'] = 'no-store'
        return response

    from .web import bp
    from .webhooks import hooks
    app.register_blueprint(bp)
    app.register_blueprint(hooks)
    csrf.exempt(hooks)  # Provider routes validate Twilio signatures, not browser CSRF tokens.

    for code in (400, 403, 404, 413, 429, 500):
        app.register_error_handler(code, lambda error: (render_template('error.html', error=error), error.code))

    @app.cli.command('init-db')
    def init_command():
        init_db()
        click.echo('Database ready. Existing data preserved.')

    @app.cli.command('create-user')
    @click.option('--email', prompt=True)
    @click.option('--name', prompt=True)
    @click.option('--password', prompt=True, hide_input=True, confirmation_prompt=True)
    def create_user(email, name, password):
        from .web import validate_account
        email, name, password = validate_account(email, name, password)
        try:
            with transaction():
                get_db().execute('INSERT INTO users (email,name,password_hash) VALUES (?,?,?)',
                                 (email, name, generate_password_hash(password)))
        except Exception:
            raise click.ClickException('Could not create user. Email may already exist.')
        click.echo('Family account created.')

    @app.cli.command('seed-demo')
    def seed_command():
        from .seed import seed_demo
        seed_demo()
        click.echo('Fictional demo data is present (created once).')

    @app.cli.command('tick')
    def tick_command():
        """Run exactly one scheduler beat - useful for a quick manual check."""
        from .services import tick
        changed = tick()
        click.echo('Scheduler beat ran.' if changed else 'Scheduler beat skipped: another worker holds the lease.')

    @app.cli.command('worker')
    def worker():
        import time
        from .services import tick
        click.echo('SAHAYAK scheduler running. Ctrl+C stops it. Keep this terminal open.')
        while True:
            try:
                tick()
            except Exception as exc:
                app.logger.error('Scheduler error (%s); details omitted for privacy.', type(exc).__name__)
            time.sleep(app.config['SCHEDULER_TICK_SECONDS'])

    @app.cli.command('check-config')
    def check_config():
        """Explain which features are simulated and which use real WhatsApp."""
        from .providers import provider_ready, webhook_ready
        mode = 'DEMO MODE' if app.config['DEMO_MODE'] else 'LIVE MODE'
        ready = provider_ready(app.config)
        click.echo(f'Run mode: {mode}')
        click.echo(f'Outbound WhatsApp configuration valid: {"yes" if ready else "no"}')
        click.echo(f'HTTPS webhook/callback configuration valid: {"yes" if webhook_ready(app.config) else "no"}')
        click.echo('Demo Mode works without any credentials or paid service.')
        if not app.config['DEMO_MODE'] and not ready:
            click.echo('LIVE MODE WITHOUT VALID OUTBOUND CONFIG: messages will be recorded as failed, not sent.')
        click.echo(f'Scheduler inside the web process: {"on" if app.config["SCHEDULER_IN_APP"] else "off"}')

    with app.app_context():
        init_db()
        if app.config['DEMO_MODE'] and not app.config.get('TESTING'):
            from .seed import seed_demo
            seed_demo()
    if app.config['SCHEDULER_IN_APP'] and not app.config.get('TESTING'):
        _start_in_app_scheduler(app)
    return app


def _start_in_app_scheduler(app):
    """Optional single-window scheduler.

    Guards: never starts inside the Werkzeug reloader child, and the database lease means that
    even two accidentally started schedulers cannot both do work in the same beat.
    """
    import os as _os
    import threading as _threading
    import time as _time

    if app.debug and _os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        return  # parent reloader process: do not start a second scheduler

    def loop():
        from .services import tick
        while True:
            with app.app_context():
                try:
                    tick()
                except Exception as exc:
                    app.logger.error('Scheduler error (%s); details omitted for privacy.', type(exc).__name__)
            _time.sleep(app.config['SCHEDULER_TICK_SECONDS'])

    _threading.Thread(target=loop, name='sahayak-scheduler', daemon=True).start()
    app.logger.info('In-app scheduler started (database lease prevents duplicate work).')
