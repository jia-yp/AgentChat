import secrets
import uuid

import pytest

from bridge.common import read_json,write_json
from bridge import lifecycle as local_lifecycle
from server.core import Store
from tests.test_system import system,send,claim


@pytest.fixture
def workspace(system):
    c,room,b,bh,ss,store=system
    manager=c.post('/api/bridge/sessions',headers=bh,json={'name':'manager','role':'manage','harness':'codex','session_type':'manager'}).json()
    mh={'Authorization':'Bearer '+manager['token']}
    with store.tx() as db:
        for index,s in enumerate(ss):db.execute('UPDATE members SET harness=? WHERE id=?',('claude' if index==0 else 'codex',s['id']))
    for s in ss:
        s['original_native']='native-'+s['id']
    pulse={'workspace_supported':True,'session_changes_supported':True,'workspace':'C:/project','project_name':'Fixture project',
           'sessions':[{'id':s['id'],'native_id':s['original_native'],'permissions':{'mode':'inherit','supported':True,'revision':0}} for s in ss]}
    c.post('/api/bridge/heartbeat',headers=bh,json=pulse).raise_for_status()
    return c,room,b,bh,ss,store,manager,mh


def transition(env,member,action='leave',**kwargs):
    c,room,*_=env
    return c.post(f'/api/rooms/{room["id"]}/members/{member["id"]}/lifecycle',json={'action':action,'nonce':uuid.uuid4().hex,**kwargs})


def member(env,session):
    c,room,*_=env
    return next(m for m in c.get('/api/rooms/'+room['id']).json()['members'] if m['id']==session['id'])


def test_workspace_defaults_preserve_existing_sessions_and_follow_only_by_owner_choice(workspace):
    c,r,b,bh,ss,store,m,mh=workspace;base='/api/rooms/'+r['id']
    settings=c.get(base+'/workspaces').json()['workspaces'][0]
    assert settings['modes']['antigravity']=='collaborate'
    payload={'modes':dict(settings['modes'],codex='full',claude='full'),'revision':settings['revision'],'manager_sessions':True}
    for h in (bh,mh,{'Authorization':'Bearer '+ss[0]['token']}):
        assert c.post(base+'/workspaces/'+b['member_id'],headers=h,json=payload).status_code==403
    c.post(base+'/workspaces/'+b['member_id'],json=payload).raise_for_status()
    assert member(workspace,ss[0])['permission_settings']['mode']=='inherit'
    new=c.post('/api/bridge/sessions',headers=bh,json={'name':'newcodex','role':'test','harness':'codex','permission_source':'workspace'}).json()
    assert new['permission_settings']['mode']=='full'
    original=member(workspace,ss[0])['permission_settings']
    c.post(base+'/members/'+ss[0]['id']+'/permissions',json={'source':'workspace','mode':'inherit','revision':original['revision']}).raise_for_status()
    assert member(workspace,ss[0])['permission_settings']['mode']=='full'
    assert member(workspace,ss[0])['permission_settings']['source']=='workspace'
    assert c.post(base+'/workspaces/'+b['member_id'],json=payload).status_code==409
    current=c.get(base+'/workspaces').json()['workspaces'][0]
    c.post(base+'/workspaces/'+b['member_id'],json={**payload,'revision':current['revision'],'modes':dict(current['modes'],claude='edits')}).raise_for_status()
    assert member(workspace,ss[0])['permission_settings']['mode']=='edits'
    assert member(workspace,ss[1])['permission_settings']['mode']=='inherit'


def test_graceful_exit_finishes_current_task_rejects_new_work_and_requires_local_ack(workspace):
    c,r,b,bh,ss,store,m,mh=workspace
    send(c,r,'@claude1 first');running=claim(c,bh,ss[0]);send(c,r,'@claude1 queued')
    response=transition(workspace,ss[0]);response.raise_for_status();op=response.json()
    assert op['state']=='leaving'
    assert claim(c,bh,ss[0]) is None
    assert c.post('/api/bridge/session-transitions/'+str(op['id'])+'/ack',headers=bh,json={}).status_code==409
    send(c,r,'@claude1 later')
    c.post('/api/bridge/deliveries/'+running['id']+'/finish',headers=bh,json={'lease':running['lease'],'body':'@owner final result','native_id':ss[0]['original_native']}).raise_for_status()
    with store.tx() as db:
        assert db.execute("SELECT COUNT(*) FROM deliveries WHERE member=? AND status IN ('pending','running','round_waiting')",(ss[0]['id'],)).fetchone()[0]==0
    # A restart preserves the pending exit; it is not silently undone or timed out.
    reopened=Store(store.path)
    with reopened.tx() as db:assert db.execute('SELECT departing FROM members WHERE id=?',(ss[0]['id'],)).fetchone()[0]==1
    c.post('/api/bridge/session-transitions/'+str(op['id'])+'/ack',headers=bh,json={}).raise_for_status()
    s=member(workspace,ss[0]);assert s['revoked'] and s['lifecycle']['state']=='left'
    assert s['native_id']==ss[0]['original_native']
    assert c.get('/api/rooms/'+r['id'],headers={'Authorization':'Bearer '+ss[0]['token']}).status_code==401


