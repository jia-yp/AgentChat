import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_system import system, send, claim
from test_synchronous import enable_sync, finish, snapshot
from test_reading import manager, timeline


def incident(system, *, maximum=3, sync=True):
    c,r,b,bh,ss,store=system;m=manager(system)
    enable_sync(c,bh,ss)
    root=send(c,r,'@claude1 original project work',round_mode='synchronous' if sync else 'global',max_rounds=maximum).json()
    d=claim(c,bh,ss[0])
    if sync:send(c,r,'@codex1 next round task',round_mode='synchronous',max_rounds=maximum).raise_for_status()
    finish(c,bh,d,body=None,error='fixture stream interrupted',native_id='original-worker-session')
    return m,root,d


def decide(c,bh,d,action,include=False):
    return finish(c,bh,d,'@owner '+json.dumps({'action':action,'include_failed':include,'reason':'PRIVATE-DIAGNOSIS'}),native_id='original-manager-session')


def test_handoff_private_durable_deduplicated_and_worker_cannot_decide(system):
    c,r,b,bh,ss,store=system;m,root,d=incident(system)
    for _ in range(3):
        view=snapshot(c,r);assert view['round']==1 and view['recovering'][0]['member']=='claude1'
    from server.core import Store
    restored=Store(store.path)
    restored.snapshot(r['id'],store.identity(b['token']))
    with store.tx() as db:assert db.execute('SELECT COUNT(*) FROM error_recoveries').fetchone()[0]==1
    task=claim(c,bh,m);assert task['error_recovery']['source']==d['id']
    assert task['error_recovery']['member']['native_id']=='original-worker-session'
    assert task['message']['private_to']==m['id'] and '最终回复格式' in task['message']['body']
    path='/api/rooms/'+r['id'];headers={'Authorization':'Bearer '+ss[0]['token']}
    public=c.get(path+'/messages',headers=headers).json()
    assert task['message']['id'] not in {v['id'] for v in public}
    assert 'recovering' not in c.get(path,headers=headers).json()['synchronous']
    assert c.post('/api/bridge/deliveries/'+task['id']+'/finish',headers=headers,json={'lease':task['lease'],'body':'@owner {"action":"retry"}'}).status_code==403
    block=next(b for b in timeline(c,r)['blocks'] if task['message']['id'] in b['ids'])
    assert '最终回复格式' not in block['preview'] and '\n' not in block['preview']
    human=c.post(path+'/reading/messages',json={'ids':[task['message']['id']]}).json()[0]
    assert '最终回复格式' not in human['body']


@pytest.mark.parametrize('action',['retry','continue'])
@pytest.mark.parametrize('sync',[True,False])
def test_decision_resumes_same_task_and_native_session_without_duplicate_dispatch(system,action,sync):
    c,r,b,bh,ss,store=system;m,root,d=incident(system,sync=sync)
    md=claim(c,bh,m);decide(c,bh,md,action)
    assert decide(c,bh,md,action)['ok']
    retry=claim(c,bh,ss[0]);assert retry['id']==d['id'] and retry['recovery_mode']==action
    assert claim(c,bh,ss[0]) is None
    c.post('/api/bridge/heartbeat',headers=bh,json={'sessions':[{'id':ss[0]['id'],'native_id':'original-worker-session','detail':'old failure'}]}).raise_for_status()
    recovered=next(s for s in c.get('/api/rooms/'+r['id']).json()['members'] if s['id']==ss[0]['id'])
    assert recovered['status']=='running' and recovered['detail']==''
    with store.tx() as db:
        assert db.execute('SELECT attempts FROM deliveries WHERE id=?',(d['id'],)).fetchone()[0]==2
        assert db.execute('SELECT native_id FROM members WHERE id=?',(ss[0]['id'],)).fetchone()[0]=='original-worker-session'
    finish(c,bh,retry,native_id='original-worker-session')
    if sync:assert snapshot(c,r)['round']==2
    assert claim(c,bh,m) is None


