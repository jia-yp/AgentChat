"""Deterministic subprocess fixture; never a production harness."""
import json
import os
import sys
from pathlib import Path

context=json.loads(Path(os.environ['AGENTCHAT_CONTEXT']).read_text(encoding='utf-8'))
sys.stdin.read()
temp=Path(os.environ['AGENTCHAT_TEMP'])
counter=temp/'executions.txt'
counter.write_text(str(int(counter.read_text())+1 if counter.exists() else 1))
print(json.dumps({'session_id':context.get('native_id') or context['requested_id'],'response':'@tester 完成 '+context['name']},ensure_ascii=False))
