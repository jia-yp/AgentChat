import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi.testclient import TestClient

from bridge.runner import Bridge
from bridge.uploads import MAX_FILE_BYTES
from tests.test_bridge_e2e import wait_until
from tests.test_steering import take
from tests.test_system import claim, send, system


ROOT = Path(__file__).resolve().parents[1]


def upload(client, room, name, content, *, upload_id=None, headers=None):
    upload_id = upload_id or uuid.uuid4().hex
    return client.post(f'/api/rooms/{room["id"]}/uploads/{upload_id}', content=content, headers={
        'Content-Type': 'application/octet-stream',
        'X-AgentChat-Filename': quote(name, safe=''),
        **(headers or {}),
    })


def path(room, message, upload_id):
    return f'/api/rooms/{room["id"]}/messages/{message["id"]}/attachments/{upload_id}'


def enable(client, bridge_headers, *agents, steer=False):
    client.post('/api/bridge/heartbeat', headers=bridge_headers, json={
        'sessions': [{'id': agent['id'], 'uploads_supported': True, 'steer_supported': steer}
                     for agent in agents],
    }).raise_for_status()


def test_upload_binding_is_atomic_idempotent_and_recall_hides_files(system):
    client, room, _, bridge_headers, agents, _ = system
    data = b'\x89PNG\r\n\x1a\n' + b'fixture' * 24000
    response = upload(client, room, '截图.png', data)
    response.raise_for_status()
    upload_id = response.json()['id']
    assert upload(client, room, '截图.png', data, upload_id=upload_id).json() == {'id': upload_id}
    assert upload(client, room, '截图.png', b'changed', upload_id=upload_id).status_code == 409
    assert send(client, room, '@codex1 请看附件', attachment_ids=[upload_id]).status_code == 409
    enable(client, bridge_headers, agents[1])

    assert send(client, room, '@codex1 请看附件', attachment_ids=[upload_id, uuid.uuid4().hex]).status_code == 409
    message = send(client, room, '@codex1 请看附件', nonce='same-send', attachment_ids=[upload_id]).json()
    assert message['attachments'] == [{'id': upload_id, 'name': '截图.png', 'size': len(data),
                                       'sha256': hashlib.sha256(data).hexdigest()}]
    assert send(client, room, '@codex1 请看附件', nonce='same-send', attachment_ids=[upload_id]).json()['id'] == message['id']
    assert send(client, room, '@codex1 请看附件', nonce='same-send').status_code == 409
    assert send(client, room, '@codex1 再发一次', attachment_ids=[upload_id]).status_code == 409

    url = path(room, message, upload_id)
    download = client.get(url)
    assert download.content == data
    assert download.headers['Content-Type'] == 'application/octet-stream'
    assert download.headers['Content-Disposition'].startswith("attachment; filename*=UTF-8''")
    assert TestClient(client.app).get(url).status_code == 401
    client.post(f'/api/rooms/{room["id"]}/messages/{message["id"]}/recall', json={}).raise_for_status()
    assert client.get(url).status_code == 404
    history = client.get(f'/api/rooms/{room["id"]}/messages').json()
    assert len(history) == 1 and 'attachments' not in history[0]


def test_upload_checks_request_size_name_and_origin(system):
    client, room, _, _, _, _ = system
    assert upload(client, room, '../escape.png', b'file').status_code == 400
    assert upload(client, room, 'forbidden.txt', b'file', headers={'Origin': 'https://other.example'}).status_code == 403
    assert upload(TestClient(client.app), room, 'anonymous.txt', b'file').status_code == 401
    assert upload(client, room, 'too-large.txt', iter([b'x' * MAX_FILE_BYTES, b'x'])).status_code == 413
    assert upload(client, room, 'empty.txt', b'').status_code == 200


def test_private_attachment_uses_message_and_room_access(system):
    client, room, _, bridge_headers, agents, _ = system
    response = client.post('/api/bridge/sessions', headers=bridge_headers, json={
        'name': 'private-manager', 'role': 'manager', 'harness': 'command', 'session_type': 'manager',
    })
    response.raise_for_status()
    manager = response.json()
    enable(client, bridge_headers, manager)
    upload_id = upload(client, room, 'private.txt', b'private file').json()['id']
    message = send(client, room, '@private-manager 请查看', attachment_ids=[upload_id]).json()
    manager_headers = {'Authorization': 'Bearer ' + manager['token']}
    worker_headers = {'Authorization': 'Bearer ' + agents[0]['token']}
    assert client.get(path(room, message, upload_id), headers=manager_headers).content == b'private file'
    assert client.get(path(room, message, upload_id), headers=worker_headers).status_code == 404
    assert send(client, room, '@claude1 转发', reply_to=message['id']).status_code == 403
    other_room = client.post('/api/rooms', json={'name': 'Other room'}).json()
    assert client.get(path(other_room, message, upload_id)).status_code == 404