@pytest.mark.parametrize('include',[False,True])
def test_next_round_can_include_failed_worker_without_leaking_manager_reason(system,include):
    c,r,b,bh,ss,store=system;m,root,d=incident(system)
    md=claim(c,bh,m);decide(c,bh,md,'next',include)
    view=snapshot(c,r);assert view['round']==2 and not view['recovering']
    next_task=claim(c,bh,ss[1]);assert next_task
    failed_worker=claim(c,bh,ss[0]);assert bool(failed_worker)==include
    if include:
        assert failed_worker['id']!=d['id'] and failed_worker['recovery_mode']=='continue'
        assert failed_worker['round_budget']['current']==2
        assert 'original project work' in failed_worker['message']['body']
        finish(c,bh,failed_worker)
    for s in ss:
        history=c.get('/api/rooms/'+r['id']+'/messages',headers={'Authorization':'Bearer '+s['token']}).json()
        assert 'PRIVATE-DIAGNOSIS' not in json.dumps(history)
        assert not any(x['message_kind']=='recovery_request' for x in history)
    with store.tx() as db:assert db.execute('SELECT status FROM deliveries WHERE id=?',(d['id'],)).fetchone()[0]=='failed'
    finish(c,bh,next_task)
    assert snapshot(c,r)['status']=='completed'


@pytest.mark.parametrize('failure',['failed','disabled','offline','expired','rate_limit','invalid_decision'])
def test_manager_failure_or_unavailable_releases_round_without_recursion(system,failure):
    c,r,b,bh,ss,store=system;m,root,d=incident(system);md=claim(c,bh,m)
    if failure in ('failed','rate_limit'):
        finish(c,bh,md,body=None,error='manager fixture failure',error_kind='rate_limit' if failure=='rate_limit' else '')
    elif failure=='invalid_decision':finish(c,bh,md,'@owner no structured decision')
    elif failure=='disabled':c.post('/api/rooms/'+r['id']+'/members/'+m['id']+'/control',json={'action':'disable'}).raise_for_status()
    else:
        with store.tx() as db:
            if failure=='offline':db.execute('UPDATE members SET last_seen=? WHERE id=(SELECT bridge FROM members WHERE id=?)',(time.time()-100,m['id']))
            else:db.execute('UPDATE deliveries SET lease_until=? WHERE id=?',(time.time()-1,md['id']))
    assert snapshot(c,r)['round']==2
    with store.tx() as db:
        assert db.execute("SELECT COUNT(*) FROM error_recoveries WHERE status='pending'").fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM error_recoveries').fetchone()[0]==1
    assert claim(c,bh,ss[1])


@pytest.mark.parametrize('manual',['next','retry','stop'])
def test_manual_takeover_cancels_manager_decision_and_no_stale_replay(system,manual):
    c,r,b,bh,ss,store=system;m,root,d=incident(system);md=claim(c,bh,m)
    path='/api/rooms/'+r['id']
    if manual=='next':response=c.post(path+'/synchronous/continue',json={'root':root['id'],'round':1})
    elif manual=='retry':response=c.post(path+'/deliveries/'+d['id']+'/retry',json={})
    else:response=c.post(path+'/control',json={'action':'stop-all'})
    response.raise_for_status()
    late=c.post('/api/bridge/deliveries/'+md['id']+'/finish',headers=bh,json={'lease':md['lease'],'body':'@owner {"action":"continue"}'})
    assert late.status_code==409
    assert bool(claim(c,bh,ss[0]))==(manual=='retry')
    if manual=='next':assert snapshot(c,r)['round']==2
    if manual=='stop':assert snapshot(c,r)['status']=='stopped'


def test_budget_and_disabled_session_remain_authoritative(system):
    c,r,b,bh,ss,store=system;m=manager(system);enable_sync(c,bh,ss)
    send(c,r,'@claude1 final',round_mode='synchronous',max_rounds=1).raise_for_status()
    d=claim(c,bh,ss[0]);finish(c,bh,d,body=None,error='fixture')
    decide(c,bh,claim(c,bh,m),'next',True)
    assert snapshot(c,r)['status']=='completed' and claim(c,bh,ss[0]) is None
    send(c,r,'@claude1 next work',round_mode='global',max_rounds=1).raise_for_status()
    d=claim(c,bh,ss[0]);finish(c,bh,d,body=None,error='fixture')
    md=claim(c,bh,m)
    c.post('/api/rooms/'+r['id']+'/members/'+ss[0]['id']+'/control',json={'action':'disable'}).raise_for_status()
    decide(c,bh,md,'continue')
    assert claim(c,bh,ss[0]) is None
    with store.tx() as db:assert db.execute('SELECT paused FROM members WHERE id=?',(ss[0]['id'],)).fetchone()[0]==1


