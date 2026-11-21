"""Send or inspect tasks only in the local disposable smoke room."""
import json
import sys
import uuid
from pathlib import Path
import httpx

root=Path(__file__).resolve().parents[1]
info=json.loads((root/'.runtime/live-test.json').read_text(encoding='utf-8'))
c=httpx.Client(base_url='http://127.0.0.1:8789')
c.post('/api/login',json=info['credentials']).raise_for_status()
room=info['room']['id']
if len(sys.argv)>1 and sys.argv[1]=='send':
    for name in ('codex_test_a','codex_test_b','agy_test'):
        msg=f'@{name} 这是接入测试。不要使用工具或修改文件，只回复 @qa_owner CONNECTED {name}。正文中的 @codex_test_b 是指代，不是额外发送目标。'
        c.post(f'/api/rooms/{room}/messages',json={'body':msg,'nonce':str(uuid.uuid4())}).raise_for_status()
if len(sys.argv)>1 and sys.argv[1]=='resume':
    c.post(f'/api/rooms/{room}/messages',json={'body':'@codex_test_a 这是第二轮恢复测试。不要使用工具，只回复 @qa_owner RESUMED codex_test_a。','nonce':str(uuid.uuid4())}).raise_for_status()
members=c.get('/api/rooms/'+room).json()['members']
print(json.dumps([{'name':m['name'],'status':m['status'],'native_id':m['native_id'],'detail':m['detail']} for m in members],ensure_ascii=False,indent=2))
messages=c.get(f'/api/rooms/{room}/messages').json()
print(json.dumps([{'id':m['id'],'sender':m['sender_name'],'body':m['body'],'deliveries':m['deliveries']} for m in messages],ensure_ascii=False,indent=2))
