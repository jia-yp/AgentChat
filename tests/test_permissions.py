import json

import pytest
from fastapi.testclient import TestClient

from bridge.adapters import build_command
from bridge.common import read_json, write_json
from bridge.permission_settings import options, report, codex_policy
from bridge.roles import role_definitions
from bridge.runner import Bridge
from server.app import create_app


@pytest.fixture
def system(tmp_path):
    app=create_app(tmp_path/'data');c=TestClient(app)
    c.post('/api/bootstrap',json={'username':'tester','password':'permission-test-password','setup_code':(tmp_path/'data/bootstrap-code').read_text()}).raise_for_status()
    room=c.post('/api/rooms',json={'name':'Permissions'}).json();base='/api/rooms/'+room['id']
    b=c.post('/api/agent/join',headers={'Authorization':'Bearer '+room['join_token']},json={'name':'bridge'}).json();bh={'Authorization':'Bearer '+b['token']}
    m=c.post('/api/bridge/sessions',headers=bh,json={'name':'worker','role':'fixture','harness':'codex'}).json()
    path=base+'/members/'+m['id']+'/permissions'
    def pulse(mode='inherit',revision=0,**extra):
        r=c.post('/api/bridge/heartbeat',headers=bh,json={'sessions':[{'id':m['id'],'permissions':{'supported':True,'mode':mode,'revision':revision,**extra}}]});r.raise_for_status();return r.json()
    pulse()
    return c,room,base,bh,m,path,pulse


def current(c,base,m):return next(x for x in c.get(base).json()['members'] if x['id']==m['id'])['permission_settings']


def test_only_owner_can_change_permissions_and_revisions(system):
    c,r,base,bh,m,path,pulse=system;payload={'mode':'full','revision':1}
    assert TestClient(c.app).post(path,json=payload).status_code==401
    manager=c.post('/api/bridge/sessions',headers=bh,json={'name':'manager','role':'manage','harness':'codex','session_type':'manager'}).json()
    for h in (bh,{'Authorization':'Bearer '+m['token']},{'Authorization':'Bearer '+manager['token']}):
        assert c.post(path,headers=h,json=payload).status_code==403
    other=c.post('/api/rooms',json={'name':'other'}).json()
    assert c.post('/api/rooms/'+other['id']+'/members/'+m['id']+'/permissions',json=payload).status_code==403
    assert c.post(path,json={'mode':'ssh','revision':1}).status_code==400
    assert c.post(path,json=payload).json()=={'mode':'full','revision':2}
    assert c.post(path,json={'mode':'workspace','revision':1}).status_code==409
    # Repeated saves are idempotent; config and acknowledgment stay separate.
    assert c.post(path,json={'mode':'full','revision':2}).json()=={'mode':'full','revision':2}
    assert current(c,base,m)['applied_mode']=='inherit'
    pulse('full',2);assert current(c,base,m)['applied_revision']==2
    pulse('inherit',2);assert current(c,base,m)['applied_revision']==0
    pulse('full',900);assert current(c,base,m)['applied_revision']==0
    # An old client cannot misleadingly show a saved permission as supported.
    c.post('/api/bridge/heartbeat',headers=bh,json={'sessions':[{'id':m['id']}]}).raise_for_status()
    assert not current(c,base,m)['supported']
    assert c.post(path,json={'mode':'workspace','revision':2}).status_code==409
    pulse();assert current(c,base,m)['mode']=='full'


def test_claim_uses_saved_permissions_without_touching_current_task(system):
    c,r,base,bh,m,path,pulse=system
    c.post(base+'/messages',json={'body':'@worker first','nonce':'1'}).raise_for_status()
    claim=c.post('/api/bridge/sessions/'+m['id']+'/claim',headers=bh,json={}).json()['delivery']
    assert claim['permission_settings']=={'mode':'inherit','revision':1}
    c.post(path,json={'mode':'full','revision':1}).raise_for_status()
    assert claim['permission_settings']=={'mode':'inherit','revision':1}
    assert pulse()['permission_settings']==[{'id':m['id'],'mode':'full','revision':2}]
    c.post('/api/bridge/deliveries/'+claim['id']+'/finish',headers=bh,json={'lease':claim['lease'],'body':'@tester done'}).raise_for_status()
    c.post(base+'/messages',json={'body':'@worker next','nonce':'2'}).raise_for_status()
    next_claim=c.post('/api/bridge/sessions/'+m['id']+'/claim',headers=bh,json={}).json()['delivery']
    assert next_claim['permission_settings']=={'mode':'full','revision':2}
    from server.core import Store
    reopened=Store(c.app.state.store.path)
    with reopened.tx() as db:assert db.execute('SELECT mode FROM agent_permissions WHERE member=?',(m['id'],)).fetchone()['mode']=='full'


