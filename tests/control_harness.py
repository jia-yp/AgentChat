"""Exercise the same manager CLI, file paths and terminal errors as native harnesses."""
import json,os,subprocess,sys
from pathlib import Path

context=json.loads(Path(os.environ['AGENTCHAT_CONTEXT']).read_text(encoding='utf-8'))
prompt=sys.stdin.read();folder=Path(context['temp_dir'])
(folder/'received-prompt.txt').write_text(prompt,encoding='utf-8')
task=prompt.rsplit('本次投递消息 ID=',1)[-1]
if context.get('session_type')=='manager':
    result=subprocess.run([sys.executable,context['cli'],'manage','--member','worker','--action','configure','--model','model-b','--effort','high'],capture_output=True,text=True,encoding='utf-8')
    if result.returncode:raise RuntimeError(result.stderr)
    (Path(context['manager_workspace'])/'managed-note.txt').write_text('private-file-ok',encoding='utf-8')
    response='@tester manager-private-done'
elif 'FAIL_RATE_LIMIT' in task:
    print('Native process exited after a provider error',file=sys.stderr)
    print(json.dumps({'session_id':context['requested_id'],'response':'','error':'HTTP 429 rate_limit_error quota exceeded'}));sys.exit(1)
else:response='@tester worker-ok'
print(json.dumps({'session_id':context['requested_id'],'response':response}))
