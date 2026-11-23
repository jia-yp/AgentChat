import concurrent.futures
import io
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge.common import read_json
from bridge.connect import parse_setup_link
from server.app import create_app
from server.core import Store


ROOT=Path(__file__).resolve().parents[1]
ROLES=[{'name':'执行者','harness':'command','role':'完成指定任务','command':['fixture']}]


@pytest.fixture
def setup_system(tmp_path):
    app=create_app(tmp_path);c=TestClient(app)
    c.post('/api/bootstrap',json={'username':'tester','password':'test-setup-password','setup_code':(tmp_path/'bootstrap-code').read_text()}).raise_for_status()
    room=c.post('/api/rooms',json={'name':'链接接入测试'}).json()
    return c,room,app.state.store


def issue(c,room,roles=None,workspace='E:\\project',**options):
    secret=secrets.token_urlsafe(32)
    revision=c.get(f'/api/rooms/{room["id"]}/setup-profile').json()['revision']
    payload={'workspace':workspace,'roles':roles or ROLES,'revision':revision,'nonce':uuid.uuid4().hex,'credential':secret,**options}
    response=c.post(f'/api/rooms/{room["id"]}/setup-invitations',json=payload)
    assert response.status_code==200,response.text
    return response.json(),secret,payload


def claim(c,invite,secret,name='接入助手'):
    payload={'name':name,'claim_id':uuid.uuid4().hex,'bridge_token':secrets.token_urlsafe(32)}
    response=c.post(f'/api/setup/requests/{invite["id"]}/claim',headers={'Authorization':'Bearer '+secret},json=payload)
    assert response.status_code==200,response.text
    return response.json(),{'Authorization':'Bearer '+payload['bridge_token']},payload


def register_and_heartbeat(c,bh,roles=None):
    sessions=[]
    for role in roles or ROLES:
        response=c.post('/api/bridge/sessions',headers=bh,json={**role,'credential':secrets.token_urlsafe(32)})
        assert response.status_code==200,response.text
        sessions.append(response.json())
    manager=c.post('/api/bridge/sessions',headers=bh,json={'name':'manager','harness':'command','role':'setup agent','session_type':'manager'}).json()
    c.post('/api/bridge/heartbeat',headers=bh,json={'sessions':[{'id':s['id']} for s in [*sessions,manager]]}).raise_for_status()
    return sessions


def test_setup_profile_private_durable_and_versioned(setup_system):
    c,room,store=setup_system;path=f'/api/rooms/{room["id"]}/setup-profile'
    assert c.get(path).json()['revision']==0
    assert TestClient(c.app).get(path).status_code==401
    payload={'workspace':'E:\\项目','roles':ROLES,'revision':0}
    assert c.put(path,json=payload).json()['revision']==1
    assert c.put(path,json=payload).status_code==409
    loaded=Store(store.path).setup_profile(room['id'],c.get('/api/me').json())
    assert loaded['roles']==ROLES and loaded['workspace']=='E:\\项目'
    other=c.post('/api/rooms',json={'name':'other'}).json()
    joined=c.post('/api/agent/join',headers={'Authorization':'Bearer '+other['join_token']},json={'name':'other'}).json()
    assert c.get(path,headers={'Authorization':'Bearer '+joined['token']}).status_code==403


def test_inherited_location_is_optional_and_manual_fields_are_required(setup_system):
    c,room,store=setup_system;path=f'/api/rooms/{room["id"]}/setup-profile'
    assert c.get(path).json()['inherit_manager'] is True
    payload={'inherit_manager':True,'roles':ROLES,'revision':0}
    assert c.put(path,json=payload).json()['revision']==1
    loaded=Store(store.path).setup_profile(room['id'],c.get('/api/me').json())
    assert loaded['inherit_manager'] is True and loaded['workspace']==loaded['project_name']==''
    # Hidden form drafts cannot accidentally override the inherited selection.
    assert c.put(path,json={**payload,'revision':1,'workspace':'ignored','project_name':'ignored'}).json()['revision']==1
    manual={'inherit_manager':False,'workspace':'E:\\project','project_name':'','roles':ROLES,'revision':1}
    assert c.put(path,json=manual).status_code==400
    assert c.put(path,json={**manual,'workspace':'','project_name':'Project'}).status_code==400
    assert c.put(path,json={**manual,'project_name':'Project'}).json()['revision']==2
    assert c.get(path).json()['project_name']=='Project' and not c.get(path).json()['inherit_manager']


