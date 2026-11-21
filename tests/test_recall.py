import concurrent.futures
import threading

import pytest
from fastapi.testclient import TestClient

from test_system import system,send,claim
from test_synchronous import enable_sync,finish,snapshot


def recall(c,r,message,**kwargs):
    return c.post(f'/api/rooms/{r["id"]}/messages/{message["id"]}/recall',json={},**kwargs)


def history(c,r,**kwargs):return c.get('/api/rooms/'+r['id']+'/messages?before=999999',**kwargs).json()


def test_recall_is_durable_idempotent_and_redacts_history_quotes_search(system):
    c,r,b,bh,ss,store=system;base='/api/rooms/'+r['id']
    payload={'body':'@all unique-recall-secret','nonce':'stable-recall-message'}
    message=c.post(base+'/messages',json=payload).json()
    assert message['recall_option']['available']
    quoted=send(c,r,'@owner retained quote reference',reply_to=message['id']).json()
    before=c.get(base+'/timeline').json()
    result=recall(c,r,message);result.raise_for_status();result=result.json()
    assert result['recalled_at'] and result['body']=='消息已撤回'
    assert all(d['status']=='cancelled' and d['attempts']==0 for d in result['deliveries'])
    assert recall(c,r,message).json()['recalled_at']==result['recalled_at']
    assert c.post(base+'/messages',json=payload).json()['recalled_at']==result['recalled_at']
    assert all(claim(c,bh,s) is None for s in ss)
    rows=history(c,r);assert next(m for m in rows if m['id']==quoted['id'])['quote']['body']=='消息已撤回'
    for headers in [bh,{'Authorization':'Bearer '+ss[0]['token']}]:
        rows=history(c,r,headers=headers)
        assert message['id'] not in [m['id'] for m in rows]
        assert 'unique-recall-secret' not in str(rows)
    assert c.get(base+'/search?q=unique-recall-secret').json()['results']==[]
    after=c.get(base+'/timeline?revision='+before['revision']).json()
    assert after['revision']!=before['revision'] and after['recalls']==[{'id':message['id'],'recalled_at':result['recalled_at']}]
    block=next(b for b in after['blocks'] if message['id'] in b['ids']);assert block['preview']=='消息已撤回'
    assert send(c,r,'@codex1 invalid quote',reply_to=message['id']).status_code==400
    assert c.post(base+'/deliveries/'+message['deliveries'][0]['id']+'/retry',json={}).status_code==409
    from server.core import Store
    reopened=Store(store.path)
    with reopened.tx() as db:assert db.execute('SELECT recalled_at FROM messages WHERE id=?',(message['id'],)).fetchone()[0]==result['recalled_at']


def test_only_original_human_owner_can_recall_in_correct_room(system):
    c,r,b,bh,ss,store=system
    message=send(c,r,'@all waiting').json()
    for headers in [bh,{'Authorization':'Bearer '+ss[0]['token']}]:assert recall(c,r,message,headers=headers).status_code==403
    outsider=TestClient(c.app);outsider.post('/api/register',json={'username':'outside','password':'another-long-password'}).raise_for_status()
    assert recall(outsider,r,message).status_code==403
    other=c.post('/api/rooms',json={'name':'other'}).json();assert recall(c,other,message).status_code==404
    task=claim(c,bh,ss[0]);finish(c,bh,task,'@owner agent response')
    response=next(m for m in history(c,r) if m['sender_kind']=='agent')
    assert recall(c,r,response).status_code==403
    own_only=send(c,r,'@owner no queued tasks').json();assert recall(c,r,own_only).status_code==409


def test_partial_delivery_or_retried_task_cannot_be_recalled(system):
    c,r,b,bh,ss,store=system
    message=send(c,r,'@all do work').json();task=claim(c,bh,ss[0])
    assert recall(c,r,message).status_code==409
    current=next(m for m in history(c,r) if m['id']==message['id'])
    assert not current['recall_option']['available'] and not current['recalled_at']
    assert sorted(d['status'] for d in current['deliveries'])==['pending','pending','running']
    finish(c,bh,task,body=None,error='fixture failure')
    c.post(f'/api/rooms/{r["id"]}/deliveries/{task["id"]}/retry',json={}).raise_for_status()
    assert recall(c,r,message).status_code==409
    assert claim(c,bh,ss[0])['id']==task['id']


def test_private_manager_queue_can_be_recalled_without_affecting_worker(system):
    c,r,b,bh,ss,store=system
    manager=c.post('/api/bridge/sessions',headers=bh,json={'name':'manager','harness':'codex','role':'manage','session_type':'manager'}).json()
    running=send(c,r,'@claude1 unrelated').json();task=claim(c,bh,ss[0])
    private=send(c,r,'@manager private-queued-secret').json()
    recall(c,r,private).raise_for_status();assert claim(c,bh,manager) is None
    assert all(m['id']!=private['id'] for m in history(c,r,headers={'Authorization':'Bearer '+ss[0]['token']}))
    assert not next(m for m in history(c,r) if m['id']==running['id'])['recalled_at']
    finish(c,bh,task)


