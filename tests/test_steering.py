import json
import time
import uuid

import pytest

from tests.test_system import system, send, claim
from tests.test_controls import finish


def ready(system, target=1):
    c,r,b,bh,ss,store=system
    with store.tx() as db:db.execute("UPDATE members SET harness='codex' WHERE id=?",(ss[target]['id'],))
    c.post('/api/bridge/heartbeat',headers=bh,json={'sessions':[{'id':ss[target]['id'],'steer_supported':True}]}).raise_for_status()
    root=send(c,r,'@all work',max_rounds=1).json()
    d=claim(c,bh,ss[target]);assert d['round_budget']['last']
    snap=c.get('/api/rooms/'+r['id']).json()
    ref=next(m for m in snap['members'] if m['id']==ss[target]['id'])['active_task']['ref']
    return d,ref,root


def note(system, ref, **kwargs):
    c,r,*_=system
    return send(c,r,'@codex1 只保留既有结论，补充此证据。',delivery_mode='steer',steer_task=ref,**kwargs)


def take(system, d, nonce='batch'):
    c,r,b,bh,*_=system
    return c.post('/api/bridge/deliveries/'+d['id']+'/steers/claim',headers=bh,json={'lease':d['lease'],'nonce':nonce})


def ack(system,d,notes,status='injected',nonce='batch'):
    c,r,b,bh,*_=system
    return c.post('/api/bridge/deliveries/'+d['id']+'/steers/ack',headers=bh,json={'lease':d['lease'],'nonce':nonce,'ids':[n['id'] for n in notes],'status':status})


def test_peer_steer_uses_current_turn_not_round_or_new_task(system):
    c,r,b,bh,ss,store=system;d,ref,root=ready(system)
    first=claim(c,bh,ss[0]);assert first
    response=note(system,ref,headers={'Authorization':'Bearer '+ss[0]['token']},cause=root['id'])
    response.raise_for_status();m=response.json()
    assert m['deliveries']==[] and m['steers'][0]['status']=='pending'
    assert m['delivery_mode']=='steer' and m['steer_task']==ref
    with store.tx() as db:
        assert store.used_rounds(db,root['id'],ss[1]['id'])==1
        assert db.execute('SELECT COUNT(*) FROM deliveries WHERE member=?',(ss[1]['id'],)).fetchone()[0]==1
    notes=take(system,d).json()['notes'];assert notes[0]['sender']=='claude1' and notes[0]['sender_kind']=='agent'
    ack(system,d,notes).raise_for_status();ack(system,d,notes).raise_for_status()
    finish(c,bh,d).raise_for_status()
    assert claim(c,bh,ss[1]) is None
    history=c.get('/api/rooms/'+r['id']+'/messages').json()
    assert next(x for x in history if x['id']==m['id'])['steers'][0]['status']=='injected'


def test_claim_lost_response_recovery_and_no_reclaim_by_new_nonce(system):
    d,ref,_=ready(system);note(system,ref).raise_for_status()
    notes=take(system,d).json()['notes'];assert notes
    assert take(system,d).json()['notes']==notes
    assert take(system,d,'another').json()['notes']==[]
    assert ack(system,d,notes,nonce='wrong').status_code==403
    ack(system,d,notes).raise_for_status()
    assert ack(system,d,notes,status='rejected').status_code==409


@pytest.mark.parametrize('action',['finish','stop-all','disable','expire'])
def test_ended_tasks_reject_pending_notes_never_redirect(system,action):
    c,r,b,bh,ss,store=system;d,ref,_=ready(system)
    m=note(system,ref).json()
    if action=='finish':finish(c,bh,d).raise_for_status()
    elif action=='stop-all':c.post('/api/rooms/'+r['id']+'/control',json={'action':'stop-all'}).raise_for_status()
    elif action=='disable':c.post(f'/api/rooms/{r["id"]}/members/{ss[1]["id"]}/control',json={'action':'disable'}).raise_for_status()
    else:
        with store.tx() as db:db.execute('UPDATE deliveries SET lease_until=? WHERE id=?',(time.time()-1,d['id']))
    history=c.get('/api/rooms/'+r['id']+'/messages').json()
    assert next(x for x in history if x['id']==m['id'])['steers'][0]['status']=='rejected'
    assert note(system,ref).status_code==409
    assert take(system,d).status_code==409


def test_unknown_ack_is_honest_and_late_ack_can_reconcile(system):
    c,r,b,bh,ss,store=system;d,ref,_=ready(system)
    m=note(system,ref).json();notes=take(system,d).json()['notes']
    finish(c,bh,d).raise_for_status()
    history=c.get('/api/rooms/'+r['id']+'/messages').json()
    assert next(x for x in history if x['id']==m['id'])['steers'][0]['status']=='unknown'
    ack(system,d,notes).raise_for_status()
    assert take(system,d).status_code==409


def test_retry_requires_new_attempt_and_cannot_drain_old_notes(system):
    c,r,b,bh,ss,store=system;d,ref,_=ready(system)
    note(system,ref).raise_for_status()
    finish(c,bh,d,body=None,error='interrupted').raise_for_status()
    c.post(f'/api/rooms/{r["id"]}/deliveries/{d["id"]}/retry',json={}).raise_for_status()
    retry=claim(c,bh,ss[1]);assert retry['id']==d['id'] and retry['lease']!=d['lease']
    assert note(system,ref).status_code==409
    assert take(system,retry).json()['notes']==[]
    assert take(system,d).status_code==403
    note(system,ref.split(':')[0]+':2').raise_for_status()
    assert len(take(system,retry,'new').json()['notes'])==1