def test_bridge_waits_for_idle_preserves_native_id_and_can_restrict_again(tmp_path):
    context=tmp_path/'session.json';config=tmp_path/'bridge.json'
    role={'harness':'claude','permission_mode':'acceptEdits','model':'unchanged','executable':'claude'}
    write_json(context,{'id':'worker','name':'worker','definition':role,'native_id':'same-native','context':str(context),'token':'test'})
    write_json(config,{'url':'http://127.0.0.1:1','token':'test','room':{'id':'room','name':'test'},'workspace':str(tmp_path),'sessions':[{'id':'worker','context':str(context)}]})
    bridge=Bridge(config);s=bridge.sessions['worker'];bridge.running['worker']={}
    bridge.apply_permissions(s,{'mode':'full','revision':2})
    assert s['definition'].get('permissions','inherit')=='inherit'
    bridge.running.clear();bridge.apply_permissions(s,{'mode':'full','revision':2})
    restored=Bridge(config).sessions['worker'];assert restored['native_id']=='same-native'
    assert report(restored)['mode']=='full' and report(restored)['revision']==2
    command=build_command(restored,tmp_path,tmp_path)
    assert 'bypassPermissions' in command and 'same-native' in command and 'unchanged' in command
    bridge.apply_permissions(s,{'mode':'inherit','revision':3})
    assert 'acceptEdits' in build_command(s,tmp_path,tmp_path) and 'bypassPermissions' not in build_command(s,tmp_path,tmp_path)
    bridge.apply_permissions(s,{'mode':'full','revision':2})
    assert s['definition']['permissions']=='inherit'


@pytest.mark.parametrize('mode,sandbox,network',[('inherit','workspace-write',None),('workspace','workspace-write',False),('network','workspace-write',True),('read-only','read-only',False),('full','danger-full-access',None)])
def test_codex_permission_flags_apply_to_resume(tmp_path,mode,sandbox,network):
    role={'harness':'codex','executable':'codex','permissions':mode}
    s={'definition':role,'native_id':'exact-id'};cmd=build_command(s,tmp_path,tmp_path)
    assert cmd[cmd.index('-s')+1]==sandbox and 'exact-id' in cmd
    assert cmd[cmd.index('-a')+1]=='never'
    if network is not None:assert 'sandbox_workspace_write.network_access='+json.dumps(network) in cmd
    assert codex_policy(role)[0]==sandbox


def test_claude_ssh_allowlist_preserves_scoped_chat_and_does_not_grant_arbitrary_shell(tmp_path):
    s={'definition':{'harness':'claude','executable':'claude','permissions':'ssh'},'native_id':'existing','context':'context.json'}
    cmd=build_command(s,tmp_path,tmp_path)
    assert cmd[cmd.index('--permission-mode')+1]=='acceptEdits'
    for tool in ('Bash(ssh *)','PowerShell(ssh *)','Bash(scp *)','Bash(sftp *)','mcp__agentchat__send'):assert tool in cmd
    assert 'Bash' not in cmd and 'bypassPermissions' not in cmd and '--resume' in cmd
    assert 'ssh *' in json.loads(cmd[cmd.index('--settings')+1])['sandbox']['excludedCommands']
    s['definition']['permissions']='edits';assert 'Bash(ssh *)' not in build_command(s,tmp_path,tmp_path)


def test_antigravity_full_access_explicitly_disables_sandbox(tmp_path):
    s={'definition':{'harness':'antigravity','executable':'agy','permissions':'full'},'native_id':'existing'}
    cmd=build_command(s,tmp_path,tmp_path)
    assert '--dangerously-skip-permissions' in cmd and '--sandbox=false' in cmd
    assert cmd[cmd.index('--mode')+1]=='accept-edits' and 'auto' not in cmd
    assert cmd[cmd.index('--conversation')+1]=='existing'
    s['definition']['permissions']='sandbox';cmd=build_command(s,tmp_path,tmp_path)
    assert '--sandbox' in cmd and '--dangerously-skip-permissions' not in cmd


@pytest.mark.parametrize('role',[{'harness':'command','command':['custom']},{'harness':'deepseek'},{'harness':'codex','args':['--dangerously-bypass-approvals-and-sandbox']}])
def test_unsupported_adapters_and_custom_overrides_are_not_falsely_managed(role):
    assert not options(role)
    with pytest.raises(ValueError):role_definitions([{'name':'worker','role':'task',**role,'permissions':'full'}])
    assert role_definitions([{'name':'worker','role':'task',**role}])
