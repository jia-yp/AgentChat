import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from bridge.cli import setup
from bridge.common import read_json


ROOT=Path(__file__).resolve().parents[1]


def wait_until(fn,timeout=15):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            value=fn()
            if value:return value
        except (OSError,httpx.HTTPError,FileNotFoundError):pass
        time.sleep(.15)
    raise AssertionError('Timed out waiting for integration result')


@pytest.mark.parametrize('names',[('one','two'),('小龙','小克')])
def test_outbound_bridge_full_lifecycle(tmp_path,names):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env={**os.environ,'AGENTCHAT_DATA':str(tmp_path/'server'),'AGENTCHAT_PORT':str(port),'PYTHONIOENCODING':'utf-8'}
    server=subprocess.Popen([sys.executable,'-m','server'],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    bridge=None
    try:
        client=httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=3)
        wait_until(lambda:client.get('/healthz').status_code==200)
        code=(tmp_path/'server/bootstrap-code').read_text()
        client.post('/api/bootstrap',json={'username':'tester','password':'test-password-long','setup_code':code}).raise_for_status()
        room=client.post('/api/rooms',json={'name':'E2E'}).json()
        workspace=tmp_path/'workspace';workspace.mkdir()
        roles=[{'name':name,'harness':'command','role':'fixture','command':[sys.executable,str(ROOT/'tests/fake_harness.py')]} for name in names]
        roles_file=tmp_path/'roles.json';roles_file.write_text(json.dumps(roles))
        config=workspace/'.agentchat/setup-e2e.json'
        completed=subprocess.run([sys.executable,str(ROOT/'agentchat.py'),'setup','--server',str(client.base_url).rstrip('/'),'--workspace',str(workspace),'--roles',str(roles_file),'--name','setup-e2e'],env={**env,'AGENTCHAT_JOIN_TOKEN':room['join_token']},capture_output=True,timeout=15)
        assert completed.returncode==0,completed.stderr.decode(errors='replace')
        bridge=subprocess.Popen([sys.executable,str(ROOT/'agentchat.py'),'run','--config',str(config)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        wait_until(lambda:config.with_suffix('.status.json').exists())
        saved=read_json(config)
        contexts=[read_json(x['context']) for x in saved['sessions']]
        assert not any((Path(x['temp_dir'])/'executions.txt').exists() for x in contexts)
        message=client.post(f'/api/rooms/{room["id"]}/messages',json={'body':f'@{names[0]} hello; @{names[1]} is a reference','nonce':'first'}).json()
        def done():
            history=client.get(f'/api/rooms/{room["id"]}/messages').json()
            return history if len(history)==2 else None
        history=wait_until(done)
        assert history[1]['sender_name']==names[0] and history[0]['deliveries'][0]['status']=='done'
        assert not (Path(contexts[1]['temp_dir'])/'executions.txt').exists()
        client.post(f'/api/rooms/{room["id"]}/messages',json={'body':f'@{names[0]} hello; @{names[1]} is a reference','nonce':'first'}).raise_for_status()
        assert (Path(contexts[0]['temp_dir'])/'executions.txt').read_text()=='1'
        # Revoking an early configured member must not starve later members.
        client.post(f'/api/rooms/{room["id"]}/members/{contexts[0]["id"]}/control',json={'action':'revoke'}).raise_for_status()
        client.post(f'/api/rooms/{room["id"]}/messages',json={'body':f'@{names[1]} now start','nonce':'second'}).raise_for_status()
        wait_until(lambda:(Path(contexts[1]['temp_dir'])/'executions.txt').exists())
        assert read_json(contexts[0]['context'])['native_id']!=read_json(contexts[1]['context']).get('native_id','') or contexts[0]['requested_id']!=contexts[1]['requested_id']
    finally:
        if bridge:
            config.with_suffix('.stop').write_text('stop')
            try:bridge.wait(timeout=10)
            except subprocess.TimeoutExpired:bridge.kill()
        server.terminate()
        try:server.wait(timeout=10)
        except subprocess.TimeoutExpired:server.kill()


def test_model_update_during_task_applies_to_next_real_subprocess(tmp_path):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env={**os.environ,'AGENTCHAT_DATA':str(tmp_path/'server'),'AGENTCHAT_PORT':str(port),'PYTHONIOENCODING':'utf-8'}
    server=subprocess.Popen([sys.executable,'-m','server'],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    bridge=None
    try:
        c=httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=3)
        wait_until(lambda:c.get('/healthz').status_code==200)
        c.post('/api/bootstrap',json={'username':'tester','password':'model-test-password','setup_code':(tmp_path/'server/bootstrap-code').read_text()}).raise_for_status()
        room=c.post('/api/rooms',json={'name':'Model E2E'}).json()
        workspace=tmp_path/'workspace';workspace.mkdir()
        roles=[{'name':'worker','harness':'command','role':'fixture','model':'model-a','command':[sys.executable,str(ROOT/'tests/model_harness.py'),'{model}']}]
        roles_file=tmp_path/'roles.json';roles_file.write_text(json.dumps(roles))
        setup_result=subprocess.run([sys.executable,str(ROOT/'agentchat.py'),'setup','--server',str(c.base_url).rstrip('/'),'--workspace',str(workspace),'--roles',str(roles_file),'--name','setup-e2e'],env={**env,'AGENTCHAT_JOIN_TOKEN':room['join_token']},capture_output=True,timeout=15)
        assert setup_result.returncode==0,setup_result.stderr.decode(errors='replace')
        config=workspace/'.agentchat/setup-e2e.json'
        bridge=subprocess.Popen([sys.executable,str(ROOT/'agentchat.py'),'run','--config',str(config)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        ctx=read_json(read_json(config)['sessions'][0]['context']);temp=Path(ctx['temp_dir'])
        c.post(f'/api/rooms/{room["id"]}/messages',json={'body':'@worker first task','nonce':'first'}).raise_for_status()
        wait_until(lambda:(temp/'entered').exists())
        c.post(f'/api/rooms/{room["id"]}/members/{ctx["id"]}/model',json={'model':'model-b','revision':1}).raise_for_status()
        c.post(f'/api/rooms/{room["id"]}/messages',json={'body':'@worker second task','nonce':'second'}).raise_for_status()
        # Let another heartbeat run while the first subprocess is still executing.
        def pending():
            member=next(x for x in c.get('/api/rooms/'+room['id']).json()['members'] if x['id']==ctx['id'])
            return member['model_settings']['applied_revision']==1 and member['model_settings']['revision']==2
        wait_until(pending)
        assert read_json(ctx['context'])['definition']['model']=='model-a'
        (temp/'release').write_text('release')
        def replies():
            messages=c.get(f'/api/rooms/{room["id"]}/messages').json()
            agent_messages=[m for m in messages if m['sender_kind']=='agent']
            return agent_messages if len(agent_messages)==2 else None
        result=wait_until(replies)
        assert [m['body'] for m in result]==['@tester model=model-a','@tester model=model-b']
        assert [m['runtime']['model'] for m in result]==['model-a','model-b']
        assert read_json(ctx['context'])['native_id']==ctx['requested_id']
        assert read_json(ctx['context'])['definition']['model']=='model-b'
    finally:
        if bridge:
            config.with_suffix('.stop').write_text('stop')
            try:bridge.wait(timeout=10)
            except subprocess.TimeoutExpired:bridge.kill()
        server.terminate()
        try:server.wait(timeout=10)
        except subprocess.TimeoutExpired:server.kill()


def test_round_budget_and_stop_all_reach_real_processes(tmp_path):
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env={**os.environ,'AGENTCHAT_DATA':str(tmp_path/'server'),'AGENTCHAT_PORT':str(port),'PYTHONIOENCODING':'utf-8'}
    server=subprocess.Popen([sys.executable,'-m','server'],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    bridge=None
    try:
        c=httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=3)
        wait_until(lambda:c.get('/healthz').status_code==200)
        c.post('/api/bootstrap',json={'username':'tester','password':'round-test-password','setup_code':(tmp_path/'server/bootstrap-code').read_text()}).raise_for_status()
        room=c.post('/api/rooms',json={'name':'Round E2E'}).json()
        workspace=tmp_path/'workspace';workspace.mkdir()
        roles=[{'name':name,'harness':'command','role':'fixture','command':[sys.executable,str(ROOT/'tests/round_harness.py')]} for name in ('worker','reviewer')]
        roles_file=tmp_path/'roles.json';roles_file.write_text(json.dumps(roles))
        result=subprocess.run([sys.executable,str(ROOT/'agentchat.py'),'setup','--server',str(c.base_url).rstrip('/'),'--workspace',str(workspace),'--roles',str(roles_file),'--name','setup-e2e'],env={**env,'AGENTCHAT_JOIN_TOKEN':room['join_token']},capture_output=True,timeout=15)
        assert result.returncode==0,result.stderr.decode(errors='replace')
        config=workspace/'.agentchat/setup-e2e.json'
        bridge=subprocess.Popen([sys.executable,str(ROOT/'agentchat.py'),'run','--config',str(config)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        contexts=[read_json(s['context']) for s in read_json(config)['sessions']]
        path=f'/api/rooms/{room["id"]}/messages'
        c.post(path,json={'body':'@all 开始','nonce':'bounded','max_rounds':2}).raise_for_status()
        history=wait_until(lambda:(h if len(h:=c.get(path).json())==5 else None),timeout=25)
        final=[m for m in history if m['depth']==2]
        assert len(final)==2 and all(m['deliveries'][0]['status']=='skipped' for m in final)
        for ctx in contexts:
            folder=Path(ctx['temp_dir'])
            assert (folder/'executions.txt').read_text()=='2'
            assert '剩余 2 轮（包含本轮）' in (folder/'prompt-1.txt').read_text(encoding='utf-8')
            assert '【最后一轮，必须收尾】' in (folder/'prompt-2.txt').read_text(encoding='utf-8')
        c.post(path,json={'body':'@worker BLOCK_UNTIL_STOP','nonce':'blocked','max_rounds':0}).raise_for_status()
        pulse=Path(contexts[0]['temp_dir'])/'pulse.txt'
        wait_until(pulse.exists)
        # Wait for a heartbeat that observes this new process, not the previous idle snapshot.
        wait_until(lambda:read_json(config.with_suffix('.status.json')).get('running')==1,timeout=15)
        c.post(path,json={'body':'@worker 排队任务','nonce':'queued','max_rounds':0}).raise_for_status()
        stopped=c.post(f'/api/rooms/{room["id"]}/control',json={'action':'stop-all'}).json()
        assert stopped['cancelled']==2
        wait_until(lambda:read_json(config.with_suffix('.status.json')).get('running')==0,timeout=15)
        last=pulse.read_text();time.sleep(.5)
        assert pulse.read_text()==last
        assert len(c.get(path).json())==7
        assert (Path(contexts[0]['temp_dir'])/'executions.txt').read_text()=='3'
    finally:
        if bridge:
            config.with_suffix('.stop').write_text('stop')
            try:bridge.wait(timeout=10)
            except subprocess.TimeoutExpired:bridge.kill()
        server.terminate()
        try:server.wait(timeout=10)
        except subprocess.TimeoutExpired:server.kill()


def test_manager_tools_privacy_workspace_and_rate_limit_full_pipeline(tmp_path):
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env={**os.environ,'AGENTCHAT_DATA':str(tmp_path/'server'),'AGENTCHAT_PORT':str(port),'PYTHONIOENCODING':'utf-8'}
    server=subprocess.Popen([sys.executable,'-m','server'],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    bridge=None
    try:
        c=httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=3);wait_until(lambda:c.get('/healthz').status_code==200)
        c.post('/api/bootstrap',json={'username':'tester','password':'manager-test-password','setup_code':(tmp_path/'server/bootstrap-code').read_text()}).raise_for_status()
        room=c.post('/api/rooms',json={'name':'Manager E2E'}).json();workspace=tmp_path/'new-project'
        command=[sys.executable,str(ROOT/'tests/control_harness.py'),'{model}']
        roles=[{'name':'worker','harness':'command','role':'work','command':command},{'name':'manager','harness':'command','role':'manage','session_type':'manager','command':command}]
        rolefile=tmp_path/'roles.json';rolefile.write_text(json.dumps(roles))
        result=subprocess.run([sys.executable,str(ROOT/'agentchat.py'),'setup','--server',str(c.base_url).rstrip('/'),'--workspace',str(workspace),'--roles',str(rolefile),'--name','setup-e2e'],env={**env,'AGENTCHAT_JOIN_TOKEN':room['join_token']},capture_output=True,timeout=15)
        assert result.returncode==0,result.stderr.decode(errors='replace')
        config=workspace/'.agentchat/setup-e2e.json';saved=read_json(config);contexts={s['name']:read_json(s['context']) for s in saved['sessions']}
        assert contexts['manager']['project_key']==contexts['worker']['project_key']
        assert contexts['manager']['workspace']==contexts['worker']['workspace']==str(workspace)
        assert Path(contexts['manager']['manager_workspace']).is_dir()
        bridge=subprocess.Popen([sys.executable,str(ROOT/'agentchat.py'),'run','--config',str(config)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        base=f'/api/rooms/{room["id"]}';path=base+'/messages'
        c.post(path,json={'body':'@manager MANAGER_SECRET_DO_NOT_LEAK configure worker','nonce':'private'}).raise_for_status()
        wait_until(lambda:len(c.get(path).json())==2)
        assert (Path(contexts['manager']['manager_workspace'])/'managed-note.txt').read_text()=='private-file-ok'
        settings=next(m for m in c.get(base).json()['members'] if m['name']=='worker')['model_settings'];assert settings['model']=='model-b' and settings['reasoning_effort']=='high'
        c.post(path,json={'body':'@worker read public context','nonce':'public'}).raise_for_status()
        wait_until(lambda:len(c.get(path).json())==4)
        prompt=(Path(contexts['worker']['temp_dir'])/'received-prompt.txt').read_text(encoding='utf-8')
        assert 'MANAGER_SECRET_DO_NOT_LEAK' not in prompt and 'manager-private-done' not in prompt
        c.post(path,json={'body':'@worker FAIL_RATE_LIMIT','nonce':'limited'}).raise_for_status()
        def off():return next(m for m in c.get(base).json()['members'] if m['name']=='worker')['disabled_reason']=='rate_limit'
        wait_until(off)
        assert c.post(path,json={'body':'@worker must stay off','nonce':'off'}).json()['deliveries'][0]['status']=='skipped'
        assert not next(m for m in c.get(base).json()['members'] if m['name']=='manager')['paused']
    finally:
        if bridge:
            config.with_suffix('.stop').write_text('stop')
            try:bridge.wait(timeout=10)
            except subprocess.TimeoutExpired:bridge.kill()
        server.terminate()
        try:server.wait(timeout=10)
        except subprocess.TimeoutExpired:server.kill()