def test_inherited_claim_freezes_location_and_rejects_old_clients(setup_system):
    c,room,_=setup_system
    inv,secret,issued=issue(c,room,workspace='',inherit_manager=True)
    path='/api/setup/requests/'+inv['id'];headers={'Authorization':'Bearer '+secret}
    profile=c.get(path,headers=headers).json()
    assert profile['inherit_manager'] is True and not profile['workspace'] and not profile['resolved_workspace']
    payload={'name':'deputy','claim_id':uuid.uuid4().hex,'bridge_token':secrets.token_urlsafe(32)}
    assert c.post(path+'/claim',headers=headers,json=payload).status_code==400
    assert c.get(path,headers=headers).json()['status']=='pending'
    payload.update(workspace='E:\\real-project',project_name='Owning project')
    first=c.post(path+'/claim',headers=headers,json=payload);first.raise_for_status()
    assert c.post(path+'/claim',headers=headers,json=payload).json()==first.json()
    for changed in ({'workspace':'E:\\different'},{'project_name':'A different project'}):
        assert c.post(path+'/claim',headers=headers,json={**payload,**changed}).status_code==409
    profile=c.get(path,headers=headers).json()
    assert profile['resolved_workspace']==payload['workspace'] and profile['resolved_project_name']==payload['project_name']
    summary=c.get('/api/rooms/'+room['id']+'/setup-profile').json()['setup']
    assert summary['resolved_project_name']=='Owning project'
    assert c.post('/api/rooms/'+room['id']+'/setup-invitations',json={**issued,'inherit_manager':False,'workspace':'E:\\manual','project_name':'Manual'}).status_code==409


def test_explicit_manual_project_is_part_of_invitation_identity(setup_system):
    c,room,_=setup_system;inv,secret,payload=issue(c,room,project_name='User project')
    assert c.post('/api/rooms/'+room['id']+'/setup-invitations',json={**payload,'project_name':'Changed'}).status_code==409
    path='/api/setup/requests/'+inv['id'];headers={'Authorization':'Bearer '+secret}
    claim_payload={'name':'deputy','claim_id':uuid.uuid4().hex,'bridge_token':secrets.token_urlsafe(32),'project_name':'Wrong'}
    assert c.post(path+'/claim',headers=headers,json=claim_payload).status_code==409
    claim_payload['project_name']='User project'
    assert c.post(path+'/claim',headers=headers,json=claim_payload).json()['project_name']=='User project'


def test_invitation_claim_and_session_registration_are_retryable(setup_system):
    c,room,store=setup_system
    inv,secret,payload=issue(c,room)
    repeated=c.post(f'/api/rooms/{room["id"]}/setup-invitations',json=payload)
    assert repeated.json()==inv
    assert c.post(f'/api/rooms/{room["id"]}/setup-invitations',json={**payload,'workspace':'different'}).status_code==409
    request_path=f'/api/setup/requests/{inv["id"]}'
    assert c.get(request_path).status_code==401
    assert c.get(request_path,headers={'Authorization':'Bearer '+secret}).json()['roles']==ROLES
    bridge,bh,claim_payload=claim(c,inv,secret)
    assert c.post(request_path+'/claim',headers={'Authorization':'Bearer '+secret},json=claim_payload).json()==bridge
    assert c.post(request_path+'/claim',headers={'Authorization':'Bearer '+secret},json={**claim_payload,'claim_id':uuid.uuid4().hex}).status_code==409
    session_payload={**ROLES[0],'credential':secrets.token_urlsafe(32)}
    first=c.post('/api/bridge/sessions',headers=bh,json=session_payload).json()
    assert c.post('/api/bridge/sessions',headers=bh,json=session_payload).json()==first
    assert c.post('/api/bridge/sessions',headers=bh,json={**session_payload,'credential':secrets.token_urlsafe(32)}).status_code==409
    snapshot=c.get('/api/rooms/'+room['id'],headers=bh).json()
    assert 'setup' not in snapshot and all(not m['enabled'] for m in snapshot['members'] if m['kind']=='agent')
    with store.tx() as db:
        assert db.execute('SELECT token_hash FROM setup_requests WHERE id=?',(inv['id'],)).fetchone()[0]!=secret


