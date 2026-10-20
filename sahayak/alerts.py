"""Family alert policy: which family contact is notified, and when.

One patient is linked to one responsible family contact (patients.contact_phone). The account
owner's number is only a fallback, so an alert can never be routed to another family's number.
"""

MISSED_RESPONSE = 'missed_response'
NOT_YET = 'not_yet'
MISSED_CHECKIN = 'missed_checkin'
CONCERNING_HEALTH = 'concerning_health'
CHECKIN = 'checkin'
CONFIRMATION = 'confirmation'

LABELS = {
    MISSED_RESPONSE: 'No reply to a medicine reminder',
    NOT_YET: 'Patient replied NOT YET, so the dose is still open',
    MISSED_CHECKIN: 'No reply to a daily check-in',
    CONCERNING_HEALTH: 'Patient reported feeling worse',
    CHECKIN: 'Daily check-in reply',
    CONFIRMATION: 'Family confirmation recorded',
}

# Wording required so a missing reply is never presented as a missed dose.
NO_REPLY_MEANING = 'Missing reply is not proof that the medicine was not taken.'


def recipient_for(user, patient):
    """The responsible family member for this patient; account number is the fallback."""
    return (patient['contact_phone'] or user['family_phone'] or '').strip()


def alert_key(kind, entity_id):
    """Stable dedupe key. Missed-response keeps the historical 'dose:<id>' form."""
    if kind == MISSED_RESPONSE:
        return f'dose:{entity_id}'
    if kind == NOT_YET:
        return f'dose:{entity_id}:not_yet'
    return f'{kind}:{entity_id}'


def should_alert_on_not_yet(user):
    """Configured policy: is a NOT YET reply escalated to the family straight away?"""
    return bool(user['alert_on_not_yet'])
