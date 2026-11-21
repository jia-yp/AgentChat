import threading
from types import SimpleNamespace

import pytest

from bridge import codex_live
from bridge.harness_errors import HarnessFailure


@pytest.mark.parametrize('outcome',['accepted','rejected','unknown','already_done','rate_limit'])
@pytest.mark.parametrize('permissions',['inherit','network','workspace','full','read-only'])
def test_exact_turn_transport_without_process_restart(tmp_path,monkeypatch,outcome,permissions):
    calls=[];acks=[];instances=[];clock=[100]
    def monotonic():clock[0]+=.25;return clock[0]
    monkeypatch.setattr(codex_live.time,'monotonic',monotonic)
    class Rpc:
        def __init__(self,*args):
            instances.append(self);self.process=object();self.backlog=[];self.events=[];self.counter=0;self.closed=False
        def send(self,body):calls.append(('notification',body))
        def request(self,method,params):
            self.counter+=1;calls.append((method,params))
            if method=='turn/steer':
                if outcome=='accepted':self.events.append({'id':self.counter,'result':{'turnId':'active-turn'}})
                elif outcome=='rejected':self.events.append({'id':self.counter,'error':{'message':'active turn changed'}})
                self.done()
            return self.counter
        def done(self):
            self.events.extend([{'method':'item/completed','params':{'turnId':'active-turn','item':{'type':'agentMessage','phase':'final_answer','text':'@owner final'}}},
                {'method':'turn/completed','params':{'turn':{'id':'active-turn','items':[],'status':'failed' if outcome=='rate_limit' else 'completed',
                    'error':{'message':'rate_limit_error: 429'} if outcome=='rate_limit' else None}}}])
        def wait(self,request,cancelled):
            method=calls[-1][0]
            if method=='initialize':return {}
            if method=='thread/resume':return {'thread':{'id':'original-native'},'model':'chosen-model','reasoningEffort':'high'}
            if method=='turn/start':
                if outcome in ('already_done','rate_limit'):self.done()
                return {'turn':{'id':'active-turn'}}
        def receive(self,timeout=.2):return self.events.pop(0) if self.events else None
        def close(self):self.closed=True
    monkeypatch.setattr(codex_live,'Rpc',Rpc)
    class Client:
        claimed=False
        def call(self,path,data,timeout=20):
            if path.endswith('/ack'):acks.append(data);return {'ok':True}
            if self.claimed:return {'notes':[]}
            self.claimed=True
            return {'notes':[{'id':'note','message':2,'sender':'claude','sender_kind':'agent','body':'brief note'}]}
    bridge=SimpleNamespace(workspace=tmp_path,stop=threading.Event(),client=Client(),redact=lambda s:s)
    session={'id':'member','name':'coder','native_id':'original-native','context':str(tmp_path/'context.json'),
        'definition':{'harness':'codex','executable':'unused','model':'chosen-model','reasoning_effort':'high','sandbox':'workspace-write','permissions':permissions,'timeout_seconds':240},'runtime':{}}
    delivery={'id':'task','lease':'lease','round_budget':{'last':False,'remaining':3,'max':8}}
    from bridge.context_window import ContextWindow
    from bridge.common import read_json
    plan=ContextWindow(session,'room',delivery)
    plan.add([{'id':1,'body':'original prompt','sender_name':'owner'}])
    plan.render('test rules',tasks=[1])
    job={'cancel':threading.Event(),'context_plan':plan}
    args=(bridge,session,delivery,job,tmp_path,'original prompt',{},tmp_path/'out',tmp_path/'err')
    if outcome=='rate_limit':
        with pytest.raises(HarnessFailure) as error:codex_live.execute(*args)
        assert error.value.kind=='rate_limit'
    else:assert codex_live.execute(*args)=='@owner final'
    assert len(instances)==1 and instances[0].closed and session['native_id']=='original-native'
    resume=next(p for m,p in calls if m=='thread/resume')
    assert resume['threadId']=='original-native' and resume['approvalPolicy']=='never'
    assert resume['sandbox']=={'full':'danger-full-access','read-only':'read-only'}.get(permissions,'workspace-write')
    if permissions in ('network','workspace','read-only'):assert resume['config']['sandbox_workspace_write.network_access']==(permissions=='network')
    assert resume['model']=='chosen-model' and resume['config']['model_reasoning_effort']=='high'
    assert sum(m=='turn/start' for m,p in calls)==1
    assert read_json(plan.path)['seen']==([1,2] if outcome=='accepted' else [1])
    steers=[p for m,p in calls if m=='turn/steer']
    if outcome in ('already_done','rate_limit'):assert not steers and not acks
    else:
        assert len(steers)==1 and steers[0]['expectedTurnId']=='active-turn' and steers[0]['threadId']=='original-native'
        assert acks[0]['status']=={'accepted':'injected','rejected':'rejected','unknown':'unknown'}[outcome]
