"""Persist and reconcile delivery callbacks, including callbacks preceding REST completion."""
from .db import get_db, one, all_rows

RANK = {'accepted':0,'scheduled':0,'queued':1,'sending':2,'sent':3,
        'failed':4,'undelivered':4,'canceled':4,'delivered':5,'read':6}

def apply_callbacks(sid):
    """Requires a write transaction. Replays stored states without downgrading delivery."""
    message=one("SELECT * FROM messages WHERE provider_sid=? AND direction='outbound'",(sid,))
    if not message:
        return
    best_state=message['delivery_status']
    best_error=message['error_code']
    for event in all_rows('SELECT * FROM provider_callbacks WHERE sid=? ORDER BY created_at',(sid,)):
        if RANK.get(event['state'],-1)>=RANK.get(best_state,-1):
            best_state=event['state']
            best_error=event['error_code'] or None
    if best_state:
        get_db().execute('UPDATE messages SET delivery_status=?,error_code=? WHERE id=?',
                         (best_state,best_error,message['id']))
        if message['kind']=='alert':
            get_db().execute('UPDATE alerts SET delivered_status=? WHERE id=?',
                             (best_state,message['entity_id']))