def test_immediate_exit_rejoin_rotates_credential_and_does_not_replay(workspace):
    c,r,b,bh,ss,store,m,mh=workspace
    send(c,r,'@claude1 first');running=claim(c,bh,ss[0])
    op=transition(workspace,ss[0],immediate=True).json()
    c.post('/api/bridge/session-transitions/'+str(op['id'])+'/ack',headers=bh,json={}).raise_for_status()
    assert c.post('/api/bridge/deliveries/'+running['id']+'/finish',headers=bh,json={'lease':running['lease'],'body':'@owner late'}).status_code==409
    join=transition(workspace,ss[0],action='rejoin').json();secret=secrets.token_urlsafe(32)
    ack='/api/bridge/session-transitions/'+str(join['id'])+'/ack'
    c.post(ack,headers=bh,json={'credential':secret}).raise_for_status()
    c.post(ack,headers=bh,json={'credential':secret}).raise_for_status()
    assert c.post(ack,headers=bh,json={'credential':secrets.token_urlsafe(32)}).status_code==409
    restored=member(workspace,ss[0]);assert not restored['enabled'] and not restored['revoked']
    assert restored['native_id']==ss[0]['original_native']
    assert claim(c,bh,ss[0]) is None
    assert c.get('/api/rooms/'+r['id'],headers={'Authorization':'Bearer '+ss[0]['token']}).status_code==401
    assert c.get('/api/rooms/'+r['id'],headers={'Authorization':'Bearer '+secret}).status_code==200
    send(c,r,'@claude1 next');assert claim(c,bh,ss[0])


def test_cancel_exit_and_stale_ack_cannot_stop_new_work(workspace):
    c,r,b,bh,ss,store,m,mh=workspace
    op=transition(workspace,ss[0]).json()
    path=f'/api/rooms/{r["id"]}/members/{ss[0]["id"]}/lifecycle'
    payload={'action':'cancel-leave','nonce':'cancel-same-exit'}
    cancelled=c.post(path,json=payload);cancelled.raise_for_status()
    assert c.post(path,json=payload).json()==cancelled.json()
    assert c.post('/api/bridge/session-transitions/'+str(op['id'])+'/ack',headers=bh,json={}).status_code==409
    send(c,r,'@claude1 fresh');assert claim(c,bh,ss[0])


def test_workspace_defaults_and_manager_actions_do_not_cross_another_bridge(workspace):
    c,r,b,bh,ss,store,m,mh=workspace;base='/api/rooms/'+r['id']
    other=c.post('/api/agent/join',headers={'Authorization':'Bearer '+r['join_token']},json={'name':'other-machine'}).json()
    oh={'Authorization':'Bearer '+other['token']}
    c.post('/api/bridge/heartbeat',headers=oh,json={'workspace_supported':True,'workspace':'C:/project'}).raise_for_status()
    remote=c.post('/api/bridge/sessions',headers=oh,json={'name':'remote-worker','role':'Test scope','harness':'codex','permission_source':'workspace'}).json()
    local=c.post('/api/bridge/sessions',headers=bh,json={'name':'local-worker','role':'Test scope','harness':'codex','permission_source':'workspace'}).json()
    policy=next(p for p in c.get(base+'/workspaces').json()['workspaces'] if p['bridge']==b['member_id'])
    c.post(base+'/workspaces/'+b['member_id'],json={'modes':dict(policy['modes'],codex='read-only'),'revision':policy['revision']}).raise_for_status()
    assert member(workspace,local)['permission_settings']['mode']=='read-only'
    assert member(workspace,remote)['permission_settings']['mode']=='network'
    send(c,r,'@manager remove remote-worker');task=claim(c,bh,m)
    assert c.post('/api/manager/sessions',headers=mh,json={'action':'leave','cause':task['message']['id'],
        'nonce':'cross-scope','members':[remote['id']]}).status_code==403
    inventory=c.get('/api/manager/sessions',headers=mh).json()
    assert remote['id'] not in {s['id'] for s in inventory['sessions']}
    assert not member(workspace,remote)['departing']


