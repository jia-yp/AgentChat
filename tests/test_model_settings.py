import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bridge.adapters import build_command
from bridge.runner import Bridge
from bridge.common import read_json, write_json
from server.app import create_app


@pytest.fixture
def model_system(tmp_path):
    app=create_app(tmp_path/'data');c=TestClient(app)
    c.post('/api/bootstrap',json={'username':'tester','password':'model-test-password','setup_code':(tmp_path/'data/bootstrap-code').read_text()}).raise_for_status()
    room=c.post('/api/rooms',json={'name':'模型测试'}).json()
    bridge=c.post('/api/agent/join',headers={'Authorization':'Bearer '+room['join_token']},json={'name':'setup-models'}).json()
    bh={'Authorization':'Bearer '+bridge['token']}
    member=c.post('/api/bridge/sessions',headers=bh,json={'name':'worker','role':'fixture','harness':'claude','model':'model-a','model_control':True}).json()
    return c,room,bridge,bh,member,app.state.store


def settings(c,room,member):
    return next(m for m in c.get('/api/rooms/'+room['id']).json()['members'] if m['id']==member['id'])['model_settings']


def test_model_access_versions_and_legacy_heartbeat(model_system):
    c,r,b,bh,m,store=model_system
    path=f'/api/rooms/{r["id"]}/members/{m["id"]}/model'
    payload={'model':'model-b','revision':1}
    assert TestClient(c.app).post(path,json=payload).status_code==401
    assert c.post(path,headers={'Authorization':'Bearer '+m['token']},json=payload).status_code==403
    other=c.post('/api/rooms',json={'name':'other'}).json()
    assert c.post(f'/api/rooms/{other["id"]}/members/{m["id"]}/model',json=payload).status_code==403
    peer=c.post('/api/agent/join',headers={'Authorization':'Bearer '+r['join_token']},json={'name':'setup-peer'}).json()
    assert c.post(path,headers={'Authorization':'Bearer '+peer['token']},json=payload).status_code==403
    assert c.post(path,json=payload).json()=={'model':'model-b','revision':2}
    assert c.post(path,json={'model':'lost-update','revision':1}).status_code==409
    assert c.post(path,json={'model':'--malicious argument','revision':2}).status_code==400
    assert c.post(path,json={'model':'x'*201,'revision':2}).status_code==422
    assert c.post(path,json={'model':'','revision':2}).json()=={'model':'','revision':3}
    heartbeat={'sessions':[{'id':m['id'],'model_control':True,'model':'model-a','config_revision':1}]}
    result=c.post('/api/bridge/heartbeat',headers=bh,json=heartbeat).json()
    assert result['model_settings']==[{'id':m['id'],'model':'','revision':3,'catalog_refresh':0,'reasoning_effort':None,'timeout_seconds':None}]
    assert settings(c,r,m)['applied_revision']==1
    heartbeat['sessions'][0].update(model='',config_revision=3)
    c.post('/api/bridge/heartbeat',headers=bh,json=heartbeat).raise_for_status()
    assert settings(c,r,m)['applied_revision']==3
    heartbeat['sessions'][0]['model']='wrong-model'
    c.post('/api/bridge/heartbeat',headers=bh,json=heartbeat).raise_for_status()
    assert settings(c,r,m)['applied_revision']==0
    # A downgraded client is visibly unsupported rather than falsely synchronized.
    c.post('/api/bridge/heartbeat',headers=bh,json={'sessions':[{'id':m['id']}]}).raise_for_status()
    assert not settings(c,r,m)['supported']