def test_completion_requires_readiness_rotates_once_and_keeps_identities(setup_system):
    c,room,store=setup_system;inv,secret,_=issue(c,room);bridge,bh,_=claim(c,inv,secret)
    path=f'/api/bridge/setup/{inv["id"]}/complete'
    assert c.post(path,json={}).status_code==403
    assert c.post(path,headers=bh,json={}).status_code==425
    c.post('/api/bridge/heartbeat',headers=bh,json={})
    assert c.post(path,headers=bh,json={}).status_code==409
    sessions=register_and_heartbeat(c,bh)
    actor=store.identity(bh['Authorization'][7:])
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        receipts=list(pool.map(lambda _:store.complete_setup(inv['id'],actor),range(2)))
    assert receipts[0]==receipts[1] and receipts[0]['token_rotated'] is True
    history=c.get(f'/api/rooms/{room["id"]}/messages').json()
    assert len(history)==1 and history[0]['sender_kind']=='bridge' and history[0]['deliveries']==[]
    assert c.get('/api/setup/requests/'+inv['id'],headers={'Authorization':'Bearer '+secret}).status_code==401
    assert c.post('/api/agent/join',headers={'Authorization':'Bearer '+secret},json={'name':'late'}).status_code==401
    assert c.get('/api/setup/requests/'+inv['id'],headers=bh).json()['status']=='complete'
    assert c.get('/api/me',headers=bh).status_code==200
    assert c.get('/api/me',headers={'Authorization':'Bearer '+sessions[0]['token']}).status_code==200
    member=next(m for m in c.get('/api/rooms/'+room['id']).json()['members'] if m['kind']=='agent')
    assert not member['enabled'] and not member['native_id']
    assert c.get(f'/api/rooms/{room["id"]}/setup-profile').json()['setup']['status']=='complete'


def test_late_completion_preserves_newer_invitation(setup_system):
    c,room,_=setup_system;first,secret,_=issue(c,room);_,bh,_=claim(c,first,secret)
    register_and_heartbeat(c,bh)
    newer,new_secret,_=issue(c,room)
    result=c.post(f'/api/bridge/setup/{first["id"]}/complete',headers=bh,json={}).json()
    assert result['token_rotated'] is False
    assert c.get('/api/setup/requests/'+newer['id'],headers={'Authorization':'Bearer '+new_secret}).status_code==200
    assert c.post(f'/api/bridge/setup/{first["id"]}/complete',headers=bh,json={}).json()==result
    assert c.get('/api/setup/requests/'+newer['id'],headers={'Authorization':'Bearer '+new_secret}).status_code==200


def test_expired_and_superseded_links_fail_without_claiming(setup_system):
    c,room,store=setup_system;inv,secret,_=issue(c,room)
    with store.tx() as db:db.execute('UPDATE setup_requests SET expires=? WHERE id=?',(time.time()-1,inv['id']))
    assert c.get('/api/setup/requests/'+inv['id'],headers={'Authorization':'Bearer '+secret}).status_code==401
    assert c.get(f'/api/rooms/{room["id"]}/setup-profile').json()['setup']['status']=='expired'
    inv,secret,_=issue(c,room);issue(c,room)
    assert c.get('/api/setup/requests/'+inv['id'],headers={'Authorization':'Bearer '+secret}).status_code==401


def test_link_parser_keeps_token_out_of_http_url():
    secret=secrets.token_urlsafe(32);request='a'*24
    assert parse_setup_link(f'https://chat.example/agent-connect/setup/{request}#token={secret}')==('https://chat.example/agent-connect',request,secret)
    for bad in (f'https://chat.example/setup/{request}?token={secret}',f'https://user:pass@chat.example/setup/{request}#token={secret}',f'https://chat.example/setup/{request}#token={secret}&token={secret}'):
        with pytest.raises(ValueError):parse_setup_link(bad)


def wait_for(fn,timeout=20):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        try:
            value=fn()
            if value:return value
        except (OSError,httpx.HTTPError):pass
        time.sleep(.15)
    raise AssertionError('Timed out waiting for setup integration')