def test_leaving_does_not_hold_the_next_synchronous_round(workspace):
    c,r,b,bh,ss,store,m,mh=workspace
    c.post('/api/bridge/heartbeat',headers=bh,json={'workspace_supported':True,'sessions':[{'id':s['id'],'sync_supported':True} for s in ss]}).raise_for_status()
    send(c,r,'@all begin',round_mode='synchronous',max_rounds=3).raise_for_status()
    tasks=[claim(c,bh,s) for s in ss]
    transition(workspace,ss[0]).raise_for_status()
    for task in tasks:
        c.post('/api/bridge/deliveries/'+task['id']+'/finish',headers=bh,json={'lease':task['lease'],
            'body':'@all next'}).raise_for_status()
    # No gateway ack is needed to release the barrier or exclude the departing worker.
    snapshot=c.get('/api/rooms/'+r['id']).json()['synchronous']
    assert snapshot['round']==2 and snapshot['status']=='running'
    assert ss[0]['id'] not in {p['member'] for p in snapshot['participants']}
    assert claim(c,bh,ss[0]) is None
    assert claim(c,bh,ss[1]) and claim(c,bh,ss[2])


def test_manager_can_create_from_owner_instruction_but_not_escalate_or_cross_workspace(workspace):
    c,r,b,bh,ss,store,m,mh=workspace
    inventory=c.get('/api/manager/sessions',headers=mh);inventory.raise_for_status()
    send(c,r,'@manager create a reviewer');task=claim(c,bh,m)
    payload={'action':'create','cause':task['message']['id'],'nonce':'create-reviewer',
             'roles':[{'name':'reviewer','harness':'claude','role':'Review code'}]}
    for h in (bh,{'Authorization':'Bearer '+ss[0]['token']}):
        assert c.post('/api/manager/sessions',headers=h,json=payload).status_code==403
    for override in ({'permissions':'full'},{'args':['--dangerously-skip-permissions']},{'executable':'custom'},{'command':['custom']}):
        assert c.post('/api/manager/sessions',headers=mh,json={**payload,'roles':[{**payload['roles'][0],**override}]}).status_code==403
    job=c.post('/api/manager/sessions',headers=mh,json=payload);job.raise_for_status();job=job.json()
    assert job['status']=='applying' and job['source']=='manager'
    assert c.post('/api/manager/sessions',headers=mh,json=payload).json()['id']==job['id']
    assert job['additions'][0]['permission_source']=='workspace'
    c.post('/api/bridge/deliveries/'+task['id']+'/finish',headers=bh,json={'lease':task['lease'],'body':'@owner submitted'}).raise_for_status()
    assert c.post('/api/manager/sessions',headers=mh,json={**payload,'nonce':'after-task'}).status_code==403
    assert any(x['body']=='@owner submitted' for x in c.get('/api/rooms/'+r['id']+'/messages').json())


def test_manager_authored_change_retry_uses_the_owner_list_fallback(workspace):
    c,r,b,bh,ss,store,m,mh=workspace
    send(c,r,'@manager create a reviewer');task=claim(c,bh,m)
    job=c.post('/api/manager/sessions',headers=mh,json={'action':'create','cause':task['message']['id'],
        'nonce':'retry-create','roles':[{'name':'new-reviewer','harness':'claude','role':'Review code'}]}).json()
    c.post('/api/bridge/session-changes/'+job['id']+'/report',headers=bh,json={'error':'fixture unavailable'}).raise_for_status()
    c.post('/api/bridge/deliveries/'+task['id']+'/finish',headers=bh,json={'lease':task['lease'],'body':'@owner failed, check harness'}).raise_for_status()
    path=f'/api/rooms/{r["id"]}/session-changes/{job["id"]}/control'
    c.post(path,json={'action':'retry'}).raise_for_status()
    retried=claim(c,bh,m)
    assert retried['session_change']['id']==job['id']
    c.post('/api/bridge/deliveries/'+retried['id']+'/finish',headers=bh,
        json={'lease':retried['lease'],'body':'{"action":"apply"}'}).raise_for_status()
    assert c.get('/api/bridge/session-changes/'+job['id'],headers=bh).json()['status']=='applying'


