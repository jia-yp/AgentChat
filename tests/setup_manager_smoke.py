"""Explicit isolated Codex setup -> same native manager conversation check."""
import io
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import uuid
import zipfile

import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from bridge.adapters import build_command,parse_result
from bridge.common import read_json,write_json
from bridge.harness_environment import harness_environment

FOLDER=ROOT/'.runtime/setup-manager-qa'


def wait(fn,seconds=180):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        value=fn()
        if value:return value
        time.sleep(.5)
    raise RuntimeError('Setup manager smoke timed out')


def main():
    FOLDER.mkdir(exist_ok=True);run=FOLDER/time.strftime('%Y%m%dT%H%M%S');run.mkdir()
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env={**os.environ,'AGENTCHAT_DATA':str(run/'data'),'AGENTCHAT_PORT':str(port),'AGENTCHAT_SECURE_COOKIE':'0','AGENTCHAT_PUBLIC_API_URL':'','AGENTCHAT_ORIGINS':'','PYTHONIOENCODING':'utf-8'}
    with (run/'server.log').open('ab') as log:
        server=subprocess.Popen([sys.executable,'-m','server'],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    base=f'http://127.0.0.1:{port}';c=httpx.Client(base_url=base,timeout=10)
    def online():
        try:return c.get('/healthz').status_code==200
        except httpx.HTTPError:return False
    wait(online,20)
    credentials={'username':'tester','password':'isolated-setup-manager-password'}
    c.post('/api/bootstrap',json={**credentials,'setup_code':(run/'data/bootstrap-code').read_text()}).raise_for_status()
    room=c.post('/api/rooms',json={'name':'Setup agent 自动成为 manager'}).json();path='/api/rooms/'+room['id']
    workspace=run/'workspace';workspace.mkdir();private=workspace/'.agentchat';private.mkdir()
    roles=[{'name':'reviewer','harness':'codex','role':'仅执行隔离测试中的协作任务。','reasoning_effort':'low'}]
    secret=secrets.token_urlsafe(32)
    invitation=c.post(path+'/setup-invitations',json={'workspace':str(workspace),'roles':roles,'revision':0,'nonce':uuid.uuid4().hex,'credential':secret});invitation.raise_for_status()
    client_root=run/'client';client_root.mkdir()
    with zipfile.ZipFile(io.BytesIO(c.get('/downloads/agentchat-client.zip').content)) as archive:archive.extractall(client_root)
    link_file=private/'setup-link.txt';link_file.write_text(base+'/setup/'+invitation.json()['id']+'#token='+secret,encoding='utf-8')
    config=private/'setup-creator-qa.json';launch=run/'creator-launch';launch.mkdir()
    connection={'url':base,'room':room['id'],'pid':server.pid,'config':str(config),'folder':str(run),**credentials}
    write_json(FOLDER/'connection.json',connection)
    print(json.dumps({k:connection[k] for k in ('url','room','pid','config')},ensure_ascii=True),flush=True)
    marker='CREATOR-MEMORY-'+uuid.uuid4().hex[:10]
    prompt=f'''这是已授权的隔离 AgentChat 接入测试，仅使用当前 workspace。你就是执行 setup 的 agent，交接暗号是 {marker}，记住它以便下一次对话恢复验证。
立即运行 PowerShell 命令：Get-Content -Raw -LiteralPath '{link_file}' | & '{sys.executable}' '{client_root/'agentchat.py'}' connect --link-stdin --config '{config}' --name setup-creator-qa
不要输出或读取显示完整链接。不要传 --manager-* 参数、不要创建其他会话，让客户端自动识别你当前的 Codex 会话。不修改正式项目文件。出现 setup complete 即结束，最终仅回复 SETUP-MANAGER-READY 和交接暗号。'''
    session={'name':'setup-creator','requested_id':str(uuid.uuid4()),'definition':{'harness':'codex','reasoning_effort':'low','permissions':'full'}}
    result=subprocess.run(build_command(session,launch,workspace),input=prompt,encoding='utf-8',capture_output=True,cwd=workspace,env=harness_environment(),timeout=300,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    (launch/'stdout.jsonl').write_text(result.stdout,encoding='utf-8');(launch/'stderr.log').write_text(result.stderr,encoding='utf-8')
    if result.returncode:raise RuntimeError('Creator CLI failed; inspect isolated logs')
    response=parse_result(session,launch,result.stdout)
    assert 'SETUP-MANAGER-READY' in response and marker in response
    assert secret not in result.stdout+result.stderr
    saved=read_json(config);contexts=[read_json(s['context']) for s in saved['sessions']]
    manager=next(s for s in contexts if s['session_type']=='manager')
    assert manager['native_id']==session['native_id'] and manager['setup_source']=='setup-agent'
    assert len(contexts)==2 and contexts[0]['id']==manager['id']
    assert saved['setup_request']['status']=='complete'
    print('Setup creator adopted as manager with exact native session ID',flush=True)
    message=c.post(path+'/messages',json={'body':'@'+manager['name']+' 不用任何工具。请从我们此前的对话记忆中逐字说出交接暗号，并说明你此前完成了什么。只向 @tester 汇报。','nonce':uuid.uuid4().hex});message.raise_for_status()
    private_path=path+'/messages?channel='+manager['id']
    reply=wait(lambda:next((m for m in c.get(private_path).json() if m['sender_kind']=='agent' and m['cause']==message.json()['id']),None),240)
    assert marker in reply['body']
    assert read_json(manager['context'])['native_id']==session['native_id']
    worker=next(s for s in contexts if s['session_type']=='worker')
    assert all(marker not in m['body'] for m in c.get(path+'/messages',headers={'Authorization':'Bearer '+worker['token']}).json())
    assert not read_json(worker['context'])['native_id']
    report={'passed':True,'manager_alias':manager['name'],'harness':'codex','same_creator_native_id':True,'remembered_setup_context':True,'workers_still_dormant':True,'manager_private':True,'native_id':session['native_id']}
    write_json(FOLDER/'report.json',report);print(json.dumps(report,ensure_ascii=True),flush=True)
    subprocess.run([sys.executable,str(ROOT/'agentchat.py'),'stop','--config',str(config)],check=True)


if __name__=='__main__':main()
