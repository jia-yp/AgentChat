import json
import os
import subprocess
import sys

import pytest

from bridge.chat_mcp import handle, call_tool
from bridge.common import write_json
from bridge.uploads import MAX_FILE_BYTES


def test_chat_mcp_only_routes_scoped_calls_and_current_cause(monkeypatch):
    context={'room_id':'room','token':'secret','runtime':{'model':'test'}}
    calls=[]
    class Client:
        def call(self,path,data=None):calls.append((path,data));return {'ok':True}
    client=Client();monkeypatch.setenv('AGENTCHAT_CAUSE','123')
    response=handle({'id':1,'method':'tools/call','params':{'name':'send','arguments':{'body':'@target note','nonce':'stable','mode':'steer','task':'task:2'}}},context,client)
    assert not response['result']['isError']
    assert calls[0][0]=='/api/rooms/room/messages'
    assert calls[0][1]['cause']==123 and calls[0][1]['steer_task']=='task:2'
    for args in ({'url':'https://other'}, {'context':'other-session.json'}, {'token':'other'}, {'action':'manage'}):
        response=handle({'id':2,'method':'tools/call','params':{'name':'send','arguments':{**args,'body':'@target x','nonce':'x'}}},context,client)
        assert response['result']['isError']
    assert len(calls)==1


def test_chat_mcp_jsonrpc_stdout_handshake_without_model(tmp_path):
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    context=tmp_path/'session.json';write_json(context,{'kind':'agent','room_id':'room','url':'http://127.0.0.1:1','token':'secret'})
    messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}},
        {'jsonrpc':'2.0','method':'notifications/initialized'}, {'jsonrpc':'2.0','id':2,'method':'tools/list'},
        {'jsonrpc':'2.0','id':3,'method':'ping'}]
    result=subprocess.run([sys.executable,str(root/'agentchat.py'),'chat-mcp'],env={**os.environ,'AGENTCHAT_CONTEXT':str(context)},
        input='\n'.join(json.dumps(m) for m in messages)+'\n',text=True,encoding='utf-8',capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    responses=[json.loads(line) for line in result.stdout.splitlines()]
    assert len(responses)==3 and responses[0]['result']['capabilities']=={'tools':{}}
    assert {t['name'] for t in responses[1]['result']['tools']}=={'members','history','send'}
    assert 'secret' not in result.stdout


def test_mcp_tool_errors_do_not_expose_session_token():
    class Client:
        def call(self,path):raise RuntimeError('error containing mysecret')
    response=handle({'id':1,'method':'tools/call','params':{'name':'members'}},{'room_id':'room','token':'mysecret'},Client())
    assert response['result']['isError'] and 'mysecret' not in json.dumps(response)


def test_send_files_reuses_upload_ids_on_retry(tmp_path):
    report = tmp_path / '成果.bin'
    report.write_bytes(b'\x00data\r\n')
    scratch = tmp_path / '.agentchat' / 'rooms' / 'room' / 'sessions' / 'sender' / 'temp'
    scratch.mkdir(parents=True)
    empty = scratch / 'empty.txt'
    empty.touch()
    context = {'workspace': str(tmp_path), 'temp_dir': str(scratch), 'room_id': 'room',
               'token': 'secret', '_task_cause': 7}
    uploads, messages = [], []

    class Client:
        def upload(self, path, name, data):
            uploads.append((path, name, data))

        def call(self, path, data):
            messages.append((path, data))
            return {'id': 9}

    args = {'body': '@admin 成果', 'nonce': 'finish-report', 'files': [report.name, str(empty)]}
    client = Client()
    for _ in range(2):
        assert call_tool(context, client, 'send', args) == {'id': 9}
    assert uploads[:2] == uploads[2:]
    assert [item[1:] for item in uploads[:2]] == [(report.name, report.read_bytes()), (empty.name, b'')]
    assert messages[0] == messages[1]
    assert messages[0][1]['attachment_ids'] == [item[0].rsplit('/', 1)[1] for item in uploads[:2]]
    assert messages[0][1]['cause'] == 7


def test_send_rejects_files_outside_workspace_or_in_private_state_before_uploading(tmp_path):
    workspace = tmp_path / 'project'
    workspace.mkdir()
    share = workspace / 'share.txt'
    share.touch()
    outside = tmp_path / 'outside.txt'
    outside.touch()
    paths = [outside, workspace, workspace / 'missing.bin']
    for relative in ('.agentchat/rooms/room/sessions/other/session.json',
                     '.agentchat/rooms/room/uploads/private/manager/private.txt',
                     '.agentchat/rooms/another/uploads/download.txt'):
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        paths.append(path)
    huge = workspace / 'huge.bin'
    with huge.open('wb') as stream:
        stream.truncate(MAX_FILE_BYTES + 1)
    paths.append(huge)

    context = {'workspace': str(workspace), 'room_id': 'room', 'token': 'secret'}
    for path in paths:
        with pytest.raises(ValueError):
            call_tool(context, None, 'send', {'body': '@admin file', 'nonce': 'same',
                                               'files': [share.name, str(path)]})


def test_send_rejects_a_symlink_outside_workspace(tmp_path):
    workspace = tmp_path / 'project'
    workspace.mkdir()
    outside = tmp_path / 'outside.txt'
    outside.touch()
    link = workspace / 'link.txt'
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip('symlink creation unavailable on this host')
    context = {'workspace': str(workspace), 'room_id': 'room', 'token': 'secret'}
    with pytest.raises(ValueError, match='workspace'):
        call_tool(context, None, 'send', {'body': '@admin file', 'nonce': 'same', 'files': [link.name]})