@pytest.mark.parametrize('cancel',[False,True])
def test_settings_removal_waits_for_local_exit_or_reports_owner_cancellation(workspace,cancel):
    c,r,b,bh,ss,store,m,mh=workspace
    send(c,r,'@claude1 keep working');running=claim(c,bh,ss[0])
    base=f'/api/rooms/{r["id"]}/session-changes'
    roster=c.get(base).json()['managers'][0]
    response=c.post(base,json={'manager':m['id'],'additions':[],'removals':[ss[0]['id']],
        'expected':roster['revision'],'nonce':'settings-remove','removal_mode':'finish'})
    response.raise_for_status();job=response.json();claim(c,bh,m)
    c.post('/api/session-changes/'+job['id']+'/apply',headers=mh,json={}).raise_for_status()
    remove='/api/bridge/session-changes/'+job['id']+'/remove'
    pending=c.post(remove,headers=bh,json={'member':ss[0]['id']}).json()
    assert not pending['ok'] and member(workspace,ss[0])['departing']
    if cancel:
        transition(workspace,ss[0],'cancel-leave').raise_for_status()
        assert c.post(remove,headers=bh,json={'member':ss[0]['id']}).status_code==409
        assert not member(workspace,ss[0])['departing']
    else:
        c.post('/api/bridge/deliveries/'+running['id']+'/finish',headers=bh,json={'lease':running['lease'],'body':'@owner finished'}).raise_for_status()
        c.post('/api/bridge/session-transitions/'+str(pending['operation']['id'])+'/ack',headers=bh,json={}).raise_for_status()
        assert c.post(remove,headers=bh,json={'member':ss[0]['id']}).json()['ok']
        c.post('/api/bridge/session-changes/'+job['id']+'/report',headers=bh,json={}).raise_for_status()


def test_manager_exit_requires_direct_instruction_and_workspace_grant(workspace):
    c,r,b,bh,ss,store,m,mh=workspace
    send(c,r,'@claude1 public');public=claim(c,bh,ss[0])
    payload={'action':'leave','cause':public['message']['id'],'nonce':'not-authorized','members':['claude1']}
    assert c.post('/api/manager/sessions',headers=mh,json=payload).status_code==403
    send(c,r,'@manager let claude1 leave');task=claim(c,bh,m)
    payload.update(cause=task['message']['id'],nonce='exit')
    assert c.post('/api/manager/sessions',headers=mh,json={**payload,'members':['manager']}).status_code==403
    c.post('/api/manager/sessions',headers=mh,json=payload).raise_for_status()
    assert member(workspace,ss[0])['departing']
    base='/api/rooms/'+r['id'];p=c.get(base+'/workspaces').json()['workspaces'][0]
    c.post(base+'/workspaces/'+b['member_id'],json={'modes':p['modes'],'revision':p['revision'],'manager_sessions':False}).raise_for_status()
    assert c.post('/api/manager/sessions',headers=mh,json={**payload,'nonce':'second','members':['codex1']}).status_code==403


def test_local_exit_waits_for_worker_and_restoration_recovers_lost_http_response(tmp_path):
    import threading
    from types import SimpleNamespace
    context=tmp_path/'session.json'
    session={'id':'s','native_id':'preserved','context':str(context),'token':'old'};write_json(context,session)
    calls=[]
    class Client:
        fail=False
        def call(self,path,payload):
            calls.append((path,payload))
            if self.fail:self.fail=False;raise OSError('lost response')
            return {}
    b=SimpleNamespace(sessions={'s':session},running={'s':{'cancel':threading.Event()}},lock=threading.RLock(),client=Client())
    op={'id':1,'member':'s','state':'leaving','immediate':False}
    local_lifecycle.sync(b,[op]);assert not calls and session['departing']
    b.running.clear();local_lifecycle.sync(b,[op]);assert session['revoked']
    join={'id':2,'member':'s','state':'joining','immediate':False};b.client.fail=True
    with pytest.raises(OSError):local_lifecycle.sync(b,[join])
    credential=calls[-1][1]['credential']
    # Server committed; retry sees joined. It uses the durably saved new key.
    local_lifecycle.sync(b,[{**join,'state':'joined'}])
    assert calls[-1][1]['credential']==credential
    assert read_json(context)['native_id']=='preserved' and read_json(context)['token']==credential
