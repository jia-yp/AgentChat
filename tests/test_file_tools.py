import json
import threading
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from bridge.common import APIError,read_json,write_json
from bridge.file_tools import FileTools
from bridge.runner import Bridge


def make_tools(tmp_path,client,**session_fields):
    session={'id':'worker','kind':'agent','name':'worker','room_id':'room','url':'https://chat.example.test',
             'token':'test-scoped-secret','context':str(tmp_path/'session.json'),'session_type':'worker',**session_fields}
    delivery={'id':'delivery','lease':'lease-a','message':{'id':42}}
    return FileTools(session,delivery,threading.Event(),client=client),session,delivery


def request(tools,name='note',tool='send',arguments=None):
    path=tools.root/'requests'/(name+'.json')
    write_json(path,{'tool':tool,'arguments':arguments if arguments is not None else {'body':'@owner note','nonce':'note-1'}})
    return path


def test_requests_keep_identity_current_cause_and_send_once(tmp_path,monkeypatch):
    monkeypatch.setenv('AGENTCHAT_CAUSE','999')
    calls=[]
    client=SimpleNamespace(call=lambda path,data=None:calls.append((path,data)) or {'id':43})
    tools,_,_=make_tools(tmp_path,client)
    path=request(tools)
    assert tools.drain() and tools.drain(final=True)
    assert len(calls)==1 and calls[0][0]=='/api/rooms/room/messages'
    assert calls[0][1]['cause']==42
    receipt=read_json(tools.root/'receipts'/path.name)
    assert receipt['ok'] and receipt['result']=={'id':43}
    assert 'test-scoped-secret' not in json.dumps(receipt)


@pytest.mark.parametrize('failure',[URLError('offline'),APIError(503,'temporarily unavailable')])
def test_network_retry_keeps_accepted_payload_and_nonce(tmp_path,failure):
    calls=[]
    def call(path,data=None):
        calls.append(data)
        if len(calls)==1:raise failure
        return {'id':43}
    tools,_,_=make_tools(tmp_path,SimpleNamespace(call=call))
    path=request(tools)
    assert not tools.drain() and not (tools.root/'receipts'/path.name).exists()
    # Rewriting a pending file cannot silently turn a retry into another action.
    request(tools,arguments={'body':'@someone different','nonce':'new-action'})
    assert tools.drain()
    assert calls[0]==calls[1] and calls[1]['nonce']=='note-1'


def test_another_attempt_never_replays_old_files_and_cancel_stops_calls(tmp_path):
    calls=[];client=SimpleNamespace(call=lambda *args:calls.append(args))
    old,session,delivery=make_tools(tmp_path,client);request(old)
    new=FileTools(session,{**delivery,'lease':'lease-b'},threading.Event(),client=client)
    assert new.root!=old.root and new.drain() and calls==[]
    old.cancel.set()
    assert not old.drain() and calls==[]
    new.close();request(new)
    assert not new.drain() and calls==[]


def test_partial_file_is_read_after_write_completes_and_bad_final_gets_receipt(tmp_path):
    calls=[];tools,_,_=make_tools(tmp_path,SimpleNamespace(call=lambda *args:calls.append(args)))
    path=tools.root/'requests'/'partial.json';path.write_text('{',encoding='utf-8')
    assert not tools.drain() and not (tools.root/'receipts'/path.name).exists()
    request(tools,'partial','members',{})
    assert tools.drain() and len(calls)==1
    path=tools.root/'requests'/'broken.json';path.write_text('{',encoding='utf-8')
    assert tools.drain(final=True)
    assert not read_json(tools.root/'receipts'/path.name)['ok'] and len(calls)==1


def test_manager_tool_cannot_be_used_by_worker_and_requires_current_owner_task(tmp_path,monkeypatch):
    monkeypatch.delenv('AGENTCHAT_CAUSE',raising=False)
    calls=[];client=SimpleNamespace(call=lambda path,data=None:calls.append((path,data)) or {'pending':True})
    worker,_,_=make_tools(tmp_path/'worker',client)
    request(worker,tool='sessions',arguments={'action':'list'})
    assert worker.drain() and calls==[]
    assert not read_json(worker.root/'receipts'/'note.json')['ok']
    manager,_,_=make_tools(tmp_path/'manager',client,session_type='manager')
    request(manager,tool='sessions',arguments={'action':'leave','members':['reviewer'],'nonce':'exit-1'})
    assert manager.drain()
    assert calls==[('/api/manager/sessions',{'action':'leave','cause':42,'nonce':'exit-1',
                                         'members':['reviewer'],'roles':[],'immediate':False,'expected':None})]


def test_bad_shape_size_and_api_error_are_scoped_error_receipts(tmp_path):
    def denied(*args):raise APIError(403,'test-scoped-secret forbidden')
    tools,_,_=make_tools(tmp_path,SimpleNamespace(call=denied))
    write_json(tools.root/'requests'/'bad.json',{'tool':'send','arguments':[]})
    (tools.root/'requests'/'big.json').write_text(' '* (1024*1024+1),encoding='utf-8')
    request(tools,'denied')
    assert tools.drain(final=True)
    for name in ('bad','big','denied'):
        result=read_json(tools.root/'receipts'/(name+'.json'))
        assert not result['ok'] and 'test-scoped-secret' not in result['error']


def test_gateway_flushes_last_file_request_before_completing_delivery(tmp_path):
    calls=[];client=SimpleNamespace(call=lambda path,data=None:calls.append(path) or {'id':43})
    tools,session,delivery=make_tools(tmp_path,client)
    job={'cancel':threading.Event()}
    bridge=Bridge.__new__(Bridge);bridge.client=client;bridge.stop=threading.Event()
    bridge.lock=threading.RLock();bridge.running={session['id']:job};bridge.redact=lambda s:s
    def execute(*args):
        request(tools);job['file_tools']=tools
        return {'lease':delivery['lease'],'body':'@owner finished'}
    bridge.execute=execute
    bridge.worker(session,delivery,job)
    assert calls==['/api/rooms/room/messages','/api/bridge/deliveries/delivery/finish']
    assert tools.stop.is_set() and bridge.running=={}
