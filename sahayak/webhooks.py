"""Twilio inbound WhatsApp webhook and delivery-status callbacks.

Security model: the provider routes are the only CSRF-exempt routes, and they are exempt only
because every request must carry a valid Twilio signature. Without configured credentials the
routes refuse to run (503) instead of silently accepting unsigned patient messages.
"""
import re
from functools import wraps

from flask import Blueprint, Response, abort, current_app, request
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from . import messages as message_text
from .db import get_db, one, transaction
from .providers import webhook_ready
from .services import handle_reply
from .validation import stamp

hooks = Blueprint('hooks', __name__)

MAX_WEBHOOK_BYTES = 16384


def signed(fn):
    """Reject anything that is not a correctly signed Twilio request."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        token = (current_app.config.get('TWILIO_AUTH_TOKEN') or '').strip()
        base = (current_app.config.get('PUBLIC_BASE_URL') or '').strip()
        if not webhook_ready(current_app.config):
            abort(503, description='WhatsApp webhook is not configured.')
        if len(request.get_data(cache=True)) > MAX_WEBHOOK_BYTES:
            abort(413)
        # Use the configured public URL, never an untrusted forwarded header or Host value.
        url = base.rstrip('/') + request.path
        if request.query_string:
            url += '?' + request.query_string.decode('ascii')
        if not RequestValidator(token).validate(url, request.form, request.headers.get('X-Twilio-Signature', '')):
            abort(403)
        return fn(*args, **kwargs)
    return wrapped


@hooks.post('/webhooks/twilio')
@signed
def inbound():
    """A patient reply arrives here. Repeat deliveries of the same MessageSid are ignored."""
    sid = request.form.get('MessageSid', '')
    if not re.fullmatch(r'SM[0-9a-fA-F]{32}', sid):
        abort(400)
    sender = request.form.get('From', '').removeprefix('whatsapp:')
    body = request.form.get('Body', '')[:1000]
    response = MessagingResponse()
    with transaction():
        if one('SELECT sid FROM inbound WHERE sid=?', (sid,)):
            # Duplicate webhook delivery: answer politely, change nothing.
            return Response(str(response), mimetype='application/xml')
        p = one('SELECT * FROM patients WHERE phone=?', (sender,))
        get_db().execute('INSERT INTO inbound VALUES (?,?,?)', (sid, p['id'] if p else None, stamp()))
        if not p:
            # Avoid confirming whether a phone number or profile exists.
            response.message(message_text.unmatched_sender())
        else:
            get_db().execute('''INSERT INTO messages
                (user_id,patient_id,dedupe_key,direction,kind,recipient,body,transport,status,next_at,created_at,provider_sid)
                VALUES (?,?,?,'inbound','reply',?,?,'whatsapp','received',?,?,?)''',
                (p['user_id'], p['id'], 'inbound:' + sid, sender, body, stamp(), stamp(), sid))
            reply = handle_reply(p['id'], body, 'whatsapp')
            response.message(reply)
            get_db().execute('''INSERT INTO messages
                (user_id,patient_id,dedupe_key,kind,recipient,body,transport,status,next_at,created_at)
                VALUES (?,?,?,'reply_ack',?,?,'whatsapp','twiml_response',?,?)''',
                (p['user_id'], p['id'], 'ack:' + sid, sender, reply, stamp(), stamp()))
    return Response(str(response), mimetype='application/xml')


@hooks.post('/webhooks/twilio/status')
@signed
def status():
    """Delivery status callbacks. Out-of-order callbacks never downgrade a delivered message."""
    sid = request.form.get('MessageSid', '')
    state = request.form.get('MessageStatus', '')
    if state not in ('queued', 'sending', 'sent', 'delivered', 'undelivered', 'failed', 'read',
                     'accepted', 'scheduled', 'canceled'):
        abort(400)
    if not re.fullmatch(r'SM[0-9a-fA-F]{32}', sid):
        abort(400)
    error = request.form.get('ErrorCode', '')
    error = error if error.isdigit() and len(error) < 10 else ''
    with transaction():
        get_db().execute('INSERT OR IGNORE INTO provider_callbacks VALUES (?,?,?,?)',
                         (sid,state,error,stamp()))
        from .callbacks import apply_callbacks
        apply_callbacks(sid)
    return '', 204
