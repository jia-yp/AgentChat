"""Explicit native Codex/Claude/Antigravity round-barrier test in an isolated room."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from bridge.common import read_json,write_json
FOLDER=ROOT/'.runtime/synchronous-qa'


def wait(fn,seconds=240):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=fn()
        if value:return value
        time.sleep(.5)
    raise RuntimeError('Synchronous native smoke timed out')


def main():
    FOLDER.mkdir(exist_ok=True)
    if len(sys.argv)>1 and sys.argv[1]=='cleanup':
        connection=read_json(FOLDER/'connection.json')
        subprocess.run([sys.executable,str(ROOT/'agentchat.py'),'stop','--config',connection['config']],check=True);return
    run=FOLDER/time.strftime('%Y%m%dT%H%M%S');run.mkdir()
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env={**os.environ,'AGENTCHAT_DATA':str(run/'data'),'AGENTCHAT_PORT':str(port),'AGENTCHAT_SECURE_COOKIE':'0','AGENTCHAT_PUBLIC_API_URL':'','AGENTCHAT_ORIGINS':'','PYTHONIOENCODING':'utf-8'}
    with (run/'server.log').open('ab') as log:
        server=subprocess.Popen([sys.executable,'-m','server'],cwd=ROOT,env=env,stdout=log,stderr=log,**({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}))
    base=f'http://127.0.0.1:{port}';c=httpx.Client(base_url=base,timeout=10)
    def online():
        try:return c.get('/healthz').status_code==200
        except httpx.HTTPError:return False
    wait(online,20)
    credentials={'username':'tester','password':'isolated-synchronous-qa-password'}
    c.post('/api/bootstrap',json={**credentials,'setup_code':(run/'data/bootstrap-code').read_text()}).raise_for_status()
    room=c.post('/api/rooms',json={'name':'同步轮次 · 快慢协作验证'}).json();path='/api/rooms/'+room['id']
    roles=[{'name':name,'harness':harness,'role':'执行管理员指定的同步轮次验收，禁止修改项目文件。','timeout_seconds':240,
            **({'reasoning_effort':'low'} if harness!='antigravity' else {'permissions':'full'})}
           for name,harness in [('coder','codex'),('reviewer','claude'),('designer','antigravity')]]
    write_json(run/'roles.json',roles);workspace=run/'workspace';config=workspace/'.agentchat/setup-sync-qa.json'
    setup=subprocess.run([sys.executable,str(ROOT/'agentchat.py'),'setup','--server',base,'--workspace',str(workspace),'--roles',str(run/'roles.json'),'--name','setup-sync-qa','--start'],cwd=ROOT,env={**env,'AGENTCHAT_JOIN_TOKEN':room['join_token']},capture_output=True,timeout=60)
    if setup.returncode:raise RuntimeError(setup.stderr.decode('utf-8',errors='replace'))
    write_json(FOLDER/'connection.json',{'url':base,'room':room['id'],'pid':server.pid,'config':str(config),'folder':str(run),**credentials})
    print(json.dumps({'url':base,'room':room['id'],'pid':server.pid,'config':str(config)},ensure_ascii=True),flush=True)
    def snapshot():return c.get(path).json()
    agents={m['name']:m for m in snapshot()['members'] if m['kind']=='agent'}
    allowances={agents['reviewer']['id']:2}
    c.put(path+'/round-limit',json={'round_mode':'synchronous','max_rounds':2,'session_speaks':allowances}).raise_for_status()
    tag=uuid.uuid4().hex[:8];markers={name:'SYNC-'+name.upper()+'-'+tag for name in agents}
    body=('@all 这是两轮同步协作验收。第一轮 coder 和 reviewer 不调用任何工具，各自只用最终回复向 @all 发出自己的标记；'
          'designer 请先用本机命令工具运行一次 PowerShell Start-Sleep -Seconds 50，等工具正常结束后，再用最终回复向 @all 发出自己的标记。'
          '禁止修改任何文件、启动额外会话或发送 Steer。三个标记分别是：'+json.dumps(markers,ensure_ascii=False)+'。'
          '第二轮是最后一轮：不要调用工具，检查完整上一轮对话，向 @管理员 一起报告三个标记，必须逐字写出三个标记，不再点名其他 agent。')
    r=c.post(path+'/messages',json={'body':body,'nonce':str(uuid.uuid4()),'round_mode':'synchronous','max_rounds':2,'session_speaks':allowances});r.raise_for_status();root=r.json()['id']
    def waiting_for_slow():
        run=snapshot()['synchronous'];parts={p['name']:p for p in run['participants']}
        if run['round']!=1:raise AssertionError('Round advanced before observing slow-participant barrier')
        return run if parts['coder']['status']=='done' and parts['reviewer']['status']=='done' and parts['designer']['status']=='running' else None
    barrier=wait(waiting_for_slow,150)
    write_json(FOLDER/'barrier.json',barrier);print('Fast Codex and Claude completed; group remains in round 1 waiting for Antigravity',flush=True)
    round_two=wait(lambda:snapshot()['synchronous'] if snapshot()['synchronous']['round']==2 else None,180)
    contexts=[Path(s['context']) for s in read_json(config)['sessions']]
    first_ids={read_json(p)['name']:read_json(p)['native_id'] for p in contexts}
    completed=wait(lambda:snapshot()['synchronous'] if snapshot()['synchronous']['status']=='completed' else None,180)
    history=c.get(path+'/messages?before=9007199254740991&limit=200').json()
    finals=[m for m in history if m['sender_kind']=='agent' and m['sync_round']==2]
    assert len(finals)==3
    assert all(all(marker in m['body'] for marker in markers.values()) for m in finals),{m['sender_name']:m['body'] for m in finals}
    assert first_ids=={read_json(p)['name']:read_json(p)['native_id'] for p in contexts}
    assert all(p['used']==1 for p in completed['participants'])
    report={'passed':True,'root':root,'markers':markers,'slow_agent_barrier_observed':True,'rounds':completed['round'],'finals_include_all_previous_markers':True,'same_native_ids':True,'native_ids':first_ids,'quota_two_finished_early':True}
    write_json(FOLDER/'report.json',report);print(json.dumps(report,ensure_ascii=True),flush=True)


if __name__=='__main__':main()
