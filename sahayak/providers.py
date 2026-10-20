"""Delivery transports, provider behaviour and the public feature matrix.

There are exactly three transports:
  demo      -> nothing leaves the machine; the outbox row is marked "simulated"
  local     -> the notification is stored for the family to read inside the app
  whatsapp  -> a real Twilio WhatsApp request (needs credentials in .env)

Keeping this in one place is what lets Demo Mode work with no API keys while the same
scheduling, alert and dashboard code runs unchanged.
"""
import re
from urllib.parse import urlsplit


DEMO = 'demo'
LOCAL = 'local'
WHATSAPP = 'whatsapp'

MODE_LABELS = {
    DEMO: 'Simulated (local demo)',
    LOCAL: 'In-app notification',
    WHATSAPP: 'WhatsApp via Twilio',
}

# Outbox states that mean "the message was handed over successfully".
DELIVERED_STATES = ('accepted', 'simulated', 'in_app')

# Outbox states that mean a human must look at the record.
FAILED_STATES = ('failed', 'unknown')

FEATURE_MATRIX = (
    ('Add patients and medicine schedules', 'Works fully', 'Works fully'),
    ('Time zone aware reminder scheduling', 'Works fully', 'Works fully'),
    ('Patient replies TAKEN / NOT YET / REMIND ME LATER',
     'Simulated from the Demo simulator page', 'Real WhatsApp reply via the Twilio webhook'),
    ('Family alert when a reply never arrives',
     'Simulated and stored as an alert', 'Real WhatsApp alert to the family number'),
    ('Dashboard, history, CSV export, reports', 'Works fully', 'Works fully'),
    ('Message delivery status (delivered / failed)',
     'Simulated status only, no delivery receipts', 'Twilio status callbacks recorded'),
    ('Provider failure handling', 'Can be simulated on purpose', 'Real API errors recorded and retried safely'),
)


def resolve_transport(demo_mode, notifications, family=False):
    """Pick the transport for a new outbox row."""
    if demo_mode:
        return DEMO
    if family and notifications == 'local':
        return LOCAL
    return WHATSAPP


def simulated_state(transport):
    """The outbox status a fake delivery gets, so records never claim a real send."""
    return 'simulated' if transport == DEMO else 'in_app'


def provider_ready(config):
    """True only when the outbound Twilio credentials and sender are configured."""
    sid = (config.get('TWILIO_ACCOUNT_SID') or '').strip()
    token = (config.get('TWILIO_AUTH_TOKEN') or '').strip()
    sender = (config.get('TWILIO_WHATSAPP_FROM') or '').strip()
    return (bool(re.fullmatch(r'AC[0-9a-fA-F]{32}', sid)) and bool(token)
            and bool(re.fullmatch(r'whatsapp:\+[1-9][0-9]{1,14}', sender)))


def webhook_ready(config):
    """True when Twilio can sign inbound requests and reach HTTPS callback URLs."""
    token = (config.get('TWILIO_AUTH_TOKEN') or '').strip()
    base = (config.get('PUBLIC_BASE_URL') or '').strip()
    try:
        parsed = urlsplit(base)
        valid_url = (parsed.scheme == 'https' and bool(parsed.hostname)
                     and parsed.username is None and parsed.password is None
                     and parsed.path in ('', '/') and not parsed.query and not parsed.fragment)
    except ValueError:
        valid_url = False
    return bool(token) and valid_url
