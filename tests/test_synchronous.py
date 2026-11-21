import concurrent.futures
import time

import pytest

from test_system import system, send, claim


def enable_sync(c,bh,sessions):
    c.post('/api/bridge/heartbeat',headers=bh,json={'sessions':[{'id':s['id'],'sync_supported':True} for s in sessions]}).raise_for_status()


def finish(c,bh,d,body='@owner done',**kw):
    r=c.post('/api/bridge/deliveries/'+d['id']+'/finish',headers=bh,json={'lease':d['lease'],'body':body,**kw});r.raise_for_status();return r.json()


def snapshot(c,r):return c.get('/api/rooms/'+r['id']).json()['synchronous']


def test_round_barrier_full_previous_context_and_merged_mentions(system):
    c,r,b,bh,ss,store=system;enable_sync(c,bh,ss)
    root=send(c,r,'@all begin',round_mode='synchronous',max_rounds=3,session_speaks={ss[0]['id']:2}).json()
    tasks=[claim(c,bh,s) for s in ss]
    assert all(d['round_budget']['current']==1 for d in tasks)
    assert len({d['synchronous']['cutoff'] for d in tasks})==1
    h={'Authorization':'Bearer '+ss[0]['token']}
    note=send(c,r,'@codex1 first fast note',h,cause=root['id'])
    note.raise_for_status()
    finish(c,bh,tasks[0],'@codex1 second fast note')
    assert snapshot(c,r)['round']==1
    assert claim(c,bh,ss[0]) is None
    hidden=c.get('/api/rooms/'+r['id']+'/messages?before=99999',headers={'Authorization':'Bearer '+ss[1]['token']}).json()
    assert [m['id'] for m in hidden]==[root['id']]
    assert send(c,r,'@owner quote hidden',{'Authorization':'Bearer '+ss[1]['token']},cause=root['id'],reply_to=note.json()['id']).status_code==400
    finish(c,bh,tasks[1],'@claude1 second participant finished')
    assert snapshot(c,r)['round']==1 and claim(c,bh,ss[1]) is None
    finish(c,bh,tasks[2],'@codex1 slow participant now finished')
    assert snapshot(c,r)['round']==2
    second0=claim(c,bh,ss[0]);second1=claim(c,bh,ss[1])
    assert len(second1['synchronous']['inbox'])==3
    assert len(second1['synchronous']['previous_messages'])==4
    assert any('slow participant' in m['body'] for m in second1['synchronous']['previous_messages'])
    assert claim(c,bh,ss[1]) is None and claim(c,bh,ss[2]) is None
    finish(c,bh,second0,'@codex1 continue');finish(c,bh,second1,'@claude1 continue')
    third0=claim(c,bh,ss[0]);third1=claim(c,bh,ss[1])
    assert all(d['round_budget']['last'] and d['round_budget']['current']==3 for d in (third0,third1))
    finish(c,bh,third0,'@all final');finish(c,bh,third1,'@all final')
    assert snapshot(c,r)['status']=='completed'
    assert all(claim(c,bh,s) is None for s in ss)
    with store.tx() as db:
        assert db.execute("SELECT COUNT(*) FROM deliveries WHERE sync_root=? AND status='round_waiting'",(root['id'],)).fetchone()[0]==0


def test_quota_shared_by_send_and_final_and_early_finish(system):
    c,r,b,bh,ss,store=system;enable_sync(c,bh,ss)
    root=send(c,r,'@claude1 work',round_mode='synchronous',max_rounds=5).json();d=claim(c,bh,ss[0]);h={'Authorization':'Bearer '+ss[0]['token']}
    payload={'body':'@owner one formal reply','nonce':'same-note','cause':root['id']}
    path='/api/rooms/'+r['id']+'/messages'
    c.post(path,headers=h,json=payload).raise_for_status()
    c.post(path,headers=h,json=payload).raise_for_status()
    assert send(c,r,'@owner exceeds quota',h,cause=root['id']).status_code==409
    finish(c,bh,d,'@owner should not be duplicated')
    assert snapshot(c,r)['status']=='completed' and snapshot(c,r)['round']==1
    assert snapshot(c,r)['participants'][0]['used']==1
    assert len(c.get(path).json())==2
    assert finish(c,bh,d,'@owner should not be duplicated')['ok']
    # k is a cap, not a requirement to make three model calls.
    send(c,r,'@claude1 next',round_mode='synchronous',max_rounds=5,session_speaks={ss[0]['id']:3}).raise_for_status()
    d=claim(c,bh,ss[0]);finish(c,bh,d)
    assert snapshot(c,r)['status']=='completed' and snapshot(c,r)['participants'][0]['used']==1


