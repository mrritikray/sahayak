"""Patient reply vocabulary and parsing.

Patients can reply with plain words (TAKEN / NOT YET / REMIND ME LATER) or add the dose
code from their reminder (TAKEN D12). Parsing is deliberately conservative: an unclear or
conflicting message never marks a dose as taken, and "NOT TAKEN" is never read as "TAKEN".
"""
import re

ACTIONS = (
    ('REMIND ME LATER', 'LATER', 'dose'),
    ('PHIR YAAD DILANA', 'LATER', 'dose'),
    ('THODI DER BAAD', 'LATER', 'dose'),
    ('REMIND LATER', 'LATER', 'dose'),
    ('REMIND ME', 'LATER', 'dose'),
    ('REMIND', 'LATER', 'dose'),
    ('LATER', 'LATER', 'dose'),
    ('NOT TAKEN', 'NOT_YET', 'dose'),
    ('NOT YET', 'NOT_YET', 'dose'),
    ('NOTYET', 'NOT_YET', 'dose'),
    ('ABHI NAHI', 'NOT_YET', 'dose'),
    ('NAHI LIYA', 'NOT_YET', 'dose'),
    ('NAHI LI', 'NOT_YET', 'dose'),
    ('DAWA LE LI', 'TAKEN', 'dose'),
    ('DAWAI LE LI', 'TAKEN', 'dose'),
    ('LE LIYA', 'TAKEN', 'dose'),
    ('LELIYA', 'TAKEN', 'dose'),
    ('TAKEN', 'TAKEN', 'dose'),
    ('TOOK', 'TAKEN', 'dose'),
    ('NEED HELP', 'NEED_HELP', 'checkin'),
    ('BETTER', 'BETTER', 'checkin'),
    ('SAME', 'SAME', 'checkin'),
    ('WORSE', 'WORSE', 'checkin'),
    ('OKAY', 'SAME', 'checkin'),
)

CODE = re.compile(r'(?<![A-Z0-9])([DC])\s?(\d{1,7})(?![0-9])')
NUMBER = re.compile(r'(?<![A-Z0-9])(\d{1,3})\s*(?:MIN|MINS|MINUTE|MINUTES)?(?![0-9])')

DELAY_LOW, DELAY_HIGH = 5, 240

HELP_TEXT = ('Please reply TAKEN or NOT YET for your medicine, or REMIND ME LATER if you need '
             'more time (for example "REMIND ME LATER 45"). When more than one dose is waiting, '
             'include the code from the reminder, for example TAKEN D12. For a daily check-in '
             'reply BETTER, SAME, WORSE or NEED HELP. SAHAYAK is not an emergency service; '
             'contact your local emergency number for urgent help.')


def _phrase(cleaned, phrases):
    for phrase in sorted(phrases, key=len, reverse=True):
        if re.search(r'(?<![A-Z])' + re.escape(phrase) + r'(?![A-Z])', cleaned):
            return phrase
    return None


def parse_reply(body):
    """Accept only one supported action, optionally one code and a supported delay.

    Do not infer confirmation from arbitrary sentences containing TAKEN. Conflicting actions,
    duplicate codes and negated prose get a clarification instead of a status update.
    """
    cleaned = ' '.join((body or '').upper().split())[:1000].strip(' .!')
    if not cleaned:
        return {'error': 'unknown'}
    codes = list(CODE.finditer(cleaned))
    if len(codes) > 1:
        return {'error': 'conflict'}
    prefix = codes[0].group(1) if codes else None
    code = int(codes[0].group(2)) if codes else None
    if code == 0:
        return {'error': 'unknown'}
    cleaned = ' '.join(CODE.sub(' ', cleaned).split())
    for phrase, action, category in sorted(ACTIONS, key=lambda a: len(a[0]), reverse=True):
        if category == 'dose' and action == 'LATER':
            match = re.fullmatch(re.escape(phrase) + r'(?:\s+(\d{1,6})(?:\s*(?:MIN|MINS|MINUTE|MINUTES))?)?', cleaned)
            if not match:
                continue
            delay = int(match.group(1)) if match.group(1) else None
            invalid = delay is not None and not DELAY_LOW <= delay <= DELAY_HIGH
            delay = None if invalid else delay
        else:
            if cleaned != phrase:
                continue
            delay, invalid = None, False
        if prefix and prefix != ('D' if category == 'dose' else 'C'):
            return {'error': 'conflict'}
        return {'action': action, 'category': category, 'code': code, 'prefix': prefix,
                'delay': delay, 'delay_invalid': invalid}
    return {'error': 'unknown'}