def test_initial_model_migration_and_claim_snapshot(model_system):
    c,r,b,bh,m,store=model_system
    legacy=c.post('/api/bridge/sessions',headers=bh,json={'name':'legacy','role':'fixture','harness':'codex'}).json()
    c.post('/api/bridge/heartbeat',headers=bh,json={'sessions':[{'id':legacy['id'],'model':'local-original','model_control':True,'config_revision':0}]}).raise_for_status()
    assert settings(c,r,legacy)['model']=='local-original'
    path=f'/api/rooms/{r["id"]}/members/{m["id"]}/model'
    c.post(f'/api/rooms/{r["id"]}/messages',json={'body':'@worker task','nonce':'one'}).raise_for_status()
    c.post(path,json={'model':'model-new','revision':1}).raise_for_status()
    claimed=c.post(f'/api/bridge/sessions/{m["id"]}/claim',headers=bh,json={}).json()['delivery']
    assert claimed['model_settings']=={'model':'model-new','revision':2,'reasoning_effort':None,'timeout_seconds':None}
    c.post(path,json={'model':'model-next','revision':2}).raise_for_status()
    assert claimed['model_settings']['model']=='model-new'
    from server.core import Store
    reopened=Store(store.path)
    assert next(x for x in reopened.snapshot(r['id'],store.identity(b['token']))['members'] if x['id']==m['id'])['model_settings']['model']=='model-next'


def test_old_database_adds_settings_without_touching_messages(tmp_path):
    from server.core import Store,SCHEMA
    path=tmp_path/'old.sqlite3'
    old_schema=SCHEMA.split('CREATE TABLE IF NOT EXISTS agent_settings')[0]
    with sqlite3.connect(path) as db:
        db.executescript(old_schema)
        db.execute("INSERT INTO rooms(id,name,owner) VALUES('room','existing','owner')")
        db.execute("INSERT INTO messages(room,sender,body) VALUES('room','owner','existing message')")
    store=Store(path)
    with store.tx() as db:
        assert db.execute('SELECT body FROM messages').fetchone()[0]=='existing message'
        assert db.execute('SELECT COUNT(*) FROM agent_settings').fetchone()[0]==0


def test_bridge_model_changes_wait_for_idle_and_survive_restart(tmp_path):
    context=tmp_path/'session.json';config=tmp_path/'bridge.json'
    role={'harness':'codex','role':'fixture','model':'old','args':['--model','extra-old','--skip-git-repo-check']}
    write_json(context,{'id':'worker','name':'worker','definition':role,'native_id':'exact-native','context':str(context),'token':'test'})
    write_json(config,{'url':'http://127.0.0.1:1','token':'bridge-test','room':{'id':'room','name':'test'},'workspace':str(tmp_path),'sessions':[{'id':'worker','context':str(context)}]})
    bridge=Bridge(config);session=bridge.sessions['worker']
    bridge.running['worker']={}
    bridge.apply_model(session,{'model':'new','revision':2})
    assert read_json(context)['definition']['model']=='old'
    bridge.running.clear()
    bridge.apply_model(session,{'model':'new','revision':2})
    restored=Bridge(config).sessions['worker']
    assert restored['native_id']=='exact-native' and restored['definition']['model']=='new'
    assert restored['setup_definition']==role
    restored['definition']['executable']='codex'
    command=build_command(restored,tmp_path,tmp_path)
    assert command.count('-m')==1 and 'extra-old' not in command and 'new' in command
    bridge.apply_model(session,{'model':'','revision':3})
    session['definition']['executable']='codex'
    command=build_command(session,tmp_path,tmp_path)
    assert '-m' not in command and '--model' not in command


@pytest.mark.parametrize('harness',['codex','claude','antigravity'])
def test_model_passed_when_resuming_native_session(harness,tmp_path):
    session={'name':'worker','native_id':'exact-native','definition':{'harness':harness,'model':'provider/model-new','executable':harness}}
    command=build_command(session,tmp_path,tmp_path)
    assert 'provider/model-new' in command and 'exact-native' in command


def test_custom_model_placeholder_and_unsupported_commands(tmp_path):
    from bridge.model_settings import supports_model
    role={'harness':'command','command':['tool','--model','{model}'],'model':'provider/model-a'}
    assert supports_model(role)
    assert build_command({'id':'agent','name':'worker','definition':role},tmp_path,tmp_path)==['tool','--model','provider/model-a']
    assert not supports_model({'harness':'command','command':['fixed-model-tool']})