def test_synchronous_root_and_next_round_recall_do_not_stall(system):
    c,r,b,bh,ss,store=system;enable_sync(c,bh,ss)
    root=send(c,r,'@all cancel before any claim',round_mode='synchronous',max_rounds=3).json()
    recall(c,r,root).raise_for_status()
    assert snapshot(c,r)['status']=='completed' and all(claim(c,bh,s) is None for s in ss)
    root=send(c,r,'@claude1 start',round_mode='synchronous',max_rounds=3).json();task=claim(c,bh,ss[0])
    note=send(c,r,'@codex1 next round',round_mode='synchronous',max_rounds=3).json()
    assert note['deliveries'][0]['status']=='round_waiting'
    recall(c,r,note).raise_for_status();finish(c,bh,task)
    assert snapshot(c,r)['status']=='completed' and claim(c,bh,ss[1]) is None


def bundled_setup(system):
    c,r,b,bh,ss,store=system;enable_sync(c,bh,ss)
    root=send(c,r,'@claude1 start',round_mode='synchronous',max_rounds=3).json();task=claim(c,bh,ss[0])
    notes=[send(c,r,'@codex1 '+text,round_mode='synchronous',max_rounds=3).json() for text in ['first note','second note']]
    finish(c,bh,task)
    assert snapshot(c,r)['round']==2
    return notes


@pytest.mark.parametrize('index',[0,1])
def test_recall_primary_or_bundled_message_preserves_other_inbox_items(system,index):
    c,r,b,bh,ss,store=system;notes=bundled_setup(system)
    recall(c,r,notes[index]).raise_for_status()
    task=claim(c,bh,ss[1]);assert task['message']['id']==notes[1-index]['id']
    assert [m['id'] for m in task['synchronous']['inbox']]==[notes[1-index]['id']]
    assert notes[index]['id'] not in [m['id'] for m in task['synchronous']['previous_messages']]
    assert task['synchronous']['quota']==1 and task['round_budget']['current']==2
    finish(c,bh,task);assert snapshot(c,r)['status']=='completed'


def test_bundled_note_is_not_recallable_after_primary_claim(system):
    c,r,b,bh,ss,store=system;notes=bundled_setup(system);task=claim(c,bh,ss[1])
    assert len(task['synchronous']['inbox'])==2
    assert recall(c,r,notes[1]).status_code==409
    message=next(m for m in history(c,r) if m['id']==notes[1]['id'])
    assert not message['recall_option']['available'] and not message['recalled_at']


def test_steer_conversion_and_recall_are_mutually_exclusive(system):
    c,r,b,bh,ss,store=system
    with store.tx() as db:db.execute("UPDATE members SET harness='codex' WHERE id=?",(ss[0]['id'],))
    c.post('/api/bridge/heartbeat',headers=bh,json={'sessions':[{'id':ss[0]['id'],'steer_supported':True}]}).raise_for_status()
    send(c,r,'@claude1 running');task=claim(c,bh,ss[0])
    queued=send(c,r,'@claude1 note').json();delivery=queued['deliveries'][0]
    path=f'/api/rooms/{r["id"]}/deliveries/{delivery["id"]}/steer'
    c.post(path,json={'task_ref':delivery['steer_option']['task_ref']}).raise_for_status()
    assert recall(c,r,queued).status_code==409
    queued=send(c,r,'@claude1 another note').json();delivery=queued['deliveries'][0]
    recall(c,r,queued).raise_for_status()
    assert c.post(f'/api/rooms/{r["id"]}/deliveries/{delivery["id"]}/steer',json={'task_ref':task['id']+':1'}).status_code==409


def test_claim_and_recall_race_has_exactly_one_winner(system):
    from server import recall as module
    from server.core import Problem
    c,r,b,bh,ss,store=system;owner={**c.get('/api/me').json(),'kind':'user'};bridge=store.identity(b['token'])
    for _ in range(8):
        message=send(c,r,'@claude1 @codex1 racing').json();barrier=threading.Barrier(2)
        def execute():barrier.wait();return store.claim(bridge,ss[0]['id'])
        def withdraw():
            barrier.wait()
            try:return module.withdraw(store,owner,r['id'],message['id'])
            except Problem as e:assert e.status==409;return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(execute);bcall=pool.submit(withdraw);task,result=a.result(),bcall.result()
        assert bool(task)!=bool(result)
        with store.tx() as db:
            status=[d['status'] for d in db.execute('SELECT status FROM deliveries WHERE message=? ORDER BY member',(message['id'],))]
            if result:assert status==['cancelled','cancelled']
            else:assert sorted(status)==['pending','running']
        c.post('/api/rooms/'+r['id']+'/control',json={'action':'stop-all'}).raise_for_status()