def test_idempotence_does_not_change_mode_or_task(system):
    c,r,b,bh,ss,store=system;d,ref,_=ready(system)
    payload={'body':'@codex1 note','nonce':'stable','delivery_mode':'steer','steer_task':ref}
    url=f'/api/rooms/{r["id"]}/messages';first=c.post(url,json=payload);first.raise_for_status()
    finish(c,bh,d).raise_for_status()
    assert c.post(url,json=payload).json()['id']==first.json()['id']
    assert c.post(url,json={**payload,'delivery_mode':'normal','steer_task':''}).status_code==409
    assert c.post(url,json={**payload,'steer_task':ref+'0'}).status_code==409


def test_private_manager_cannot_leak_steer_or_task(system):
    c,r,b,bh,ss,store=system;d,ref,root=ready(system)
    manager=c.post('/api/bridge/sessions',headers=bh,json={'name':'manager','harness':'codex','role':'manage','session_type':'manager'}).json()
    c.post('/api/bridge/heartbeat',headers=bh,json={'sessions':[{'id':manager['id'],'steer_supported':True}]}).raise_for_status()
    private=send(c,r,'@manager private work').json();md=claim(c,bh,manager)
    snap=c.get('/api/rooms/'+r['id']).json();mr=next(m for m in snap['members'] if m['id']==manager['id'])['active_task']['ref']
    message=send(c,r,'@manager private note',delivery_mode='steer',steer_task=mr).json();assert message['private_to']==manager['id']
    wh={'Authorization':'Bearer '+ss[0]['token']}
    assert all(m['id']!=manager['id'] for m in c.get('/api/rooms/'+r['id'],headers=wh).json()['members'])
    assert all(m['id']!=message['id'] for m in c.get('/api/rooms/'+r['id']+'/messages',headers=wh).json())
    assert send(c,r,'@manager leak',headers=wh,cause=root['id'],delivery_mode='steer',steer_task=mr).status_code==400
    assert note(system,mr).status_code==409
    assert note(system,ref,reply_to=private['id']).status_code==403
    assert note(system,ref,headers={'Authorization':'Bearer '+manager['token']},cause=private['id']).status_code==400


def test_capability_activation_limits_and_pause_guards(system):
    c,r,b,bh,ss,store=system
    assert note(system,'x:1').status_code==409
    assert not next(m for m in c.get('/api/rooms/'+r['id']).json()['members'] if m['id']==ss[1]['id'])['enabled']
    d,ref,_=ready(system)
    assert send(c,r,'@all note',delivery_mode='steer',steer_task=ref).status_code==400
    assert send(c,r,'@codex1 '+'x'*4000,delivery_mode='steer',steer_task=ref).status_code==400
    note(system,ref).raise_for_status();assert note(system,ref).status_code==429
    for _ in range(7):
        with store.tx() as db:db.execute('UPDATE steers SET created=created-3')
        note(system,ref).raise_for_status()
    with store.tx() as db:db.execute('UPDATE steers SET created=created-3')
    assert note(system,ref).status_code==429
    # Legacy bridges must never advertise capability left over from a newer one.
    c.post('/api/bridge/heartbeat',headers=bh,json={'sessions':[{'id':ss[1]['id']}]}).raise_for_status()
    assert note(system,ref).status_code==409


def test_bridge_scoping_and_expired_claim_outcome(system):
    c,r,b,bh,ss,store=system;d,ref,_=ready(system)
    message=note(system,ref).json();notes=take(system,d).json()['notes']
    rogue=c.post('/api/bridge/deliveries/'+d['id']+'/steers/claim',headers={'Authorization':'Bearer '+ss[0]['token']},json={'lease':d['lease'],'nonce':'stolen'})
    assert rogue.status_code==403
    with store.tx() as db:db.execute('UPDATE steers SET claimed=claimed-46')
    history=c.get('/api/rooms/'+r['id']+'/messages').json()
    assert next(m for m in history if m['id']==message['id'])['steers'][0]['status']=='unknown'
    assert take(system,d,'fresh').json()['notes']==[]


def test_steer_prompt_preserves_final_round_and_peer_identity():
    from bridge.codex_live import steer_input
    text=steer_input([{'sender':'claude','sender_kind':'agent','body':'new instructions'}],{'round_budget':{'last':True,'max':3,'remaining':1}})
    assert '不是新任务' in text and '不是管理员指令' in text and '最后一轮' in text
    assert '不再执行新的工具工作' in text and 'claude' in text


def test_bridge_claim_drain_preserves_running_work_and_queue(system):
    c,r,b,bh,ss,store=system;d,ref,_=ready(system)
    queued=send(c,r,'@codex1 next').json()
    with store.tx() as db:db.execute('UPDATE members SET paused=1 WHERE id=?',(b['member_id'],))
    finish(c,bh,d).raise_for_status()
    assert claim(c,bh,ss[1]) is None
    with store.tx() as db:db.execute('UPDATE members SET paused=0 WHERE id=?',(b['member_id'],))
    assert claim(c,bh,ss[1])['message']['id']==queued['id']