def test_midround_human_messages_wait_and_settings_are_frozen(system):
    c,r,b,bh,ss,store=system;enable_sync(c,bh,ss)
    root=send(c,r,'@claude1 first',round_mode='synchronous',max_rounds=4).json();d=claim(c,bh,ss[0])
    extra=send(c,r,'@codex1 join next round',round_mode='synchronous',max_rounds=4).json()
    assert extra['root']==root['id'] and extra['deliveries'][0]['status']=='round_waiting'
    assert claim(c,bh,ss[1]) is None
    assert send(c,r,'@codex1 cannot change budget',round_mode='synchronous',max_rounds=5).status_code==409
    assert send(c,r,'@codex1 cannot escape with asynchronous',round_mode='global').status_code==409
    finish(c,bh,d);next_task=claim(c,bh,ss[1]);assert next_task['round_budget']['current']==2
    finish(c,bh,next_task);assert snapshot(c,r)['status']=='completed'


def test_failure_waits_only_for_running_peers_then_advances_without_replay(system):
    c,r,b,bh,ss,store=system;enable_sync(c,bh,ss)
    root=send(c,r,'@all work',round_mode='synchronous',max_rounds=3).json()
    one,two,three=[claim(c,bh,s) for s in ss];finish(c,bh,one,'@codex1 next')
    finish(c,bh,two,body=None,error='failed fixture')
    assert snapshot(c,r)['status']=='running' and snapshot(c,r)['round']==1
    assert claim(c,bh,ss[1]) is None
    from server.core import Store
    restored=Store(store.path)
    assert restored.snapshot(r['id'],store.identity(b['token']))['synchronous']['status']=='running'
    retry='/api/rooms/'+r['id']+'/deliveries/'+two['id']+'/retry'
    c.post(retry,json={}).raise_for_status();reclaimed=claim(c,bh,ss[1]);assert reclaimed['id']==two['id']
    finish(c,bh,reclaimed,body=None,error='second failure')
    finish(c,bh,three);assert snapshot(c,r)['round']==2
    assert c.post(retry,json={}).status_code==409
    next_task=claim(c,bh,ss[1]);assert next_task['id']!=two['id']
    finish(c,bh,next_task);assert snapshot(c,r)['status']=='completed'
    failed=next(m for m in c.get('/api/rooms/'+r['id']+'/messages').json() if m['id']==root['id'])
    assert next(d for d in failed['deliveries'] if d['id']==two['id'])['retryable'] is False
    with store.tx() as db:
        old=db.execute('SELECT status,attempts,error FROM deliveries WHERE id=?',(two['id'],)).fetchone()
        assert tuple(old)==('failed',2,'second failure')


def test_expired_lease_rate_limit_and_disabled_participants(system):
    c,r,b,bh,ss,store=system;enable_sync(c,bh,ss)
    root=send(c,r,'@all task',round_mode='synchronous').json();tasks=[claim(c,bh,s) for s in ss]
    finish(c,bh,tasks[0],'@owner done')
    finish(c,bh,tasks[1],body=None,error='429 rate limit',error_kind='rate_limit')
    with store.tx() as db:db.execute('UPDATE deliveries SET lease_until=? WHERE id=?',(time.time()-1,tasks[2]['id']))
    s=snapshot(c,r);assert s['status']=='completed' and sum(p['status']=='failed' for p in s['participants'])==2
    send(c,r,'@claude1 @codex2 next',round_mode='synchronous').raise_for_status()
    c.post('/api/rooms/'+r['id']+'/members/'+ss[2]['id']+'/control',json={'action':'disable'}).raise_for_status()
    finish(c,bh,claim(c,bh,ss[0]));assert snapshot(c,r)['status']=='completed'


