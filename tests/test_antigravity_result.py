import copy
import json

import pytest

from bridge.adapters import parse_result
from bridge.antigravity_result import INTERRUPTED,NOT_FOUND
from bridge.harness_errors import HarnessFailure


def completed_events():
    native='exact-conversation'
    def step(index,kind,state='DONE',**extra):
        return {'event':'step_update','step_update':{'conversation_id':native,'step_index':index,'step_type':kind,'state':state,**extra}}
    return [
        {'event':'init','conversation_id':native},
        step(144,'user_input'),step(145,'system_message'),
        step(146,'agent_response','ACTIVE',text_delta='@owner 已完成'),
        step(146,'agent_response',text_delta='\n计划已保存。'),
        {'event':'result','result':{'conversation_id':native,'status':'ERROR','error':INTERRUPTED,
                                  'finished_reason':'error','response':'@owner 已完成\n计划已保存。'}},
    ]


def parse(tmp_path,events,session=None):
    session=session or {'definition':{'harness':'antigravity'}}
    result=parse_result(session,tmp_path,'\n'.join(json.dumps(e,ensure_ascii=False) for e in events))
    return result,session


def test_completed_current_turn_recovers_only_matching_final_response(tmp_path):
    events=completed_events();text,session=parse(tmp_path,events)
    assert text==events[-1]['result']['response'] and session['native_id']=='exact-conversation'
    assert session['native_result_warning']
    events[-1]['result']={'status':'SUCCESS','response':'@owner next'}
    _,session=parse(tmp_path,events,session)
    assert 'native_result_warning' not in session


@pytest.mark.parametrize('failure',[
    'active_response','active_tool','failed_tool','missing_input','wrong_conversation',
    'stale_response','missing_delta','rate_limit','explicit_cancel','transport_error','no_steps',
])
def test_real_errors_and_incomplete_or_unrelated_responses_still_fail(tmp_path,failure):
    events=copy.deepcopy(completed_events())
    if failure=='active_response':events[-2]['step_update']['state']='ACTIVE'
    elif failure in ('active_tool','failed_tool'):
        events.insert(-1,{'event':'step_update','step_update':{'conversation_id':'exact-conversation','step_index':147,'step_type':'tool','state':'ACTIVE' if failure=='active_tool' else 'ERROR'}})
    elif failure=='missing_input':events.pop(1)
    elif failure=='wrong_conversation':events[-2]['step_update']['conversation_id']='someone-else'
    elif failure=='stale_response':events[-1]['result']['response']='@owner a previous turn'
    elif failure=='missing_delta':events[-2]['step_update'].pop('text_delta')
    elif failure=='rate_limit':events[-1]['result']['error']='RESOURCE_EXHAUSTED'
    elif failure=='explicit_cancel':events[-1]['result']['finished_reason']='interrupted'
    elif failure=='transport_error':events.insert(-1,{'event':'error','error':'connection lost'})
    elif failure=='no_steps':events=[events[0],events[-1]]
    with pytest.raises(HarnessFailure) as error:parse(tmp_path,events)
    assert error.value.kind==('rate_limit' if failure=='rate_limit' else 'harness_error')


def test_prior_turn_error_is_not_confused_with_completed_current_turn(tmp_path):
    events=completed_events()
    events.insert(1,{'event':'step_update','step_update':{'conversation_id':'exact-conversation','step_index':126,'step_type':'tool','state':'ERROR'}})
    assert parse(tmp_path,events)[0]==events[-1]['result']['response']


def test_stale_404_requires_a_completed_current_response_and_no_error_step(tmp_path):
    events=completed_events();events[-1]['result']['error']=NOT_FOUND
    assert parse(tmp_path,events)[0]==events[-1]['result']['response']
    events.insert(-2,{'event':'step_update','step_update':{'conversation_id':'exact-conversation','step_index':145,'step_type':'error_message','state':'DONE'}})
    with pytest.raises(HarnessFailure):parse(tmp_path,events)
    events=completed_events();events[-1]['result'].update(error=NOT_FOUND,response='')
    with pytest.raises(HarnessFailure):parse(tmp_path,events)


def test_recovered_reply_journal_does_not_invoke_harness_again(tmp_path):
    import threading
    from bridge.common import read_json,write_json
    from bridge.runner import Bridge
    context=tmp_path/'session.json'
    session={'id':'worker','name':'worker','context':str(context),'native_id':'exact-conversation','detail':INTERRUPTED}
    payload={'lease':'expired','body':'@owner recovered response','native_id':'exact-conversation'}
    write_json(tmp_path/'outbox/delivery.json',payload)
    bridge=Bridge.__new__(Bridge);bridge.lock=threading.RLock();bridge.running={'worker':{}};bridge.stop=threading.Event()
    replies=[]
    class Client:
        def call(self,path,data):replies.append((path,data.copy()));return {'ok':True}
    bridge.client=Client()
    def never_execute(*args):pytest.fail('A recovered final reply must not rerun file operations')
    bridge.execute=never_execute
    bridge.worker(session,{'id':'delivery','lease':'fresh'},{'cancel':threading.Event()})
    assert replies==[('/api/bridge/deliveries/delivery/finish',{**payload,'lease':'fresh'})]
    assert read_json(context)['detail']=='' and session['native_id']=='exact-conversation'


@pytest.mark.parametrize('exit_code,empty,success',[(3,False,True),(3,True,False),(1,False,False)])
def test_process_exit_three_only_recovers_verified_complete_answer(tmp_path,monkeypatch,exit_code,empty,success):
    import sys,threading
    from bridge.runner import Bridge
    events=completed_events();events[-1]['result']['error']=NOT_FOUND
    if empty:events[-1]['result']['response']=''
    output='\n'.join(json.dumps(e,ensure_ascii=False) for e in events)
    monkeypatch.setattr('bridge.runner.build_command',lambda *args:[sys.executable,'-c',
        'import sys;sys.stdin.read();print('+repr(output)+');sys.exit('+str(exit_code)+')'])
    bridge=Bridge.__new__(Bridge);bridge.workspace=tmp_path;bridge.stop=threading.Event()
    bridge.steer_capabilities={};bridge.prompt=lambda *args:'test'
    bridge.redact=lambda value:value
    s={'id':'worker','name':'worker','context':str(tmp_path/'session.json'),'temp_dir':str(tmp_path),
       'definition':{'harness':'antigravity'},'runtime':{}}
    call=lambda:bridge.execute(s,{'id':'delivery','lease':'lease','message':{'id':1,'sender_name':'owner'}},
                              {'cancel':threading.Event()},tmp_path)
    if success:assert call()['body']==events[-1]['result']['response']
    else:
        with pytest.raises(HarnessFailure):call()
