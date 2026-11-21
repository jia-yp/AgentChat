import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from bridge import execution, codex_live, runner
from bridge.execution import ExecutionGuard,ExecutionStopped,cancel_job


def test_codex_slow_native_resume_has_no_hidden_startup_deadline(monkeypatch):
    clock=[0];monkeypatch.setattr(codex_live.time,'monotonic',lambda:clock[0])
    rpc=codex_live.Rpc.__new__(codex_live.Rpc);rpc.backlog=[]
    events=iter([{'method':'thread/status/changed'}]*3+[{'id':7,'result':{'thread':{'id':'same-native'}}}])
    def receive():
        clock[0]+=300
        return next(events)
    rpc.receive=receive
    assert rpc.wait(7,lambda:False)['thread']['id']=='same-native'
    assert clock[0]>40


@pytest.mark.parametrize('setting',[{}, {'timeout_seconds':None}, {'timeout_seconds':0}])
def test_ordinary_harness_can_run_silently_for_many_hours(tmp_path,monkeypatch,setting):
    clock=[100];monkeypatch.setattr(execution.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(runner.time,'sleep',lambda _:None)
    class Process:
        returncode=0
        def __init__(self,*args,**kw):self.stdin=io.BytesIO();self.output=kw['stdout'];self.polls=0
        def poll(self):
            clock[0]+=7200;self.polls+=1
            if self.polls<4:return None
            self.output.write(b'@owner finished after hours');return 0
    monkeypatch.setattr(runner.subprocess,'Popen',Process)
    bridge=runner.Bridge.__new__(runner.Bridge);bridge.workspace=tmp_path;bridge.stop=threading.Event();bridge.steer_capabilities={}
    bridge.prompt=lambda *args:'a long project task';bridge.complete_response=lambda s,d,r:r
    session={'id':'worker','name':'worker','context':str(tmp_path/'session.json'),'temp_dir':str(tmp_path),
             'definition':{'harness':'command','command':['fixture'],**setting}}
    job={'cancel':threading.Event()}
    assert bridge.execute(session,{'id':'task','message':{'id':1}},job,tmp_path)=='@owner finished after hours'
    assert job['timeout_seconds']==0 and clock[0]>1800


@pytest.mark.parametrize('setting,finish_after,timeout',[
    ({},3,False),({'timeout_seconds':None},3,False),({'timeout_seconds':0},3,False),
    ({'timeout_seconds':10},3,True),({'timeout_seconds':10},1,False)])
def test_codex_long_tasks_and_completed_result_deadline_race(tmp_path,monkeypatch,setting,finish_after,timeout):
    clock=[100];monkeypatch.setattr(execution.time,'monotonic',lambda:clock[0]);instances=[]
    class Rpc:
        def __init__(self,*args):self.process=object();self.backlog=[];self.calls=[];self.reads=0;self.closed=False;instances.append(self)
        def send(self,body):pass
        def request(self,method,params):self.calls.append((method,params));return len(self.calls)
        def wait(self,request,cancelled):
            method=self.calls[request-1][0]
            if method=='initialize':return {}
            if method=='thread/resume':return {'thread':{'id':'original'},'model':'fixture'}
            if method=='turn/start':return {'turn':{'id':'turn'}}
        def receive(self,timeout=.2):
            if not timeout:return None
            self.reads+=1;clock[0]+=7200
            if self.reads==finish_after:return {'method':'turn/completed','params':{'turn':{'id':'turn','status':'completed','items':[{'type':'agentMessage','phase':'final_answer','text':'@owner done'}]}}}
            return {'method':'item/started','params':{'item':{'type':'contextCompaction'}}}
        def close(self):self.closed=True
    monkeypatch.setattr(codex_live,'Rpc',Rpc)
    bridge=SimpleNamespace(workspace=tmp_path,stop=threading.Event(),client=SimpleNamespace(call=lambda *a,**k:{'notes':[]}),redact=lambda s:s)
    session={'id':'worker','name':'worker','native_id':'original','runtime':{},'context':str(tmp_path/'session.json'),
        'definition':{'harness':'codex','executable':'fixture',**setting}}
    args=(bridge,session,{'id':'task','lease':'lease'},{'cancel':threading.Event()},tmp_path,'prompt',{},tmp_path/'out',tmp_path/'err')
    if timeout:
        with pytest.raises(ExecutionStopped) as error:codex_live.execute(*args)
        assert error.value.kind=='execution_timeout' and '10 秒' in str(error.value)
    else:assert codex_live.execute(*args)=='@owner done'
    assert instances[0].closed and session['native_id']=='original'


def test_stop_reasons_and_server_lease_deadline_are_independent_of_task_duration(monkeypatch):
    clock=[100];monkeypatch.setattr(execution.time,'monotonic',lambda:clock[0])
    bridge=runner.Bridge.__new__(runner.Bridge);bridge.stop=threading.Event();bridge.lock=threading.RLock()
    job={'cancel':threading.Event(),'lease_deadline':190};bridge.running={'worker':job}
    guard=ExecutionGuard(bridge,job,{})
    clock[0]=175;bridge.check_leases();guard.check();assert not job['cancel'].is_set()
    clock[0]=190;bridge.check_leases()
    with pytest.raises(ExecutionStopped) as error:guard.check()
    assert error.value.kind=='lease_expired' and '续期' in str(error.value)
    cancel_job(job,'bridge_stopped','later shutdown')
    assert job['cancel_kind']=='lease_expired' # Preserve the original cause.
    job={'cancel':threading.Event()};guard=ExecutionGuard(bridge,job,{})
    bridge.stop.set()
    with pytest.raises(ExecutionStopped) as error:guard.check()
    assert error.value.kind=='bridge_stopped'
    bridge.stop.clear();cancel_job(job,'task_cancelled','管理员停止了本轮协作')
    with pytest.raises(ExecutionStopped) as error:guard.check()
    assert error.value.kind=='task_cancelled' and '管理员' in str(error.value)


def test_failure_keeps_a_durable_diagnostic_and_exact_reason(tmp_path):
    bridge=runner.Bridge.__new__(runner.Bridge);bridge.lock=threading.RLock();bridge.redact=lambda s:s
    sent=[];bridge.client=SimpleNamespace(call=lambda path,payload:sent.append(payload))
    def fail(*args):raise ExecutionStopped('execution_timeout','explicit 20 second limit')
    bridge.execute=fail
    session={'id':'worker','name':'worker','native_id':'original','context':str(tmp_path/'session.json')}
    job={'cancel':threading.Event(),'started_at':123,'timeout_seconds':20};bridge.running={'worker':job}
    bridge.worker(session,{'id':'task','lease':'lease','message':{'id':12}},job)
    record=json.loads(next((tmp_path/'logs').glob('*.failure.json')).read_text())
    assert record['kind']=='execution_timeout' and record['timeout_seconds']==20 and record['native_id']=='original'
    assert sent[0]['error_kind']=='execution_timeout' and sent[0]['error']==record['reason']
    assert not bridge.running


def test_heartbeat_renews_the_granted_lease_and_preserves_server_cancellation(tmp_path,monkeypatch):
    clock=[100];monkeypatch.setattr(execution.time,'monotonic',lambda:clock[0])
    bridge=runner.Bridge.__new__(runner.Bridge);bridge.lock=threading.RLock();bridge.sessions={}
    bridge.path=tmp_path/'config.json';bridge.config={'room':{'name':'fixture'}}
    bridge.workspace=tmp_path
    job={'cancel':threading.Event(),'delivery':{'id':'task','lease':'lease'},'lease_deadline':120};bridge.running={'worker':job}
    response={'valid_leases':['task'],'lease_seconds':120}
    bridge.client=SimpleNamespace(call=lambda *a,**k:response)
    bridge.heartbeat();assert job['lease_deadline']==220
    clock[0]=200;bridge.check_leases();assert not job['cancel'].is_set()
    response={'valid_leases':[],'invalid_leases':[{'id':'task','kind':'task_cancelled','reason':'管理员关闭了 session'}]}
    bridge.heartbeat()
    assert job['cancel'].is_set() and job['cancel_kind']=='task_cancelled' and '管理员关闭' in job['cancel_reason']
