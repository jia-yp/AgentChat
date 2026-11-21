"""Subprocess-only manager fixture exercising its own scoped CLI identity."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
from bridge.processes import background_options

context=json.loads(Path(os.environ['AGENTCHAT_CONTEXT']).read_text(encoding='utf-8'))
prompt=sys.stdin.read()
rid=re.search(r'--request ([a-f0-9]{24})',prompt).group(1)
if os.name=='nt':
    command=['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(root/'bridge/sessions.ps1'),'-Request',rid,'-Action','apply']
else:command=[sys.executable,str(root/'agentchat.py'),'sessions','--request',rid,'--action','apply']
result=subprocess.run(command,capture_output=True,**background_options())
if result.returncode:
    sys.stderr.buffer.write(result.stderr);sys.exit(result.returncode)
receipt=json.loads(result.stdout.decode('utf-8-sig'))
assert receipt['status'] in ('applying','complete')
Path(context['manager_workspace'],'last-change.txt').write_text(rid)
print(json.dumps({'session_id':context.get('native_id') or context['requested_id'],'response':'@tester {"action":"apply"}'}))
