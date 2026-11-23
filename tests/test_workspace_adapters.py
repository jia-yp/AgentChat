import json
import uuid
from pathlib import Path

import pytest

from bridge import attachments,chat_mcp
from bridge.adapters import build_command
from bridge.permission_settings import antigravity_policy


def test_collaboration_keeps_native_denials_and_scopes_chat_tools(tmp_path,monkeypatch):
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    settings=tmp_path/'.gemini/antigravity-cli/settings.json';settings.parent.mkdir(parents=True)
    original={'permissions':{'deny':['command(danger)'],'ask':['read_url(private.example)'],'allow':['read_url(docs.example)']}}
    settings.write_text(json.dumps(original),encoding='utf-8')
    role={'harness':'antigravity','permissions':'collaborate'}
    session={'url':'https://chat.example.test/agent-connect','workspace':str(tmp_path/'workspace')}
    args=antigravity_policy(role,session);config=json.loads(args[args.index('--settings')+1])
    assert '--sandbox' in args and '--dangerously-skip-permissions' not in args
    assert config['toolPermission']=='proceed-in-sandbox'
    assert config['permissions']['deny']==original['permissions']['deny']
    assert config['permissions']['ask']==original['permissions']['ask']
    assert 'read_url(chat.example.test)' in config['permissions']['allow']
    assert 'mcp(agentchat/send)' in config['permissions']['allow']
    assert 'mcp(agentchat/sessions)' not in config['permissions']['allow']
    args=antigravity_policy(role,{**session,'session_type':'manager'})
    assert 'mcp(agentchat/sessions)' in json.loads(args[args.index('--settings')+1])['permissions']['allow']
    assert json.loads(settings.read_text())==original


def test_mcp_management_visibility_and_unmanaged_desktop_has_no_identity():
    message={'id':1,'method':'tools/list'}
    names=lambda context:[t['name'] for t in chat_mcp.handle(message,context,None)['result']['tools']]
    assert names({'session_type':'worker'})==['members','history','send']
    assert names({'session_type':'manager'})==['members','history','send','sessions']
    assert names({'unmanaged':True})==[]
    with pytest.raises(ValueError):chat_mcp.call_tool({'session_type':'worker'},None,'sessions',{'action':'list'})
    response=chat_mcp.handle({'id':2,'method':'tools/call','params':{'name':'members'}},{'unmanaged':True},None)
    assert response['result']['isError']


def test_attaching_existing_history_requires_exact_local_workspace(tmp_path,monkeypatch):
    native=str(uuid.uuid4());role={'harness':'claude','attach_native_id':native}
    monkeypatch.setattr(attachments,'native_metadata',lambda *a:{'workspace':str(tmp_path/'other')})
    with pytest.raises(ValueError,match='workspace'):attachments.inspect(role,tmp_path,{})
    monkeypatch.setattr(attachments,'native_metadata',lambda *a:{'workspace':str(tmp_path)})
    prepared=attachments.inspect(role,tmp_path,{})
    session={'definition':dict(role,executable='claude'),'name':'reviewer','requested_id':str(uuid.uuid4()),**prepared}
    cmd=build_command(session,tmp_path,tmp_path)
    assert cmd[cmd.index('--resume')+1]==native and '--fork-session' in cmd
    session['native_id']='forked-native'
    cmd=build_command(session,tmp_path,tmp_path)
    assert cmd[cmd.index('--resume')+1]=='forked-native' and '--fork-session' not in cmd