def test_legacy_blocked_round_releases_waiting_messages_after_restart(system):
    from server.core import Store
    c,r,b,bh,ss,store=system;enable_sync(c,bh,ss)
    root=send(c,r,'@claude1 long work',round_mode='synchronous',max_rounds=4).json();task=claim(c,bh,ss[0])
    queued=send(c,r,'@codex1 subsequent human message',round_mode='synchronous',max_rounds=4).json()
    with store.tx() as db:
        db.execute("UPDATE deliveries SET status='failed',error='legacy timeout' WHERE id=?",(task['id'],))
        db.execute("UPDATE sync_participants SET status='failed',error='legacy timeout' WHERE delivery=?",(task['id'],))
        db.execute("UPDATE sync_runs SET status='blocked' WHERE root=?",(root['id'],))
    restored=Store(store.path)
    state=restored.snapshot(r['id'],store.identity(b['token']))['synchronous']
    assert state['status']=='running' and state['round']==2
    next_task=claim(c,bh,ss[1]);assert next_task['message']['id']==queued['id']
    assert claim(c,bh,ss[0]) is None
    finish(c,bh,next_task);assert snapshot(c,r)['status']=='completed'
    send(c,r,'@claude1 fresh conversation',round_mode='synchronous',max_rounds=2).raise_for_status()


def test_all_failed_and_unsupported_future_participants_cannot_deadlock(system):
    c,r,b,bh,ss,store=system;enable_sync(c,bh,ss)
    root=send(c,r,'@claude1 first',round_mode='synchronous',max_rounds=3).json();task=claim(c,bh,ss[0])
    send(c,r,'@codex1 queued',round_mode='synchronous',max_rounds=3).raise_for_status()
    c.post('/api/bridge/heartbeat',headers=bh,json={'sessions':[{'id':ss[1]['id'],'sync_supported':False}]}).raise_for_status()
    finish(c,bh,task,body=None,error='explicit timeout')
    assert snapshot(c,r)['status']=='completed'
    assert claim(c,bh,ss[0]) is None and claim(c,bh,ss[1]) is None
    # Budget and mode can be changed for a genuinely new conversation.
    send(c,r,'@claude1 new independent work',round_mode='global',max_rounds=9).raise_for_status()


def test_steer_exception_and_conversion_do_not_create_next_round(system):
    c,r,b,bh,ss,store=system;enable_sync(c,bh,ss)
    with store.tx() as db:db.execute("UPDATE members SET harness='codex',steer_supported=1,last_seen=? WHERE id=?",(time.time(),ss[1]['id']))
    root=send(c,r,'@all work',round_mode='synchronous',session_speaks={ss[0]['id']:2}).json();tasks=[claim(c,bh,s) for s in ss]
    h={'Authorization':'Bearer '+ss[0]['token']};ref=tasks[1]['id']+':1'
    immediate=send(c,r,'@codex1 immediate steer',h,cause=root['id'],delivery_mode='steer',steer_task=ref);immediate.raise_for_status()
    assert snapshot(c,r)['participants'][0]['used']==0
    target_history=c.get('/api/rooms/'+r['id']+'/messages',headers={'Authorization':'Bearer '+ss[1]['token']}).json()
    assert any('immediate steer' in m['body'] for m in target_history)
    other=c.get('/api/rooms/'+r['id']+'/messages',headers={'Authorization':'Bearer '+ss[2]['token']}).json()
    assert not any('immediate steer' in m['body'] for m in other)
    note=send(c,r,'@codex1 queued note',h,cause=root['id']).json();delivery=note['deliveries'][0]
    assert delivery['status']=='round_waiting'
    c.post('/api/rooms/'+r['id']+'/deliveries/'+delivery['id']+'/steer',json={'task_ref':ref}).raise_for_status()
    for d in tasks:finish(c,bh,d)
    assert snapshot(c,r)['status']=='completed' and snapshot(c,r)['round']==1


