import json
import secrets
import time
import uuid
from types import SimpleNamespace

import pytest

from bridge.common import APIError,read_json,write_json
from bridge.runner import Bridge
from server import session_changes
from tests.test_system import system,send,claim


ROLE={'name':'新增成员','harness':'command','role':'只完成分工','command':['fixture'],'timeout_seconds':0}


@pytest.fixture
def changes(system):
    c,room,b,bh,ss,store=system
    m=c.post('/api/bridge/sessions',headers=bh,json={'name':'manager','role':'管理','harness':'command','session_type':'manager'}).json()
    mh={'Authorization':'Bearer '+m['token']}
    c.post('/api/bridge/heartbeat',headers=bh,json={'session_changes_supported':True,'workspace':'E:\\project','project_name':'User project'}).raise_for_status()
    base='/api/rooms/'+room['id']+'/session-changes'
    return c,room,b,bh,ss,store,m,mh,base


def submit(env,additions=None,removals=None,**overrides):
    c,room,b,bh,ss,store,m,mh,base=env
    manager=c.get(base).json()['managers'][0]
    payload={'manager':m['id'],'additions':[ROLE] if additions is None else additions,'removals':removals or [],'expected':manager['revision'],'nonce':uuid.uuid4().hex,**overrides}
    r=c.post(base,json=payload);r.raise_for_status()
    return r.json(),payload


def apply(env,job):
    c,room,b,bh,ss,store,m,mh,base=env
    task=claim(c,bh,m);assert task['session_change']['id']==job['id']
    r=c.post('/api/session-changes/'+job['id']+'/apply',headers=mh,json={});r.raise_for_status()
    return task


def register(env,job,role=ROLE,**extra):
    c,room,b,bh,ss,store,m,mh,base=env
    return c.post('/api/bridge/sessions',headers=bh,json={**role,'change_id':job['id'],'credential':secrets.token_urlsafe(32),**extra})


def test_add_remove_private_durable_dormant_and_native_preserved(changes):
    c,room,b,bh,ss,store,m,mh,base=changes
    send(c,room,'@claude1 working');running=claim(c,bh,ss[0])
    send(c,room,'@codex1 independent');peer=claim(c,bh,ss[1])
    job,payload=submit(changes,removals=[ss[0]['id']])
    assert c.post(base,json=payload).json()['id']==job['id']
    assert claim(c,bh,m)['session_change']['additions']==[ROLE]
    assert c.post('/api/session-changes/'+job['id']+'/apply',headers=mh,json={}).json()['status']=='applying'
    assert c.post('/api/bridge/session-changes/'+job['id']+'/report',headers=bh,json={}).status_code==409
    credential=secrets.token_urlsafe(32)
    new=register(changes,job,credential=credential).json()
    assert register(changes,job,credential=credential).json()==new
    c.post('/api/bridge/session-changes/'+job['id']+'/remove',headers=bh,json={'member':ss[0]['id']}).raise_for_status()
    c.post('/api/bridge/session-changes/'+job['id']+'/report',headers=bh,json={}).raise_for_status()
    assert c.get(base).json()['changes'][0]['status']=='complete'
    assert c.get(base).json()['managers'][0]['project']=='User project'
    assert claim(c,bh,new) is None
    snap=c.get('/api/rooms/'+room['id']).json()
    assert not next(x for x in snap['members'] if x['id']==new['id'])['enabled']
    with store.tx() as db:
        assert db.execute('SELECT status FROM deliveries WHERE id=?',(peer['id'],)).fetchone()[0]=='running'
        assert db.execute('SELECT status FROM deliveries WHERE id=?',(running['id'],)).fetchone()[0]=='cancelled'
        assert db.execute('SELECT timeout_seconds FROM agent_settings WHERE member=?',(new['id'],)).fetchone()[0]==0
    private=c.get('/api/rooms/'+room['id']+'/messages').json()
    assert any(x['message_kind']=='session_change_result' for x in private)
    public=c.get('/api/rooms/'+room['id']+'/messages',headers={'Authorization':'Bearer '+ss[1]['token']}).json()
    assert all(not x['message_kind'].startswith('session_change') for x in public)
    timeline=c.get('/api/rooms/'+room['id']+'/timeline').json()
    assert any(x['message_kind']=='session_change_result' and x['private_to']==m['id'] for x in timeline['blocks'])
    send(c,room,'@新增成员 开始');assert claim(c,bh,new)


