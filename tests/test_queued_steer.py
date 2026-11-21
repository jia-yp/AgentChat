import json
import sqlite3
import time

import pytest

from server.core import Store
from tests.test_system import system,send,claim
from tests.test_controls import finish
from tests.test_steering import take,ack


def begin(system):
    c,r,b,bh,ss,store=system
    with store.tx() as db:db.execute("UPDATE members SET harness='codex' WHERE name IN ('codex1','codex2')")
    c.post('/api/bridge/heartbeat',headers=bh,json={'sessions':[{'id':s['id'],'steer_supported':True} for s in ss[1:]]}).raise_for_status()
    root=send(c,r,'@all assigned work',max_rounds=8).json()
    deliveries=[claim(c,bh,s) for s in ss]
    return root,deliveries,{'Authorization':'Bearer '+ss[0]['token']}


def option(message,name='codex1'):
    delivery=next(d for d in message['deliveries'] if d['name']==name)
    assert delivery['status']=='pending'
    return delivery,delivery['steer_option']['task_ref']


def convert(system,delivery,ref,headers=None):
    c,r,*_=system
    return c.post(f'/api/rooms/{r["id"]}/deliveries/{delivery["id"]}/steer',headers=headers or {},json={'task_ref':ref})


def test_convert_peer_message_once_preserves_identity_and_removes_queue(system):
    c,r,b,bh,ss,store=system;root,ds,peer=begin(system)
    msg=send(c,r,'@codex1 short correction',headers=peer,cause=root['id']).json()
    q,ref=option(msg)
    result=convert(system,q,ref);result.raise_for_status()
    assert result.json()['id']==msg['id'] and result.json()['sender_name']=='claude1'
    assert result.json()['delivery_mode']=='normal'  # Original send semantics and nonce remain immutable.
    assert result.json()['deliveries'][0]['status']=='steered'
    assert result.json()['steers'][0]['source_delivery']==q['id']
    notes=take(system,ds[1]).json()['notes']
    assert len(notes)==1 and notes[0]['sender']=='claude1' and notes[0]['sender_kind']=='agent'
    ack(system,ds[1],notes).raise_for_status()
    finish(c,bh,ds[1]).raise_for_status()
    assert claim(c,bh,ss[1]) is None
    repeated=convert(system,q,ref);repeated.raise_for_status()
    assert len(repeated.json()['steers'])==1
    assert repeated.json()['steers'][0]['status']=='injected'
    assert convert(system,q,ref+'0').status_code==409
    with store.tx() as db:assert store.used_rounds(db,root['id'],ss[1]['id'])==1


def test_multi_recipient_conversion_is_independent(system):
    c,r,b,bh,ss,store=system;root,ds,peer=begin(system)
    msg=send(c,r,'@codex1 @codex2 shared correction',headers=peer,cause=root['id']).json()
    q1,ref1=option(msg);q2,ref2=option(msg,'codex2')
    first=convert(system,q1,ref1).json()
    assert next(d for d in first['deliveries'] if d['id']==q2['id'])['status']=='pending'
    second=convert(system,q2,ref2);second.raise_for_status()
    assert len(second.json()['steers'])==2
    assert all(d['status']=='steered' for d in second.json()['deliveries'])
    assert len(take(system,ds[1]).json()['notes'])==len(take(system,ds[2]).json()['notes'])==1


@pytest.mark.parametrize('outcome',['rejected','unknown'])
def test_failed_or_uncertain_injection_never_requeues_original(system,outcome):
    c,r,b,bh,ss,store=system;root,ds,peer=begin(system)
    msg=send(c,r,'@codex1 note').json();q,ref=option(msg)
    convert(system,q,ref).raise_for_status()
    notes=take(system,ds[1]).json()['notes'];ack(system,ds[1],notes,status=outcome).raise_for_status()
    finish(c,bh,ds[1]).raise_for_status();assert claim(c,bh,ss[1]) is None
    assert c.post(f'/api/rooms/{r["id"]}/deliveries/{q["id"]}/retry',json={}).status_code==409
    assert convert(system,q,ref).json()['steers'][0]['status']==outcome


def test_old_target_reference_cannot_follow_next_task(system):
    c,r,b,bh,ss,store=system;root,ds,peer=begin(system)
    next_task=send(c,r,'@codex1 independent next task').json()
    note=send(c,r,'@codex1 note for original task').json();q,ref=option(note)
    finish(c,bh,ds[1]).raise_for_status();second=claim(c,bh,ss[1]);assert second['message']['id']==next_task['id']
    assert convert(system,q,ref).status_code==409
    with store.tx() as db:
        assert db.execute('SELECT status FROM deliveries WHERE id=?',(q['id'],)).fetchone()['status']=='pending'
        assert db.execute('SELECT COUNT(*) FROM steers').fetchone()[0]==0


def test_already_claimed_message_cannot_be_steered_again(system):
    c,r,b,bh,ss,store=system;root,ds,peer=begin(system)
    msg=send(c,r,'@codex1 next').json();q,ref=option(msg)
    finish(c,bh,ds[1]).raise_for_status();second=claim(c,bh,ss[1]);assert second['id']==q['id']
    assert convert(system,q,ref).status_code==409
    assert convert(system,q,q['id']+':1').status_code==409
    with store.tx() as db:assert db.execute('SELECT status FROM deliveries WHERE id=?',(q['id'],)).fetchone()['status']=='running'


