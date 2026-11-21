"""Explicitly invoked, disposable localhost harness smoke test. Never production."""
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx

ROOT=Path(__file__).resolve().parents[1]
runtime=ROOT/'.runtime'
workspace=runtime/'live-workspace'
workspace.mkdir(parents=True,exist_ok=True)
client=httpx.Client(base_url='http://127.0.0.1:8789')
credentials={'username':'qa_owner','password':'local-qa-only-2026-password'}
if not client.get('/api/status').json()['initialized']:
    result=client.post('/api/bootstrap',json={**credentials,'setup_code':(runtime/'qa-data'/'bootstrap-code').read_text()})
else:result=client.post('/api/login',json=credentials)
result.raise_for_status()
room=client.post('/api/rooms',json={'name':'联调实验室 · 本地临时测试'}).json()
roles=[
    {'name':'codex_test_a','harness':'codex','role':'测试 session A。只响应明确点名的短测试，不修改项目文件。'},
    {'name':'codex_test_b','harness':'codex','role':'测试 session B。与 A 保持不同原生 session，不修改项目文件。'},
    {'name':'agy_test','harness':'antigravity','role':'测试 Antigravity 接入。只响应明确测试消息，不修改项目文件。'},
]
(runtime/'live-roles.json').write_text(json.dumps(roles,ensure_ascii=False,indent=2),encoding='utf-8')
(runtime/'live-test.json').write_text(json.dumps({'room':room,'credentials':credentials},ensure_ascii=False),encoding='utf-8')
env=os.environ.copy();env['AGENTCHAT_JOIN_TOKEN']=room['join_token']
result=subprocess.run([sys.executable,str(ROOT/'agentchat.py'),'setup','--server','http://127.0.0.1:8789','--workspace',str(workspace),'--roles',str(runtime/'live-roles.json'),'--name','setup-lab','--start'],env=env)
result.check_returncode()
print('Prepared disposable QA room; no harness has been activated.')