def test_scope_stale_revision_and_lease_are_enforced(changes):
    c,room,b,bh,ss,store,m,mh,base=changes;job,payload=submit(changes)
    for headers in (bh,mh,{'Authorization':'Bearer '+ss[0]['token']}):
        assert c.post(base,headers=headers,json=payload).status_code==403
    for headers in (bh,{'Authorization':'Bearer '+ss[0]['token']}):
        assert c.post('/api/session-changes/'+job['id']+'/apply',headers=headers,json={}).status_code==403
    assert c.post('/api/session-changes/'+job['id']+'/apply',headers=mh,json={}).status_code==409
    apply(changes,job)
    assert register(changes,job,{**ROLE,'name':'unauthorized'}).status_code==403
    assert register(changes,job,session_type='manager').status_code==403
    assert c.post('/api/bridge/session-changes/'+job['id']+'/remove',headers=bh,json={'member':ss[0]['id']}).status_code==403
    assert c.post(base,json={**payload,'nonce':uuid.uuid4().hex}).status_code==409
    c.post(base+'/'+job['id']+'/control',json={'action':'cancel'}).raise_for_status()
    assert register(changes,job).status_code==409
    assert c.post(base,json={**payload,'nonce':uuid.uuid4().hex,'expected':'stale'}).status_code==409


def test_manager_final_fallback_failure_retry_and_stop(changes):
    c,room,b,bh,ss,store,m,mh,base=changes;job,_=submit(changes)
    task=claim(c,bh,m)
    c.post('/api/bridge/deliveries/'+task['id']+'/finish',headers=bh,json={'lease':task['lease'],'error':'network interrupted'}).raise_for_status()
    assert c.get(base).json()['changes'][0]['status']=='failed'
    assert c.post('/api/rooms/'+room['id']+'/deliveries/'+task['id']+'/retry',json={}).status_code==409
    retry=c.post(base+'/'+job['id']+'/control',json={'action':'retry'}).json()
    assert retry['delivery']!=task['id'] and retry['id']==job['id']
    task2=claim(c,bh,m)
    c.post('/api/bridge/deliveries/'+task2['id']+'/finish',headers=bh,json={'lease':task2['lease'],'body':'@owner {"action":"apply"}'}).raise_for_status()
    assert c.get(base).json()['changes'][0]['status']=='applying'
    c.post('/api/rooms/'+room['id']+'/control',json={'action':'stop-all'}).raise_for_status()
    assert c.get(base).json()['changes'][0]['status']=='cancelled'
    assert register(changes,job).status_code==409


def test_offline_queue_pause_and_manager_prose_cannot_claim_success(changes):
    c,room,b,bh,ss,store,m,mh,base=changes
    with store.tx() as db:db.execute('UPDATE members SET last_seen=0 WHERE id=?',(b['member_id'],))
    job,_=submit(changes)
    assert not c.get(base).json()['managers'][0]['online']
    assert c.get(base).json()['changes'][0]['status']=='pending'
    task=claim(c,bh,m)
    assert send(c,room,'@owner made up',mh,cause=task['message']['id']).status_code==409
    c.post('/api/bridge/deliveries/'+task['id']+'/finish',headers=bh,json={'lease':task['lease'],'body':'@owner finished'}).raise_for_status()
    assert c.get(base).json()['changes'][0]['status']=='failed'
    c.post(base+'/'+job['id']+'/control',json={'action':'retry'}).raise_for_status();apply(changes,job)
    c.post('/api/rooms/'+room['id']+'/members/'+b['member_id']+'/control',json={'action':'pause'}).raise_for_status()
    assert register(changes,job).status_code==409
    assert c.post('/api/bridge/heartbeat',headers=bh,json={'session_changes_supported':True}).json()['session_changes']==[]


