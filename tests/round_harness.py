"""Test-only peer relay and cancellable process; never a production harness."""
import json,os,sys,time
from pathlib import Path

context=json.loads(Path(os.environ['AGENTCHAT_CONTEXT']).read_text(encoding='utf-8'))
folder=Path(os.environ['AGENTCHAT_TEMP']);prompt=sys.stdin.read()
counter=folder/'executions.txt';count=int(counter.read_text())+1 if counter.exists() else 1
counter.write_text(str(count));(folder/f'prompt-{count}.txt').write_text(prompt,encoding='utf-8')
if 'BLOCK_UNTIL_STOP' in prompt.rsplit('本次投递消息 ID=',1)[-1]:
    while True:
        (folder/'pulse.txt').write_text(str(time.time()))
        time.sleep(.1)
peer='reviewer' if context['name']=='worker' else 'worker'
print(json.dumps({'session_id':context['requested_id'],'response':'@'+peer+' 最后总结' if '【最后一轮，必须收尾】' in prompt else '@'+peer+' 接力讨论'}))
