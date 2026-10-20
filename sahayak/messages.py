"""Every outbound message text lives here so wording can be reviewed in one place.

Safety rules encoded in this module:
* A missing reply is never described as a missed dose.
* Only the dosage/instructions entered and confirmed by the family are ever repeated.
* SAHAYAK never diagnoses, never advises a dose change and never claims emergency cover.
"""
from zoneinfo import ZoneInfo

from .validation import parse

EMERGENCY_NOTE = ('SAHAYAK is a family care aid, not an emergency service. '
                  'For urgent danger, call your local emergency number.')


def _pick(row, *keys, default=''):
    """Read the first non-empty value from a sqlite3.Row or dict."""
    for key in keys:
        try:
            value = row[key]
        except (IndexError, KeyError, TypeError):
            continue
        if value:
            return value
    return default


def readable_time(value, zone):
    return parse(value).astimezone(ZoneInfo(zone)).strftime('%d %b %Y, %H:%M')


def _dose_fields(dose):
    return (_pick(dose, 'medicine_name', 'name', default='your medicine'),
            _pick(dose, 'dosage_text', 'dosage', default=''),
            _pick(dose, 'instruction_text', 'instructions', default=''),
            dose['id'], dose['scheduled_at'])


def dose_reminder(patient, dose):
    """The scheduled medicine reminder sent to the patient's WhatsApp number."""
    medicine, dosage, instructions, ident, when = _dose_fields(dose)
    when = readable_time(when, patient['timezone'])
    lines = [f"Namaste! Aapki {medicine} lene ka samay ho gaya hai ({when}, {patient['timezone']})."]
    if dosage:
        lines.append(f'Entered dosage: {dosage}.')
    if instructions:
        lines.append(instructions.strip())
    lines += ['Kya aapne dawa le li hai? Kripya TAKEN ya NOT YET reply karein.',
              'Baad me yaad dilane ke liye REMIND ME LATER reply karein.',
              f'Dose code: D{ident}. Ek se zyada dawa waiting ho to code likhein, jaise TAKEN D{ident}.',
              'Sirf sach me dawa lene par TAKEN reply karein.',
              EMERGENCY_NOTE]
    return ' '.join(lines)


def missed_followup(patient, dose):
    """The single optional follow-up sent when the response window closes unanswered."""
    medicine, _, _, ident, when = _dose_fields(dose)
    when = readable_time(when, patient['timezone'])
    return (f'SAHAYAK follow-up: Aapne {medicine} ({when}) ke baare me abhi reply nahi kiya. '
            f'Kya aapne dawa le li hai? Kripya TAKEN ya NOT YET reply karein. Dose code: D{ident}. '
            f'Sirf sach me dawa lene par TAKEN reply karein. {EMERGENCY_NOTE}')


def requested_reminder(patient, dose, delay_minutes):
    """The actual reminder delivered AFTER the patient's requested delay, not a new promise."""
    medicine, dosage, instructions, ident, when = _dose_fields(dose)
    when = readable_time(when, patient['timezone'])
    details = f'Entered dosage: {dosage}. {instructions}'
    return (f'SAHAYAK requested follow-up: {medicine}, scheduled for {when}. '
            f'{details} Aapne baad me yaad dilane ko kaha tha. Kya aapne dawa le li hai? '
            f'Reply TAKEN D{ident} only if taken, or NOT YET D{ident}. {EMERGENCY_NOTE}')


def late_reminder(patient, dose):
    """Patient asked for a reminder but gave no usable delay; the configured default is used."""
    medicine, _, _, ident, _ = _dose_fields(dose)
    return (f'SAHAYAK: {medicine} ke liye follow-up reminder set kiya gaya hai. '
            f'Jab dawa le lein to TAKEN reply karein. Dose code: D{ident}. {EMERGENCY_NOTE}')


def family_missed_alert(patient, dose):
    """Case B: no reply inside the escalation window."""
    medicine, _, _, ident, when = _dose_fields(dose)
    when = readable_time(when, patient['timezone'])
    return (f'SAHAYAK Alert: {patient["name"]} has not responded to the {medicine} reminder '
            f'scheduled for {when}. Please check with them. '
            f'(Dose code D{ident}. No reply does not prove that the medicine was not taken, '
            f'and this is not confirmation of an emergency.)')


def family_not_yet_alert(patient, dose):
    """Case A: the patient replied NOT YET, so the dose is unresolved."""
    medicine, _, _, ident, when = _dose_fields(dose)
    when = readable_time(when, patient['timezone'])
    return (f'SAHAYAK Alert: {patient["name"]} replied NOT YET for the {medicine} dose '
            f'scheduled for {when}. Please check with them. '
            f'(Dose code D{ident}. This records the patient reply; it is not a confirmed missed dose.)')


def checkin_question(patient, checkin):
    ident = checkin['id']
    return (f'SAHAYAK: How are you feeling today? Reply BETTER C{ident}, SAME C{ident}, '
            f'WORSE C{ident} or NEED HELP C{ident}. Not an emergency service.')


def family_checkin_alert(patient, checkin):
    return (f'SAHAYAK Alert: No well-being reply received from {patient["name"]} for check-in '
            f'C{checkin["id"]}. Please check on them. This is not proof of an emergency.')


def family_concerning_alert(patient, checkin, action):
    return (f'SAHAYAK Alert: {patient["name"]} replied {action} to check-in C{checkin["id"]}. '
            f'Please check on them. SAHAYAK cannot diagnose or provide emergency response. '
            f'For urgent danger, call your local emergency number.')


def unmatched_sender():
    return ('SAHAYAK cannot match this care interaction. Please contact your family coordinator. '
            'For urgent help, call your local emergency number.')