@pytest.mark.parametrize('legacy_project',[False,True])
def test_local_apply_restart_preserves_existing_files_and_registration(changes,tmp_path,monkeypatch,legacy_project):
    c,room,b,bh,ss,store,m,mh,base=changes
    from bridge import cli
    from bridge.session_changes import SessionChanges
    class LocalClient:
        def __init__(self,*_):pass
        def call(self,path,data=None,**_):
            result=c.get(path,headers=bh) if data is None else c.post(path,headers=bh,json=data)
            if result.status_code!=200:raise APIError(result.status_code,result.json().get('error','test error'))
            return result.json()
    monkeypatch.setattr(cli,'Client',LocalClient)
    workspace=tmp_path/'workspace';workspace.mkdir()
    context=workspace/'manager.json';write_json(context,{'native_id':'original-native','definition':{'name':'manager','harness':'command'},'id':m['id'],'session_type':'manager','project_name':'Same project'})
    config=workspace/'bridge.json';write_json(config,{'url':'http://testserver','token':'test-token','id':b['member_id'],'name':'bridge','kind':'bridge','room':room,'workspace':str(workspace),'project_name':'Same project','sessions':[{'id':m['id'],'name':'manager','context':str(context)}]})
    if legacy_project:
        old=read_json(config);old.pop('project_name');write_json(config,old)
    loaded=Bridge(config);assert loaded.config['project_name']=='Same project'
    import threading
    bridge=SimpleNamespace(path=config,config=loaded.config,client=LocalClient(),stop=threading.Event(),redact=str,lock=threading.RLock(),sessions={},running={})
    job,_=submit(changes);apply(changes,job)
    # Simulate report loss after durable local registration.
    original=bridge.client.call
    def interrupted(path,data=None,**kw):
        if path.endswith('/report'):raise ConnectionError('offline')
        return original(path,data,**kw)
    bridge.client.call=interrupted
    SessionChanges(bridge).apply(job['id'])
    saved=read_json(config);assert len(saved['sessions'])==2
    added=read_json(saved['sessions'][1]['context']);marker=__import__('pathlib').Path(added['temp_dir'])/'keep.txt';marker.write_text('keep')
    bridge.client.call=original
    # A new applier after restart uses the stable config; no duplicate identity.
    SessionChanges(bridge).apply(job['id'])
    assert c.get(base).json()['changes'][0]['status']=='complete'
    assert read_json(config)==saved and read_json(context)['native_id']=='original-native'
    assert added['workspace']==str(workspace) and added['project_name']=='Same project' and not added['native_id']
    job2,_=submit(changes,additions=[],removals=[added['id']])
    bridge.sessions[added['id']]=added
    target={'cancel':threading.Event()};other={'cancel':threading.Event()}
    bridge.running={added['id']:target,'independent':other}
    # Close previous manager delivery first (bridge report can precede its reply).
    with store.tx() as db:db.execute("UPDATE deliveries SET status='done' WHERE id=?",(job['delivery'],))
    apply(changes,job2);SessionChanges(bridge).apply(job2['id'])
    assert c.get(base).json()['changes'][0]['status']=='complete'
    assert marker.read_text()=='keep' and read_json(config)==saved
    assert target['cancel'].is_set() and not other['cancel'].is_set()


def test_deployment_drain_keeps_active_lease_and_pending_messages(changes):
    c,room,b,bh,ss,store,m,mh,base=changes
    send(c,room,'@claude1 long task');active=claim(c,bh,ss[0])
    send(c,room,'@codex1 queued task')
    with store.tx() as db:db.execute('UPDATE members SET claims_paused_until=? WHERE id=?',(time.time()+90,b['member_id']))
    peer=claim(c,bh,ss[1]);assert peer  # Busy workers can still enlist peers.
    c.post('/api/bridge/deliveries/'+peer['id']+'/finish',headers=bh,json={'lease':peer['lease'],'body':'@owner peer finished'}).raise_for_status()
    heartbeat=c.post('/api/bridge/heartbeat',headers=bh,json={'leases':[{'id':active['id'],'lease':active['lease']}]}).json()
    assert active['id'] in heartbeat['valid_leases']
    assert c.get(base).json()['managers'][0]['project']=='User project'
    c.post('/api/bridge/deliveries/'+active['id']+'/finish',headers=bh,json={'lease':active['lease'],'body':'@owner completed normally'}).raise_for_status()
    send(c,room,'@codex1 next queued task')
    assert claim(c,bh,ss[1]) is None
    # A disconnected updater must never leave the conversation stuck.
    with store.tx() as db:db.execute('UPDATE members SET claims_paused_until=? WHERE id=?',(time.time()-1,b['member_id']))
    task=claim(c,bh,ss[1]);assert task['message']['body']=='@codex1 next queued task' and task['round_budget']['max']==8
