"""Manual integration check using installed/authenticated Codex and Claude.

Run explicitly; creates only an isolated local server/workspace under .runtime.
Keeps the server available for browser QA; `cleanup` stops its bridge.
The printed server PID can be stopped after browser verification.
"""
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

folder=ROOT/'.runtime/steer-qa';folder.mkdir(exist_ok=True)


def wait_until(check,seconds=150):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        result=check()
        if result:return result
        time.sleep(.5)
    raise RuntimeError('Timed out waiting for isolated Steer check')


def main():
    if len(sys.argv)>1 and sys.argv[1]=='cleanup':
        c=read_json(folder/'connection.json')
        subprocess.run([sys.executable,str(ROOT/'agentchat.py'),'stop','--config',c['config']],check=True)
        return
    stamp=time.strftime('%Y%m%dT%H%M%S');runfolder=folder/stamp;runfolder.mkdir()
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env={**os.environ,'AGENTCHAT_DATA':str(runfolder/'data'),'AGENTCHAT_PORT':str(port),'AGENTCHAT_SECURE_COOKIE':'0','AGENTCHAT_PUBLIC_API_URL':'','AGENTCHAT_ORIGINS':'','PYTHONIOENCODING':'utf-8'}
    with (runfolder/'server.log').open('ab') as log:
        server=subprocess.Popen([sys.executable,'-m','server'],cwd=ROOT,env=env,stdout=log,stderr=log,
            **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}))
    base=f'http://127.0.0.1:{port}';c=httpx.Client(base_url=base,timeout=5)
    def online():
        try:return c.get('/healthz').status_code==200
        except httpx.HTTPError:return False
    wait_until(online,15)
    password='isolated-steer-qa-password'
    c.post('/api/bootstrap',json={'username':'tester','password':password,'setup_code':(runfolder/'data/bootstrap-code').read_text()}).raise_for_status()
    room=c.post('/api/rooms',json={'name':'Steer 运行中注入验证'}).json();workspace=runfolder/'workspace'
    roles=[{'name':'coder','harness':'codex','role':'仅执行这个隔离项目的验证；向 @tester 汇报。','reasoning_effort':'low','timeout_seconds':240},
           {'name':'reviewer','harness':'claude','role':'仅按管理员指令发送当前任务的补充信息，向 @tester 汇报。','reasoning_effort':'low','timeout_seconds':180}]
    rolefile=runfolder/'roles.json';write_json(rolefile,roles)
    config=workspace/'.agentchat/setup-steer-qa.json'
    result=subprocess.run([sys.executable,str(ROOT/'agentchat.py'),'setup','--server',base,'--workspace',str(workspace),'--roles',str(rolefile),'--name','setup-steer-qa','--start'],cwd=ROOT,env={**env,'AGENTCHAT_JOIN_TOKEN':room['join_token']},capture_output=True,timeout=55)
    if result.returncode:raise RuntimeError(result.stderr.decode('utf-8',errors='replace'))
    saved=read_json(config);sessions=[read_json(s['context']) for s in saved['sessions']]
    connection={'url':base,'room':room['id'],'pid':server.pid,'username':'tester','password':password,'config':str(config),'folder':str(runfolder)}
    write_json(folder/'connection.json',connection)
    print(json.dumps({k:v for k,v in connection.items() if k not in ('password',)},ensure_ascii=True),flush=True)
    path='/api/rooms/'+room['id'];coder=next(s for s in sessions if s['name']=='coder')
    def send(body,**kw):
        r=c.post(path+'/messages',json={'body':body,'nonce':str(uuid.uuid4()),**kw});r.raise_for_status();return r.json()
    first=send('@coder 这是隔离集成验证。先用本地 PowerShell 工具执行一次 Start-Sleep -Seconds 45（不要提前结束或改成回复），这段等待期间会有同伴发送 Steer 补充。等待结束后，请把本轮收到的补充中的验收标记原样写在最终回复中；若没有收到补充，请如实说明。不要修改任何文件，也不要启动额外 session。',max_rounds=8)
    logs=Path(coder['context']).parent/'logs'
    def started_tool():
        for p in logs.glob('*.stdout.log'):
            for line in p.read_text(encoding='utf-8',errors='replace').splitlines():
                try:event=json.loads(line)
                except ValueError:continue
                if event.get('method')=='item/started' and event.get('params',{}).get('item',{}).get('type')=='commandExecution':return True
        return False
    wait_until(started_tool,90);print('Codex tool running; dispatching Claude note task',flush=True)
    marker='STEER-'+uuid.uuid4().hex[:12]
    second=send('@reviewer 你发现了一条关于 coder 当前工作的补充事实：本次验收标记应为 '+marker+'。请现在直接告诉正在工作的 @coder，让它在当前任务完成前纳入这个信息，无需额外工作。请自行选择适合这种实时纠偏的群聊投递方式，不要等它做完，也不需要让我操作网页。不要读取/输出 token；发出后向 @tester 简短汇报真实投递状态，不必反复查询。')
    def history():return c.get(path+'/messages?before=9007199254740991&limit=100').json()
    injected=wait_until(lambda:next((m for m in history() if m.get('delivery_mode')=='steer' and any(s['status']=='injected' for s in m['steers'])),None),120)
    assert injected['sender_name']=='reviewer' and marker in injected['body']
    print('Claude note accepted by exact active Codex turn',flush=True)
    response=wait_until(lambda:next((m for m in history() if m['sender_name']=='coder' and m['cause']==first['id']),None),120)
    assert marker in response['body'],response['body']
    native=read_json(coder['context'])['native_id'];assert native
    resumed=send('@coder 继续刚才的同一 session。仅从你已收到的上下文回忆那条 Steer 的验收标记，并向 @tester 回复。不要调用工具。',max_rounds=8)
    reply=wait_until(lambda:next((m for m in history() if m['sender_name']=='coder' and m['cause']==resumed['id']),None),120)
    assert marker in reply['body'] and read_json(coder['context'])['native_id']==native
    record={'passed':True,'marker':marker,'native_id':native,'same_native_id_after_resume':True,'note_message':injected['id'],'first_response':response['id'],'resumed_response':reply['id'],'claude_sender':True,'workspace':str(workspace)}
    write_json(folder/'report.json',record);print(json.dumps(record,ensure_ascii=True),flush=True)


if __name__=='__main__':main()
