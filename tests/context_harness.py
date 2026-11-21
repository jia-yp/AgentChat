"""Deterministic real stdin fixture for context submission, never production."""
import json
import os
from pathlib import Path
import sys

prompt = sys.stdin.read()
Path(os.environ['AGENTCHAT_TEMP'], 'received-prompt.txt').write_text(prompt, encoding='utf-8')
mode = sys.argv[1]
error = ('prompt is too long' if mode == 'context-limit' else "You've hit your 5-hour limit · resets 12:10pm (America/New_York)")
print(json.dumps({'session_id': 'native-one', 'response': '@owner fixture complete' if mode == 'success' else '',
                  'error': '' if mode == 'success' else error,
                  'usage': {'input_tokens': 10 if mode == 'partial-quota' else 0}}, ensure_ascii=False))
sys.exit(0 if mode == 'success' else 1)