def test_manager_private_and_stopping_all_do_not_leak_or_restart(system):
    c,r,b,bh,ss,store=system;enable_sync(c,bh,ss)
    mgr=c.post('/api/bridge/sessions',headers=bh,json={'name':'manager','role':'private','harness':'command','session_type':'manager'}).json()
    c.put('/api/rooms/'+r['id']+'/round-limit',json={'max_rounds':2,'round_mode':'synchronous'}).raise_for_status()
    root=send(c,r,'@all public').json();d=claim(c,bh,ss[0])
    private=send(c,r,'@manager private content').json();md=claim(c,bh,mgr)
    assert md and md['round_budget']['mode']=='global' and md['round_budget']['max']==0 and 'synchronous' not in md
    assert all(p['member']!=mgr['id'] for p in snapshot(c,r)['participants'])
    assert all('private content' not in x['body'] for x in c.get('/api/rooms/'+r['id']+'/messages',headers={'Authorization':'Bearer '+ss[0]['token']}).json())
    finish(c,bh,d,'@codex1 future')
    c.post('/api/rooms/'+r['id']+'/control',json={'action':'stop-all'}).raise_for_status()
    assert snapshot(c,r)['status']=='stopped'
    assert all(claim(c,bh,s) is None for s in ss)
    assert send(c,r,'@codex1 late',{'Authorization':'Bearer '+ss[0]['token']},cause=root['id']).status_code==409


def test_old_bridge_dormant_and_validation(system):
    c,r,b,bh,ss,store=system
    assert send(c,r,'@all work',round_mode='synchronous').status_code==409
    enable_sync(c,bh,[ss[0]])
    root=send(c,r,'@claude1 work',round_mode='synchronous').json();d=claim(c,bh,ss[0]);finish(c,bh,d,'@codex1 still dormant')
    assert snapshot(c,r)['status']=='completed' and claim(c,bh,ss[1]) is None
    for value in (0,101,-1):
        assert send(c,r,'@claude1 invalid',round_mode='synchronous',session_speaks={ss[0]['id']:value}).status_code==400


def test_concurrent_finishes_advance_exactly_once(system):
    c,r,b,bh,ss,store=system;enable_sync(c,bh,ss)
    send(c,r,'@all start',round_mode='synchronous',max_rounds=2).raise_for_status()
    tasks=[claim(c,bh,s) for s in ss]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda d:finish(c,bh,d,'@all next'),tasks))
    state=snapshot(c,r)
    assert state['round']==2 and len(state['participants'])==3
    next_tasks=[claim(c,bh,s) for s in ss]
    assert len({d['id'] for d in next_tasks})==3
    assert all(len(d['synchronous']['inbox'])==2 for d in next_tasks)
    assert all(claim(c,bh,s) is None for s in ss)


def test_complete_previous_round_is_in_prompt_beyond_history_page(system,tmp_path):
    from bridge.runner import Bridge
    c,r,b,bh,ss,store=system;enable_sync(c,bh,ss)
    root=send(c,r,'@all start',round_mode='synchronous',max_rounds=2,session_speaks={ss[0]['id']:20,ss[2]['id']:20}).json()
    tasks=[claim(c,bh,s) for s in ss]
    for number in range(35):
        # Stay below each sender's independent anti-flood limit of 20/minute.
        h={'Authorization':'Bearer '+ss[0 if number<18 else 2]['token']}
        send(c,r,f'@codex1 unique-round-note-{number:02d}',h,cause=root['id']).raise_for_status()
    finish(c,bh,tasks[0]);finish(c,bh,tasks[1]);finish(c,bh,tasks[2],'@codex1 slow-last-note')
    delivery=claim(c,bh,ss[1])
    assert len(delivery['synchronous']['previous_messages'])==38
    bridge=Bridge.__new__(Bridge);bridge.workspace=tmp_path;bridge.config={'room':{'id':r['id']}}
    class Identity:
        def call(self,path):
            result=c.get(path,headers={'Authorization':'Bearer '+ss[1]['token']});result.raise_for_status();return result.json()
    bridge.client=Identity()
    session={'name':'codex1','definition':{'role':'test'},'temp_dir':str(tmp_path),'context':str(tmp_path/'session.json')}
    prompt=bridge.prompt(session,delivery)
    assert all(f'unique-round-note-{number:02d}' in prompt for number in range(35))
    assert 'slow-last-note' in prompt and '本轮完整收件箱' in prompt
    assert '第 2/2 轮' in prompt and '不再继续工作' in prompt
