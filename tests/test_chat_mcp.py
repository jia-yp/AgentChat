import json
import os
import subprocess
import sys

from bridge.chat_mcp import handle, call_tool
from bridge.common import write_json


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