def test_steer_claim_carries_attachments(system):
    client, room, _, bridge_headers, agents, store = system
    target = agents[1]
    with store.tx() as db:
        db.execute("UPDATE members SET harness='codex' WHERE id=?", (target['id'],))
    enable(client, bridge_headers, target, steer=True)
    send(client, room, '@codex1 正常工作').raise_for_status()
    active = claim(client, bridge_headers, target)
    upload_id = upload(client, room, '补充.txt', b'extra').json()['id']
    message = send(client, room, '@codex1 补充附件', attachment_ids=[upload_id]).json()
    queued = message['deliveries'][0]
    converted = client.post(f'/api/rooms/{room["id"]}/deliveries/{queued["id"]}/steer', json={
        'task_ref': queued['steer_option']['task_ref'],
    })
    converted.raise_for_status()
    notes = take(system, active).json()['notes']
    assert notes[0]['attachments'] == message['attachments']
    assert client.get(path(room, message, upload_id), headers={
        'Authorization': 'Bearer ' + target['token'],
    }).content == b'extra'


def test_harness_reads_uploads_in_each_workspace(tmp_path):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    data_dir = tmp_path / 'server'
    env = {**os.environ, 'AGENTCHAT_DATA': str(data_dir), 'AGENTCHAT_HOST': '127.0.0.1',
           'AGENTCHAT_PORT': str(port), 'PYTHONIOENCODING': 'utf-8'}
    server = subprocess.Popen([sys.executable, '-m', 'server'], cwd=ROOT, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=5) as client:
            wait_until(lambda: client.get('/healthz').status_code == 200)
            client.post('/api/bootstrap', json={
                'username': 'tester', 'password': 'test-password-long',
                'setup_code': (data_dir / 'bootstrap-code').read_text(),
            }).raise_for_status()
            room = client.post('/api/rooms', json={'name': 'Attachment transfer'}).json()
            harness = tmp_path / 'read_uploads.py'
            harness.write_text('''import hashlib, json, sys
from pathlib import Path
prompt = sys.stdin.read()
assert '用户上传了' in prompt
files = json.loads(next(line for line in prompt.splitlines() if line.startswith('[{"消息":')))
result = {}
for file in files:
    path = Path(file['本地路径'])
    assert path.resolve().is_relative_to(Path.cwd().resolve())
    result[file['文件名']] = hashlib.sha256(path.read_bytes()).hexdigest()
print(json.dumps({'response': '@tester '+json.dumps(result, ensure_ascii=False)}, ensure_ascii=False))
''', encoding='utf-8')
            bridges = []
            for name in ('first', 'second'):
                workspace = tmp_path / name
                roles = tmp_path / (name+'.json')
                roles.write_text(json.dumps([{
                    'name': name, 'harness': 'command', 'role': 'fixture',
                    'command': [sys.executable, str(harness)],
                }]), encoding='utf-8')
                config = workspace / '.agentchat' / (name+'.json')
                setup = subprocess.run([sys.executable, str(ROOT/'agentchat.py'), 'setup',
                    '--server', str(client.base_url).rstrip('/'), '--workspace', str(workspace),
                    '--roles', str(roles), '--name', 'bridge-'+name, '--config', str(config)],
                    env={**env, 'AGENTCHAT_JOIN_TOKEN': room['join_token']}, capture_output=True, timeout=15)
                assert setup.returncode == 0, setup.stderr.decode(errors='replace')
                bridge = Bridge(config)
                bridge.heartbeat()
                bridges.append(bridge)

            files = {'截图.png': b'\x89PNG\r\n\x1a\nbinary\x00', 'brief.txt': '测试文档'.encode()}
            ids = [upload(client, room, name, data).json()['id'] for name, data in files.items()]
            message = send(client, room, '@first @second 请查看附件', attachment_ids=ids)
            message.raise_for_status()
            for bridge in bridges:
                session = next(iter(bridge.sessions.values()))
                delivery = bridge.client.call(f'/api/bridge/sessions/{session["id"]}/claim', {})['delivery']
                bridge.worker(session, delivery, {'delivery': delivery, 'cancel': threading.Event()})
                folder = bridge.workspace / '.agentchat' / 'rooms' / room['id'] / 'uploads'
                assert {name: (folder / f'{upload_id}-{name}').read_bytes()
                        for upload_id, name in zip(ids, files)} == files

            history = client.get(f'/api/rooms/{room["id"]}/messages').json()
            assert [d['status'] for d in history[0]['deliveries']] == ['done', 'done']
            observed = [json.loads(m['body'].split(' ', 1)[1]) for m in history[1:]]
            expected = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
            assert observed == [expected, expected]
    finally:
        server.terminate()
        try:
            server.wait(timeout=8)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=8)