def test_only_owner_or_original_enabled_sender_may_convert(system):
    c,r,b,bh,ss,store=system;root,ds,peer=begin(system)
    msg=send(c,r,'@codex1 peer note',headers=peer,cause=root['id']).json();q,ref=option(msg)
    other={'Authorization':'Bearer '+ss[2]['token']}
    assert convert(system,q,ref,other).status_code==403
    assert convert(system,q,ref,bh).status_code==403
    listed=c.get(f'/api/rooms/{r["id"]}/messages',headers=other).json()
    assert next(m for m in listed if m['id']==msg['id'])['deliveries'][0]['steer_option'] is None
    convert(system,q,ref,peer).raise_for_status()
    with store.tx() as db:db.execute('UPDATE steers SET created=created-3')
    another=send(c,r,'@codex1 second note',headers=peer,cause=root['id']).json();q2,ref2=option(another)
    c.post(f'/api/rooms/{r["id"]}/members/{ss[0]["id"]}/control',json={'action':'disable'}).raise_for_status()
    assert convert(system,q2,ref2,peer).status_code==403


def test_private_manager_queue_and_other_room_are_scoped(system):
    c,r,b,bh,ss,store=system;root,ds,peer=begin(system)
    manager=c.post('/api/bridge/sessions',headers=bh,json={'name':'manager','harness':'codex','role':'manage','session_type':'manager'}).json()
    c.post('/api/bridge/heartbeat',headers=bh,json={'sessions':[{'id':manager['id'],'steer_supported':True}]}).raise_for_status()
    send(c,r,'@manager private active').raise_for_status();active=claim(c,bh,manager)
    note=send(c,r,'@manager private note').json();q,ref=option(note,'manager')
    assert convert(system,q,ref,peer).status_code==403
    result=convert(system,q,ref);result.raise_for_status();assert result.json()['private_to']==manager['id']
    assert all(m['id']!=note['id'] for m in c.get(f'/api/rooms/{r["id"]}/messages',headers=peer).json())
    another=c.post('/api/rooms',json={'name':'other'}).json()
    assert c.post(f'/api/rooms/{another["id"]}/deliveries/{q["id"]}/steer',json={'task_ref':ref}).status_code==404


@pytest.mark.parametrize('failure',['unsupported','too_long','offline','stopped'])
def test_unavailable_conversion_is_visible_and_does_not_lose_queue(system,failure):
    c,r,b,bh,ss,store=system;root,ds,peer=begin(system)
    msg=send(c,r,'@codex1 '+('x'*4001 if failure=='too_long' else 'note')).json()
    q=msg['deliveries'][0];ref=ds[1]['id']+':1'
    with store.tx() as db:
        if failure=='unsupported':db.execute('UPDATE members SET steer_supported=0 WHERE id=?',(ss[1]['id'],))
        elif failure=='offline':db.execute('UPDATE members SET last_seen=0 WHERE id=?',(ss[1]['id'],))
        elif failure=='stopped':db.execute('UPDATE messages SET stopped=1 WHERE id=?',(msg['id'],))
    result=convert(system,q,ref);assert result.status_code in (400,409)
    with store.tx() as db:assert db.execute('SELECT status FROM deliveries WHERE id=?',(q['id'],)).fetchone()['status']=='pending'
    if failure!='stopped':
        history=c.get(f'/api/rooms/{r["id"]}/messages').json();offer=next(m for m in history if m['id']==msg['id'])['deliveries'][0]['steer_option']
        assert offer['reason'] and not offer['task_ref']


def test_migration_preserves_legacy_receipts_and_accepts_two_recipients(tmp_path):
    path=tmp_path/'old.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE steers(id TEXT PRIMARY KEY,message INTEGER UNIQUE NOT NULL,member TEXT NOT NULL,
            delivery TEXT NOT NULL,attempt INTEGER NOT NULL,lease TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
            error TEXT NOT NULL DEFAULT '',claim_nonce TEXT NOT NULL DEFAULT '',claimed REAL,created REAL NOT NULL);
            CREATE INDEX steers_delivery ON steers(delivery,attempt,status);
            INSERT INTO steers VALUES('original',42,'one','task',2,'secret','injected','','batch',100,99);''')
    store=Store(path)
    with store.tx() as db:
        row=dict(db.execute("SELECT * FROM steers WHERE id='original'").fetchone())
        assert row['status']=='injected' and row['attempt']==2 and row['claim_nonce']=='batch' and row['lease']=='secret'
        assert row['source_delivery'] is None
        db.execute("INSERT INTO steers(id,message,member,delivery,attempt,lease,created,source_delivery) VALUES('second',42,'two','task2',1,'lease2',110,'queue')")
    reopened=Store(path)
    with reopened.tx() as db:
        assert db.execute('SELECT COUNT(*) FROM steers').fetchone()[0]==2
        assert db.execute("SELECT source_delivery FROM steers WHERE id='second'").fetchone()['source_delivery']=='queue'