@pytest.mark.parametrize('location_mode',['legacy','manual','inherit'])
def test_downloaded_client_short_link_completion_and_resume(tmp_path,location_mode):
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env={**os.environ,'AGENTCHAT_DATA':str(tmp_path/'data'),'AGENTCHAT_PORT':str(port),'PYTHONIOENCODING':'utf-8'}
    server=subprocess.Popen([sys.executable,'-m','server'],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    config_path=None
    try:
        c=httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=4)
        wait_for(lambda:c.get('/healthz').status_code==200)
        c.post('/api/bootstrap',json={'username':'tester','password':'short-link-password','setup_code':(tmp_path/'data/bootstrap-code').read_text()}).raise_for_status()
        room=c.post('/api/rooms',json={'name':'downloaded client'}).json()
        workspace=tmp_path/'workspace';workspace.mkdir()
        roles=[{'name':name,'harness':'command','role':'仅测试','command':[sys.executable,str(ROOT/'tests/fake_harness.py')]} for name in ('小龙','小克')]
        options={} if location_mode=='legacy' else {'inherit_manager':location_mode=='inherit','project_name':'手动项目' if location_mode=='manual' else ''}
        requested_workspace='' if location_mode=='inherit' else str(workspace)
        inv,secret,_=issue(c,room,roles,requested_workspace,**options)
        client_root=tmp_path/'client';client_root.mkdir()
        with zipfile.ZipFile(io.BytesIO(c.get('/downloads/agentchat-client.zip').content)) as archive:archive.extractall(client_root)
        cli=client_root/'agentchat.py';config_path=workspace/'.agentchat/test-link.json'
        link=f'{str(c.base_url).rstrip("/")}/setup/{inv["id"]}#token={secret}\n'
        identity=tmp_path/'creator.json';identity.write_text(json.dumps({'harness':'command','native_id':'exact-creator-session','workspace':str(workspace),'project_name':'创建者所属项目','command':[sys.executable,str(ROOT/'tests/fake_harness.py')]}),encoding='utf-8')
        result=subprocess.run([sys.executable,str(cli),'connect','--link-stdin','--config',str(config_path),'--name','setup-test','--manager-identity',str(identity)],input=link,encoding='utf-8',env=env,capture_output=True,timeout=55)
        assert result.returncode==0,result.stdout+result.stderr
        assert '"setup": "complete"' in result.stdout and secret not in result.stdout+result.stderr
        config=read_json(config_path);all_contexts=[read_json(s['context']) for s in config['sessions']]
        for directory in ('.agents','.claude','.agent'):
            for skill in ('agentchat-setup','agentchat-chat'):
                assert (workspace/directory/'skills'/skill/'SKILL.md').read_bytes()==(client_root/'skills'/skill/'SKILL.md').read_bytes()
        expected_name={'legacy':'downloaded client','manual':'手动项目','inherit':'创建者所属项目'}[location_mode]
        assert config['workspace']==str(workspace) and config['project_name']==expected_name
        assert all(s['project_name']==expected_name and s['workspace']==str(workspace) for s in all_contexts)
        assert len(all_contexts)==3 and not any((Path(s['temp_dir'])/'executions.txt').exists() for s in all_contexts)
        manager=all_contexts[0];contexts=all_contexts[1:]
        assert manager['session_type']=='manager' and manager['native_id']=='exact-creator-session'
        assert Path(manager['manager_workspace']).is_dir() and (Path(manager['context']).parent/'MANAGER.md').is_file()
        assert all(s['project_key']==manager['project_key'] for s in contexts)
        assert c.get('/api/setup/requests/'+inv['id'],headers={'Authorization':'Bearer '+secret}).status_code==401
        messages_path=f'/api/rooms/{room["id"]}/messages'
        assert len(c.get(messages_path).json())==1
        retry=subprocess.run([sys.executable,str(cli),'connect','--link-stdin','--config',str(config_path)],input=link,encoding='utf-8',env=env,capture_output=True,timeout=25)
        assert retry.returncode==0,retry.stdout+retry.stderr
        assert len(c.get(messages_path).json())==1
        c.post(messages_path,json={'body':'@小龙 执行，@小克只是指代','nonce':'first'}).raise_for_status()
        wait_for(lambda:len(c.get(messages_path).json())==3)
        native=read_json(contexts[0]['context'])['native_id'];assert native
        config_path.with_suffix('.stop').write_text('stop')
        wait_for(lambda:read_json(config_path.with_suffix('.status.json')).get('stopped'))
        resumed=subprocess.run([sys.executable,str(cli),'start','--config',str(config_path)],env=env,capture_output=True,timeout=25)
        assert resumed.returncode==0,resumed.stderr.decode(errors='replace')
        c.post(messages_path,json={'body':'@小龙 继续','nonce':'second'}).raise_for_status()
        wait_for(lambda:len(c.get(messages_path).json())==5)
        assert read_json(contexts[0]['context'])['native_id']==native
        assert (Path(contexts[0]['temp_dir'])/'executions.txt').read_text()=='2'
        assert not (Path(contexts[1]['temp_dir'])/'executions.txt').exists()
        # A new invitation discovers the custom-named legacy config automatically.
        newer,new_secret,_=issue(c,room,roles,requested_workspace,**options)
        new_link=f'{str(c.base_url).rstrip("/")}/setup/{newer["id"]}#token={new_secret}\n'
        reuse=['--config',str(config_path)] if location_mode=='inherit' else []
        reconnected=subprocess.run([sys.executable,str(cli),'connect','--link-stdin',*reuse],input=new_link,encoding='utf-8',env=env,capture_output=True,timeout=25)
        assert reconnected.returncode==0,reconnected.stdout+reconnected.stderr
        assert read_json(config_path)['id']==config['id']
        assert read_json(config_path)['project_name']==expected_name
        assert read_json(contexts[0]['context'])['native_id']==native
        assert len([m for m in c.get('/api/rooms/'+room['id']).json()['members'] if m['kind']=='bridge'])==1
        managers=[m for m in c.get('/api/rooms/'+room['id']).json()['members'] if m.get('session_type')=='manager']
        assert len(managers)==1 and managers[0]['id']==manager['id']
        c.post(messages_path,json={'body':'@manager private-manager-check','nonce':'manager-first'}).raise_for_status()
        wait_for(lambda:c.get(messages_path+'?channel='+manager['id']).json()[-1]['sender_kind']=='agent')
        assert read_json(manager['context'])['native_id']=='exact-creator-session'
        assert all('private-manager-check' not in m['body'] for m in c.get(messages_path,headers={'Authorization':'Bearer '+contexts[0]['token']}).json())
        if location_mode=='inherit':
            # The downloaded bridge follows delete/restore without a restart or a new native session.
            control=f'/api/rooms/{room["id"]}/members/{contexts[0]["id"]}/control'
            c.post(control,json={'action':'delete'}).raise_for_status()
            wait_for(lambda:read_json(contexts[0]['context']).get('revoked'))
            restored=c.post(control,json={'action':'restore'});restored.raise_for_status()
            assert restored.json()['requires_bridge_restart'] is False
            wait_for(lambda:not read_json(contexts[0]['context']).get('revoked'))
            c.post(messages_path,json={'body':'@小龙 恢复后继续','nonce':'after-restore'}).raise_for_status()
            wait_for(lambda:(Path(contexts[0]['temp_dir'])/'executions.txt').read_text()=='3')
            assert read_json(contexts[0]['context'])['native_id']==native
    finally:
        try:
            if config_path and config_path.exists():
                config_path.with_suffix('.stop').write_text('stop')
                if config_path.with_suffix('.status.json').exists():
                    wait_for(lambda:read_json(config_path.with_suffix('.status.json')).get('stopped'),timeout=12)
        finally:
            server.terminate()
            try:server.wait(timeout=8)
            except subprocess.TimeoutExpired:server.kill();server.wait(timeout=8)


