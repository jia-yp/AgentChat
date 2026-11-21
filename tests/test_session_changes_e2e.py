import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
import uuid

import httpx

from bridge.cli import setup
from bridge.common import read_json
from bridge.processes import background_options
from tests.test_bridge_e2e import wait_until


ROOT=Path(__file__).resolve().parents[1]


def test_existing_manager_adds_and_removes_without_setup_token_or_bridge_restart(tmp_path,monkeypatch):
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env={**os.environ,'AGENTCHAT_DATA':str(tmp_path/'server'),'AGENTCHAT_PORT':str(port),'PYTHONIOENCODING':'utf-8'}
    options=background_options()
    server=subprocess.Popen([sys.executable,'-m','server'],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**options)
    process=None
    try:
        c=httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=5);wait_until(lambda:c.get('/healthz').status_code==200)
        c.post('/api/bootstrap',json={'username':'tester','password':'manager-change-e2e-password','setup_code':(tmp_path/'server/bootstrap-code').read_text()}).raise_for_status()
        room=c.post('/api/rooms',json={'name':'Session change test'}).json();base='/api/rooms/'+room['id']
        config=tmp_path/'workspace/.agentchat/bridge.json'
        monkeypatch.setenv('AGENTCHAT_JOIN_TOKEN',room['join_token'])
        args=SimpleNamespace(workspace=str(tmp_path/'workspace'),server=str(c.base_url).rstrip('/'),name='setup-e2e',config=str(config),roles=None,allow_unavailable=False,start=False,token_stdin=False,project_name='Original project')
        setup(args,[{'name':'manager','harness':'command','role':'管理','session_type':'manager','command':[sys.executable,str(ROOT/'tests/session_change_harness.py')]}])
        monkeypatch.delenv('AGENTCHAT_JOIN_TOKEN')
        process=subprocess.Popen([sys.executable,str(ROOT/'agentchat.py'),'run','--config',str(config)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**options)
        def inventory():
            data=c.get(base+'/session-changes').json()
            return data if data['managers'] and data['managers'][0]['supported'] else None
        inv=wait_until(inventory);manager=inv['managers'][0]
        original_pid=wait_until(lambda:read_json(config.with_suffix('.status.json')))['pid']
        role={'name':'新增测试','harness':'command','role':'执行一次测试','command':[sys.executable,str(ROOT/'tests/fake_harness.py')],'timeout_seconds':0}
        def submit(additions,removals):
            inv=inventory();m=inv['managers'][0]
            response=c.post(base+'/session-changes',json={'manager':m['id'],'expected':m['revision'],'nonce':uuid.uuid4().hex,'additions':additions,'removals':removals});response.raise_for_status();return response.json()
        job=submit([role],[])
        def finished(jid):return next(x for x in inventory()['changes'] if x['id']==jid)['status']=='complete'
        wait_until(lambda:finished(job['id']),timeout=35)
        contexts={x['name']:read_json(x['context']) for x in read_json(config)['sessions']}
        worker=contexts['新增测试'];manager_context=contexts['manager'];manager_native=read_json(manager_context['context'])['native_id']
        assert worker['project_key']==manager_context['project_key'] and worker['project_name']=='Original project'
        assert not (Path(worker['temp_dir'])/'executions.txt').exists()
        c.post(base+'/messages',json={'body':'@新增测试 hello','nonce':'activate'}).raise_for_status()
        def replied():return any(m['sender']==worker['id'] for m in c.get(base+'/messages').json())
        wait_until(replied)
        native=read_json(worker['context'])['native_id'];assert native
        removal=submit([],[worker['id']]);wait_until(lambda:finished(removal['id']),timeout=35)
        assert (Path(worker['temp_dir'])/'executions.txt').read_text()=='1'
        assert read_json(worker['context'])['native_id']==native
        assert read_json(manager_context['context'])['native_id']==manager_native
        assert c.post(base+'/messages',json={'body':'@新增测试 removed','nonce':'no-trigger'}).status_code==400
        assert process.poll() is None and read_json(config.with_suffix('.status.json'))['pid']==original_pid
        # No fresh setup link or credential rotation was needed for either edit.
        assert c.get(base+'/setup-profile').json()['setup'] is None
    finally:
        if process:
            config.with_suffix('.stop').write_text('stop')
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:process.kill()
        server.terminate()
        try:server.wait(timeout=10)
        except subprocess.TimeoutExpired:server.kill()
