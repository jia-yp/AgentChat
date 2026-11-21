"""Explicit isolated native SSH/permission regression; no production room messages."""
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

FOLDER=ROOT/'.runtime/permissions-qa'
SSH_TARGET=os.environ.get('AGENTCHAT_SSH_TARGET','user@nas-host')


def wait(fn,seconds=180):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=fn()
        if value:return value
        time.sleep(.5)
    raise RuntimeError('Permission smoke timeout')


def main():
    FOLDER.mkdir(exist_ok=True)
    if len(sys.argv)>1 and sys.argv[1]=='cleanup':
        connection=read_json(FOLDER/'connection.json')
        subprocess.run([sys.executable,str(ROOT/'agentchat.py'),'stop','--config',connection['config']],check=True)
        return
    run=FOLDER/time.strftime('%Y%m%dT%H%M%S');run.mkdir()
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env={**os.environ,'AGENTCHAT_DATA':str(run/'data'),'AGENTCHAT_PORT':str(port),'AGENTCHAT_SECURE_COOKIE':'0','AGENTCHAT_PUBLIC_API_URL':'','AGENTCHAT_ORIGINS':'','PYTHONIOENCODING':'utf-8'}
    with (run/'server.log').open('ab') as log:
        server=subprocess.Popen([sys.executable,'-m','server'],cwd=ROOT,env=env,stdout=log,stderr=log,**({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}))
    base=f'http://127.0.0.1:{port}';c=httpx.Client(base_url=base,timeout=5)
    def online():
        try:return c.get('/healthz').status_code==200
        except httpx.HTTPError:return False
    wait(online,20)
    credentials={'username':'tester','password':'isolated-permissions-qa-password'}
    c.post('/api/bootstrap',json={**credentials,'setup_code':(run/'data/bootstrap-code').read_text()}).raise_for_status()
    room=c.post('/api/rooms',json={'name':'权限管理 · SSH 验证'}).json();path='/api/rooms/'+room['id']
    roles=[{'name':name,'harness':harness,'role':'仅做管理员要求的隔离验证，禁止修改项目文件或远程文件。','timeout_seconds':180,**({'reasoning_effort':'low'} if harness!='antigravity' else {})}
           for name,harness in [('coder','codex'),('reviewer','claude'),('designer','antigravity')]]
    write_json(run/'roles.json',roles);workspace=run/'workspace';config=workspace/'.agentchat/setup-permission-qa.json'
    setup=subprocess.run([sys.executable,str(ROOT/'agentchat.py'),'setup','--server',base,'--workspace',str(workspace),'--roles',str(run/'roles.json'),'--name','setup-permission-qa','--start'],cwd=ROOT,env={**env,'AGENTCHAT_JOIN_TOKEN':room['join_token']},capture_output=True,timeout=60)
    if setup.returncode:raise RuntimeError(setup.stderr.decode('utf-8',errors='replace'))
    connection={'url':base,'room':room['id'],'pid':server.pid,'config':str(config),'folder':str(run),**credentials}
    write_json(FOLDER/'connection.json',connection)
    print(json.dumps({k:v for k,v in connection.items() if k!='password'},ensure_ascii=True),flush=True)
    def members():return {m['name']:m for m in c.get(path).json()['members'] if m['kind']=='agent'}
    def history():return c.get(path+'/messages?before=9007199254740991&limit=100').json()
    def send(body):
        r=c.post(path+'/messages',json={'body':body,'nonce':str(uuid.uuid4()),'max_rounds':8});r.raise_for_status();return r.json()['id']
    def replies(cause,names):
        found={m['sender_name']:m for m in history() if m['cause']==cause}
        return found if all(n in found for n in names) else None
    names=list(members())
    first=send('@all 这是隔离验证。不要使用工具，仅向 @tester 回复 PERMISSION-BASELINE。')
    wait(lambda:replies(first,names),240)
    saved=read_json(config);contexts={s['name']:Path(s['context']) for item in saved['sessions'] for s in [read_json(item['context'])]}
    before={name:read_json(ctx)['native_id'] for name,ctx in contexts.items()}
    modes={'coder':'full','reviewer':'ssh','designer':'full'}
    for name,mode in modes.items():
        m=members()[name];r=c.post(path+'/members/'+m['id']+'/permissions',json={'mode':mode,'revision':m['permission_settings']['revision']});r.raise_for_status()
    wait(lambda:all(m['permission_settings']['applied_revision']==m['permission_settings']['revision'] for m in members().values()),30)
    marker='AGENTCHAT-SSH-'+uuid.uuid4().hex[:10]
    ssh=send('@all 请实际执行一次只读 SSH 连通性验证：ssh -o BatchMode=yes -o ConnectTimeout=8 '+SSH_TARGET+' "echo '+marker+'"。用原生 Bash 或 PowerShell 工具直接运行 ssh 命令。不要读取密钥内容，不修改 SSH 配置、远程文件或其他文件，不创建新 session。向 @tester 回复实际标准输出与退出码；失败就报告具体错误，不猜测成功。')
    result=wait(lambda:replies(ssh,names),240)
    after={name:read_json(ctx)['native_id'] for name,ctx in contexts.items()}
    assert before==after,(before,after)
    assert all(marker in reply['body'] for reply in result.values()),{k:v['body'] for k,v in result.items()}
    print('Native SSH replies received; original session IDs preserved',flush=True)
    # Revoke the new privilege, then resume once more. App-server logs retain
    # the actual native sandbox response for independent policy verification.
    for name,mode in {'coder':'workspace','reviewer':'edits','designer':'sandbox'}.items():
        m=members()[name];c.post(path+'/members/'+m['id']+'/permissions',json={'mode':mode,'revision':m['permission_settings']['revision']}).raise_for_status()
    wait(lambda:all(m['permission_settings']['applied_revision']==m['permission_settings']['revision'] for m in members().values()),30)
    last=send('@all 权限已收回。不要调用工具，仅根据本次注入的执行权限说明，向 @tester 报告当前权限。')
    wait(lambda:replies(last,names),240)
    assert before=={name:read_json(ctx)['native_id'] for name,ctx in contexts.items()}
    report={'passed':True,'marker':marker,'initial_message':first,'ssh_message':ssh,'restricted_message':last,'same_session_ids':True,'native_ids':before,'modes_after_restrict':{name:m['permission_settings']['applied_mode'] for name,m in members().items()}}
    write_json(FOLDER/'report.json',report);print(json.dumps(report,ensure_ascii=True),flush=True)


if __name__=='__main__':main()
