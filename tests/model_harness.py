"""Test subprocess: record the received model and optionally hold the first turn."""
import json
import os
import sys
import time
from pathlib import Path

session=json.loads(Path(os.environ['AGENTCHAT_CONTEXT']).read_text(encoding='utf-8'))
sys.stdin.read()
temp=Path(session['temp_dir'])
if not session.get('native_id'):
    (temp/'entered').write_text('running',encoding='utf-8')
    deadline=time.monotonic()+25
    while not (temp/'release').exists():
        if time.monotonic()>deadline:raise RuntimeError('Test release missing')
        time.sleep(.1)
print(json.dumps({'session_id':session['requested_id'],'response':'@tester model='+sys.argv[1]}))
