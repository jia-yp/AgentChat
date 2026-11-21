"""Explicit NAS integration test using a disposable container and downloaded client.

Run: python tests/nas_smoke.py --ssh user@nas-host
Never initializes or changes the production database.
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path

import httpx

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser();parser.add_argument('--ssh',required=True)
args=parser.parse_args()
name='agentchat-smoke-'+uuid.uuid4().hex[:8]
runtime=ROOT/'.runtime'/name
runtime.mkdir(parents=True)
tunnel=bridge=None
container_id=None
def ssh(command):
    return subprocess.check_output(['ssh','-o','BatchMode=yes',args.ssh,command],text=True,encoding='utf-8').strip()
def wait(fn,seconds=20):
    until=time.monotonic()+seconds
    while time.monotonic()<until:
        try:
            result=fn()
            if result:return result
        except (OSError,httpx.HTTPError):pass
        time.sleep(.3)
    raise RuntimeError('NAS smoke timed out')
try:
    container_id=ssh(f'docker run -d --rm --name {name} --user 1000:1000 --cap-drop ALL -e AGENTCHAT_DATA=/tmp/agentchat -p 127.0.0.1:18781:8781 agentchat-api')
    if not re.fullmatch('[0-9a-f]{64}',container_id):raise RuntimeError('Unexpected container ID')
    options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
    tunnel=subprocess.Popen(['ssh','-o','BatchMode=yes','-o','ExitOnForwardFailure=yes','-N','-L','127.0.0.1:18789:127.0.0.1:18781',args.ssh],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**options)
    c=httpx.Client(base_url='http://127.0.0.1:18789',timeout=5)
    wait(lambda:c.get('/healthz').status_code==200)
    code=ssh(f'docker exec {container_id} cat /tmp/agentchat/bootstrap-code')
    c.post('/api/bootstrap',json={'username':'tester','password':'nas-smoke-only-password','setup_code':code}).raise_for_status()
    room=c.post('/api/rooms',json={'name':'Disposable NAS smoke'}).json()
    package=c.get('/downloads/agentchat-client.zip');package.raise_for_status()
    client_root=runtime/'client';client_root.mkdir()
    with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
        for entry in archive.infolist():
            if not (client_root/entry.filename).resolve().is_relative_to(client_root.resolve()):raise RuntimeError('Unsafe zip path')
        archive.extractall(client_root)
    workspace=runtime/'workspace';workspace.mkdir()
    roles=[{'name':'worker','harness':'command','role':'Temporary NAS delivery fixture','command':[sys.executable,str(ROOT/'tests/fake_harness.py')]}]
    (runtime/'roles.json').write_text(json.dumps(roles),encoding='utf-8')
    env={**os.environ,'AGENTCHAT_JOIN_TOKEN':room['join_token'],'PYTHONIOENCODING':'utf-8'}
    setup=subprocess.run([sys.executable,str(client_root/'agentchat.py'),'setup','--server',str(c.base_url).rstrip('/'),'--workspace',str(workspace),'--roles',str(runtime/'roles.json'),'--name','setup-smoke'],env=env,capture_output=True,timeout=20)
    if setup.returncode:raise RuntimeError(setup.stderr.decode(errors='replace'))
    env.pop('AGENTCHAT_JOIN_TOKEN')
    config=workspace/'.agentchat/setup-smoke.json'
    bridge=subprocess.Popen([sys.executable,str(client_root/'agentchat.py'),'run','--config',str(config)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**options)
    wait(lambda:config.with_suffix('.status.json').exists())
    c.post(f'/api/rooms/{room["id"]}/messages',json={'body':'@worker Verify NAS outbound queue','nonce':'nas-smoke'}).raise_for_status()
    messages=wait(lambda:(items if len(items:=c.get(f'/api/rooms/{room["id"]}/messages').json())==2 else None))
    assert messages[0]['deliveries'][0]['status']=='done'
    assert messages[1]['sender_name']=='worker'
    report={'nas_downloaded_client':True,'outbound_bridge':True,'administrator_activation':True,'harness_subprocess':True,'response_persisted':True,'production_untouched':True}
    (runtime/'report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))
finally:
    if bridge:
        config.with_suffix('.stop').write_text('stop')
        try:bridge.wait(timeout=10)
        except subprocess.TimeoutExpired:bridge.kill()
    if tunnel:
        tunnel.terminate()
        try:tunnel.wait(timeout=5)
        except subprocess.TimeoutExpired:tunnel.kill()
    if container_id and re.fullmatch('[0-9a-f]{64}',container_id):
        ssh(f'docker rm -f {container_id}')