def test_repeated_failure_has_previous_decision_and_keeps_one_pending_handler(system):
    c,r,b,bh,ss,store=system;m,root,d=incident(system)
    decide(c,bh,claim(c,bh,m),'continue');again=claim(c,bh,ss[0]);finish(c,bh,again,body=None,error='still broken')
    md=claim(c,bh,m);assert md['error_recovery']['attempt']==2
    assert md['error_recovery']['previous_decisions'][0]['action']=='continue'
    assert claim(c,bh,m) is None
    decide(c,bh,md,'next');assert snapshot(c,r)['round']==2


def test_legacy_blocked_failure_is_handed_to_manager_after_restart(system):
    c,r,b,bh,ss,store=system;m=manager(system);enable_sync(c,bh,ss)
    root=send(c,r,'@claude1 long task',round_mode='synchronous',max_rounds=4).json();d=claim(c,bh,ss[0])
    send(c,r,'@codex1 queued',round_mode='synchronous',max_rounds=4).raise_for_status()
    with store.tx() as db:
        db.execute("UPDATE deliveries SET status='failed',error='legacy timeout' WHERE id=?",(d['id'],))
        db.execute("UPDATE sync_participants SET status='failed' WHERE delivery=?",(d['id'],))
        db.execute("UPDATE sync_runs SET status='blocked' WHERE root=?",(root['id'],))
    from server.core import Store
    Store(store.path)
    assert snapshot(c,r)['recovering']
    decide(c,bh,claim(c,bh,m),'next',True)
    assert snapshot(c,r)['round']==2 and claim(c,bh,ss[0]) and claim(c,bh,ss[1])


def test_recovery_prompt_has_local_diagnostics_and_no_other_session_credentials(tmp_path):
    from bridge.runner import Bridge
    bridge=Bridge.__new__(Bridge);bridge.workspace=tmp_path;bridge.config={'room':{'id':'room'}}
    bridge.sessions={'worker':{'context':str(tmp_path/'worker/session.json'),'native_id':'original','token':'NEVER-PRINT-THIS'}}
    session={'name':'manager','context':str(tmp_path/'manager/session.json'),'temp_dir':str(tmp_path),'definition':{'harness':'codex'}}
    prompt=bridge.prompt(session,{'error_recovery':{'member_id':'worker'},'message':{'body':'@manager decide'}})
    assert 'original' in prompt and json.dumps(str(tmp_path/'worker/logs')) in prompt
    assert 'NEVER-PRINT-THIS' not in prompt and 'include_failed=true' in prompt


def test_next_round_coalesces_existing_mentions_and_preserves_bundled_task(system):
    c,r,b,bh,ss,store=system;m=manager(system);enable_sync(c,bh,ss)
    send(c,r,'@codex1 begin',round_mode='synchronous',max_rounds=4).raise_for_status();first=claim(c,bh,ss[1])
    send(c,r,'@claude1 inbox-A',round_mode='synchronous',max_rounds=4).raise_for_status()
    send(c,r,'@claude1 inbox-B',round_mode='synchronous',max_rounds=4).raise_for_status()
    finish(c,bh,first);d=claim(c,bh,ss[0]);assert len(d['synchronous']['inbox'])==2
    send(c,r,'@claude1 already-queued',round_mode='synchronous',max_rounds=4).raise_for_status()
    finish(c,bh,d,body=None,error='fixture')
    md=claim(c,bh,m);assert 'inbox-B' in md['message']['body']
    assert send(c,r,'@owner duplicate',{'Authorization':'Bearer '+m['token']},cause=md['message']['id']).status_code==409
    decide(c,bh,md,'next',True)
    resumed=claim(c,bh,ss[0]);assert resumed['round_budget']['current']==3
    assert len(resumed['synchronous']['inbox'])==2 and resumed['synchronous']['quota']==1
    combined=json.dumps(resumed['synchronous']['inbox']);assert 'inbox-A' in combined and 'inbox-B' in combined
    assert claim(c,bh,ss[0]) is None


def test_individual_next_uses_session_allowance_instead_of_global_depth(system):
    c,r,b,bh,ss,store=system;m=manager(system)
    send(c,r,'@claude1 task',round_mode='individual',max_rounds=1,session_rounds={ss[0]['id']:3}).raise_for_status()
    d=claim(c,bh,ss[0]);finish(c,bh,d,body=None,error='fixture')
    decide(c,bh,claim(c,bh,m),'next',True)
    resumed=claim(c,bh,ss[0]);assert resumed['round_budget']['current']==2 and resumed['round_budget']['max']==3
