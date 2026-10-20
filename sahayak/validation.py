"""Strict user input validation; no prescription interpretation or dosage recommendations."""
import hashlib
import json
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import phonenumbers


class ValidationError(ValueError):
    pass


def text(value, label, maximum=120, required=True):
    value = (value or '').strip()
    if required and not value:
        raise ValidationError(f'{label} is required.')
    if len(value) > maximum:
        raise ValidationError(f'{label} must be at most {maximum} characters.')
    return value


def phone(value, required=True):
    value = (value or '').strip()
    if not value and not required:
        return ''
    try:
        if not value.startswith('+'):
            raise ValueError()
        parsed = phonenumbers.parse(value, None)
        # Possible rather than allocated/valid: supports explicitly fictional demo numbers.
        if not phonenumbers.is_possible_number(parsed):
            raise ValueError()
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except (ValueError, phonenumbers.NumberParseException):
        raise ValidationError('Use an international phone number, for example +12025550101.')


def tz(value):
    try:
        ZoneInfo(value)
        return value
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValidationError('Choose a valid IANA timezone, for example Asia/Kolkata.')


def clock(value):
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value or ''):
        raise ValidationError('Times must use 24-hour HH:MM, for example 08:30.')
    return value


def patient_values(form):
    """Name, WhatsApp number, timezone and the responsible family contact.

    The contact is required so a patient can never be left without a person to alert, and the
    contact phone is validated exactly like the patient phone.
    """
    return (text(form.get('name'), 'Patient name'), phone(form.get('phone')),
            tz(form.get('timezone', '')), text(form.get('contact_name'), 'Family contact name'),
            phone(form.get('contact_phone')))


def flag(form, name, default=False):
    value = form.get(name)
    if value is None:
        return default
    return value in ('1', 'on', 'true', 'yes')


def medicine_values(form):
    name = text(form.get('name'), 'Medicine name')
    dosage = text(form.get('dosage'), 'Prescribed dosage text', 240)
    instructions = text(form.get('instructions'), 'Instructions', 1000, False)
    try:
        start = date.fromisoformat(form.get('start_date', ''))
        end = date.fromisoformat(form['end_date']) if form.get('end_date') else None
    except (ValueError, TypeError):
        raise ValidationError('Enter valid start and end dates.')
    if end and end < start:
        raise ValidationError('End date cannot be before start date.')
    times = [clock(t.strip()) for t in form.get('times', '').split(',')]
    if len(times) > 8 or len(set(times)) != len(times):
        raise ValidationError('Use one to eight different reminder times; duplicates are not allowed.')
    state = form.get('state', 'active')
    if state not in ('active', 'paused', 'inactive'):
        raise ValidationError('Invalid schedule state.')
    if form.get('confirmed') != 'yes':
        raise ValidationError('Confirm that you reviewed the entered prescription and schedule.')
    times_json = json.dumps(sorted(times))
    fingerprint = hashlib.sha256(json.dumps(
        [name.casefold(), dosage.casefold(), str(start), str(end), sorted(times)]).encode()).hexdigest()
    return (name, dosage, instructions, str(start), str(end) if end else None, times_json, state, fingerprint)


def settings_values(form):
    """Family alert policy: response window, escalation window and reminder behaviour.

    Windows may be omitted by a caller (an older form, or a script) and then take their
    safe default instead of raising.
    """
    def number(name, default):
        value = form.get(name)
        return default if value in (None, '') else value
    values = (text(form.get('name'), 'Family name'), phone(form.get('family_phone'), False),
              tz(form.get('timezone')), integer(number('response_minutes', 60), 'Response window', 5, 1440),
              clock(form.get('checkin_time')), integer(number('repeat_minutes', 15), 'Follow-up interval', 1, 240),
              integer(number('repeat_limit', 1), 'Automatic follow-ups', 0, 3),
              integer(number('escalation_minutes', 0), 'Family escalation window', 0, 1440),
              integer(number('reply_delay_minutes', 30), 'REMIND ME LATER default delay', 5, 240),
              form.get('notifications'))
    if values[-1] not in ('local', 'whatsapp'):
        raise ValidationError('Choose in-app or WhatsApp notifications.')
    if values[-1] == 'whatsapp' and not values[1]:
        raise ValidationError('Family phone number is required for WhatsApp notifications.')
    if values[6] and values[5] >= values[3]:
        raise ValidationError('Repeat interval must be shorter than response window.')
    return values


def integer(value, label, low, high):
    try:
        value = int(value)
    except (ValueError, TypeError):
        raise ValidationError(f'{label} must be a number.')
    if not low <= value <= high:
        raise ValidationError(f'{label} must be between {low} and {high}.')
    return value


def utc_now():
    return datetime.now(timezone.utc)


def stamp(dt=None):
    return (dt or utc_now()).astimezone(timezone.utc).isoformat(timespec='seconds')


def parse(value):
    return datetime.fromisoformat(value)


def local_instant(day, at, zone):
    """First fold on fall-back; nonexistent spring-forward time moves to next valid wall time."""
    naive = datetime.combine(day, datetime.strptime(at, '%H:%M').time())
    aware = naive.replace(tzinfo=ZoneInfo(zone), fold=0)
    utc = aware.astimezone(timezone.utc)
    # Round trip normalizes DST gaps deterministically; duplicate folds never create two doses.
    return utc