def test_new_setup_requires_manager_but_legacy_links_keep_their_behavior(setup_system):
    c,room,store=setup_system;inv,secret,_=issue(c,room);_,bh,_=claim(c,inv,secret)
    profile=c.get('/api/setup/requests/'+inv['id'],headers=bh).json()
    assert profile['manager_policy']=='setup-agent'
    s=c.post('/api/bridge/sessions',headers=bh,json={**ROLES[0]}).json()
    c.post('/api/bridge/heartbeat',headers=bh,json={'sessions':[{'id':s['id']}]}).raise_for_status()
    path='/api/bridge/setup/'+inv['id']+'/complete'
    result=c.post(path,headers=bh,json={});assert result.status_code==409 and 'manager' in result.text
    assert c.get('/api/setup/requests/'+inv['id'],headers={'Authorization':'Bearer '+secret}).status_code==200
    with store.tx() as db:db.execute('UPDATE setup_requests SET manager_required=0 WHERE id=?',(inv['id'],))
    c.post(path,headers=bh,json={}).raise_for_status()


def test_legacy_manager_roles_are_not_copied_into_new_profiles_or_templates(setup_system):
    c,room,store=setup_system
    roles=[{'name':'old-manager','session_type':'manager','harness':'codex','role':'old manager'},*ROLES]
    c.put('/api/rooms/'+room['id']+'/setup-profile',json={'workspace':'E:\\project','roles':roles,'revision':0}).raise_for_status()
    assert c.get('/api/rooms/'+room['id']+'/setup-profile').json()['roles']==ROLES
    saved=c.post('/api/role-templates',json={'name':'old roles','roles':roles});saved.raise_for_status()
    assert saved.json()['roles']==ROLES
