"""Acknowledge ownership changes only after the local worker has stopped."""
import secrets
from pathlib import Path

from .common import APIError, read_json, write_json
from .execution import cancel_job


def sync(bridge, operations):
    for op in operations:
        session = bridge.sessions.get(op['member'])
        if session is None:
            # Never create a replacement native identity for a missing local one.
            continue
        with bridge.lock:
            session['departing'] = op['state'] == 'leaving'
            job = bridge.running.get(session['id'])
            if job:
                if op['state'] == 'leaving' and op['immediate']:
                    cancel_job(job, 'session_left', '执行已停止：管理员要求立即退出托管')
                continue
            if op['state'] in ('left','joined') and session.get('lifecycle_operation') == op['id']:
                continue
        base = '/api/bridge/session-transitions/' + str(op['id']) + '/ack'
        try:
            if op['state'] == 'leaving':
                bridge.client.call(base, {})
                session['revoked'] = True
                session['departing'] = False
                session['lifecycle_operation'] = op['id']
                write_json(session['context'], session)
            elif op['state'] in ('joining','joined'):
                path = Path(session['context']).parent / 'rejoin-credential.json'
                pending = read_json(path) if path.exists() else {}
                if op['state'] == 'joined' and pending.get('operation') != op['id']:
                    # A server confirmation without this machine's matching key
                    # is not permission to manufacture another active writer.
                    continue
                if pending.get('operation') != op['id']:
                    pending = {'operation':op['id'], 'credential':secrets.token_urlsafe(32)}
                    write_json(path, pending)
                bridge.client.call(base, {'credential':pending['credential']})
                session.update(token=pending['credential'],revoked=False,departing=False,lifecycle_operation=op['id'],detail='')
                write_json(session['context'], session)
                registration = Path(session['context']).parent / 'registration.json'
                if registration.exists():
                    saved=read_json(registration);saved['credential']=pending['credential'];write_json(registration,saved)
            elif op['state'] == 'left':
                session.update(revoked=True,departing=False,lifecycle_operation=op['id'])
                write_json(session['context'],session)
            elif op['state'] == 'cancelled':
                session['departing']=False
        except APIError as exc:
            if exc.status not in (404,409):raise
            # Cancellation or a stale acknowledgment is settled by next heartbeat.
            continue
