"""Opt-in real Claude/remote-server round trip and native resume test (two model turns).

Run: python tests/claude_smoke.py --ssh user@your-server
Uses a disposable container on the remote Docker host and its downloaded client; never production data.
"""
import argparse
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh', required=True)
    parser.add_argument('--switch-model', help='Also change the room-managed model before the resume turn')
    args = parser.parse_args()
    name = 'agentchat-claude-' + uuid.uuid4().hex[:8]
    runtime = ROOT / '.runtime' / name
    runtime.mkdir(parents=True)
    print('Artifacts: ' + str(runtime), flush=True)
    report = {'started_utc': datetime.now(timezone.utc).isoformat(), 'passed': False}
    container_id = None
    tunnel = None
    config = None
    client = None
    bridge_pids = []

    def ssh(command):
        return subprocess.check_output(
            ['ssh', '-o', 'BatchMode=yes', args.ssh, command],
            text=True, encoding='utf-8', timeout=30,
        ).strip()

    def wait(fn, description, seconds=30):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            try:
                value = fn()
                if value:
                    return value
            except (OSError, httpx.HTTPError):
                pass
            time.sleep(.5)
        raise RuntimeError('Timed out: ' + description)

    def run_cli(*arguments, extra_env=None):
        completed = subprocess.run(
            [sys.executable, str(client_root / 'agentchat.py'), *arguments],
            cwd=client_root, env={**os.environ, 'PYTHONIOENCODING': 'utf-8', **(extra_env or {})},
            capture_output=True, text=True, encoding='utf-8', timeout=45,
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
        return completed.stdout

    def start_bridge():
        run_cli('start', '--config', str(config))
        status = json.loads(config.with_suffix('.status.json').read_text(encoding='utf-8'))
        bridge_pids.append(status['pid'])
        print('Bridge started: PID=' + str(status['pid']), flush=True)

    def stop_bridge():
        if config is None or not config.exists():
            return
        run_cli('stop', '--config', str(config))
        status_path = config.with_suffix('.status.json')
        if status_path.exists():
            wait(lambda: json.loads(status_path.read_text(encoding='utf-8')).get('stopped'), 'bridge stopped')

    def history():
        response = client.get(f'/api/rooms/{room_id}/messages?limit=200')
        response.raise_for_status()
        return response.json()

    def send(body):
        response = client.post(f'/api/rooms/{room_id}/messages', json={'body': body, 'nonce': str(uuid.uuid4())})
        response.raise_for_status()
        return response.json()

    def wait_reply(message_id):
        def complete():
            items = history()
            task = next(item for item in items if item['id'] == message_id)
            deliveries = task['deliveries']
            if len(deliveries) != 1:
                raise AssertionError('Expected exactly one Claude delivery')
            delivery = deliveries[0]
            if delivery['status'] in ('failed', 'cancelled'):
                raise RuntimeError('Claude delivery failed: ' + delivery['error'])
            if delivery['status'] != 'done':
                return None
            replies = [item for item in items if item['sender_name'] == 'claude_test' and item['reply_to'] == message_id]
            if len(replies) != 1:
                raise AssertionError('Expected exactly one persisted reply')
            assert delivery['attempts'] == 1, 'Task must not be re-executed'
            return replies[0]
        return wait(complete, 'Claude response', seconds=210)

    try:
        container_id = ssh(
            f'docker run -d --rm --name {name} --user 1000:1000 --cap-drop ALL '
            '-e AGENTCHAT_DATA=/tmp/agentchat -p 127.0.0.1::8781 agentchat-api'
        )
        if not re.fullmatch('[0-9a-f]{64}', container_id):
            raise RuntimeError('Unexpected container ID')
        remote_bind = ssh(f'docker port {container_id} 8781/tcp')
        if not re.fullmatch(r'127\.0\.0\.1:[0-9]+', remote_bind):
            raise RuntimeError('Expected loopback-only disposable API')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            local_port = sock.getsockname()[1]
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        tunnel = subprocess.Popen(
            ['ssh', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes', '-N',
             '-L', f'127.0.0.1:{local_port}:{remote_bind}', args.ssh],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options,
        )
        client = httpx.Client(base_url=f'http://127.0.0.1:{local_port}', timeout=5)
        wait(lambda: client.get('/healthz').status_code == 200, 'disposable NAS API')
        code = ssh(f'docker exec {container_id} cat /tmp/agentchat/bootstrap-code')
        client.post('/api/bootstrap', json={
            'username': 'tester', 'password': 'qa-' + uuid.uuid4().hex, 'setup_code': code,
        }).raise_for_status()
        response = client.post('/api/rooms', json={'name': 'Claude real round trip / disposable test'})
        response.raise_for_status()
        room = response.json()
        room_id = room['id']

        package = client.get('/downloads/agentchat-client.zip')
        package.raise_for_status()
        client_root = runtime / 'client'
        client_root.mkdir()
        with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
            for entry in archive.infolist():
                if not (client_root / entry.filename).resolve().is_relative_to(client_root.resolve()):
                    raise RuntimeError('Unsafe downloaded client path')
            archive.extractall(client_root)
        sys.path.insert(0, str(client_root))
        from bridge.adapters import discover
        from bridge.common import read_json
        from bridge.runner import Bridge

        executable = discover()['claude']['executable']
        if not executable:
            raise RuntimeError('Claude Code CLI not detected')
        report['claude_version'] = subprocess.check_output([executable, '--version'], text=True, encoding='utf-8').strip()
        report['nas_downloaded_client'] = True
        workspace = runtime / 'workspace'
        workspace.mkdir()
        roles = [{
            'name': 'claude_test', 'harness': 'claude',
            'role': '仅响应管理员的接入与记忆测试；不要修改文件或调用工具。',
            'timeout_seconds': 180,
            # Make memory retrieval independent of filesystem, MCP and browser tools.
            'args': ['--tools', '', '--strict-mcp-config', '--no-chrome', '--effort', 'low'],
        }]
        roles_path = runtime / 'roles.json'
        roles_path.write_text(json.dumps(roles, ensure_ascii=False, indent=2), encoding='utf-8')
        run_cli('setup', '--server', str(client.base_url).rstrip('/'), '--workspace', str(workspace),
                '--roles', str(roles_path), '--name', 'setup-claude',
                extra_env={'AGENTCHAT_JOIN_TOKEN': room['join_token']})
        config = workspace / '.agentchat' / 'setup-claude.json'
        context = Path(read_json(config)['sessions'][0]['context'])
        start_bridge()
        member = next(item for item in client.get(f'/api/rooms/{room_id}').json()['members'] if item['name'] == 'claude_test')
        assert not member['enabled'] and not read_json(context)['native_id']
        assert not list((context.parent / 'logs').glob('*.stdout.log'))
        report['dormant_until_administrator_mention'] = True
        marker = 'MEMORY-' + uuid.uuid4().hex
        first_task = send(
            f'@claude_test 这是一次原生 session 恢复测试。请在本次会话中记住校验词 {marker}。'
            '不要使用工具、不要读写文件，也不要把校验词写入最终回复。'
            '现在最终回复必须严格为：@tester CONNECTED claude_test。稍后我会问你这个校验词。'
        )
        first_reply = wait_reply(first_task['id'])
        assert first_reply['body'].startswith('@tester') and 'CONNECTED claude_test' in first_reply['body']
        assert marker not in first_reply['body']
        first_native = read_json(context)['native_id']
        assert first_native == read_json(context)['requested_id']
        report['first_round_trip'] = True
        report['first_native_id'] = first_native
        print('First NAS -> bridge -> Claude -> NAS round trip passed.', flush=True)

        # A real bridge process restart, with no active model process to preserve memory.
        stop_bridge()
        report['bridge_restarted'] = True
        if args.switch_model:
            member = next(item for item in client.get(f'/api/rooms/{room_id}').json()['members'] if item['name']=='claude_test')
            changed=client.post(f'/api/rooms/{room_id}/members/{member["id"]}/model',json={
                'model':args.switch_model,'revision':member['model_settings']['revision'],
            })
            changed.raise_for_status()
            report['requested_model_change']=args.switch_model
        # The bridge supplies the latest 30 messages. These owner-only messages ensure
        # neither the original task nor the quote in its reply reveals the marker.
        for index in range(32):
            send(f'@admin 恢复测试的历史间隔 {index + 1}；无需 agent 执行。')
        second_task = send(
            '@claude_test 现在验证原生 session 恢复。请只根据你之前会话的记忆，'
            '取出我最初要求你记住、但不允许你写入回复的校验词。'
            '不要调用工具、读取文件或群聊历史。最终回复严格为：@tester RESUMED 加一个空格和该校验词。'
            '如果无法记住，请明确回复 @tester MEMORY_MISSING，禁止猜测。'
        )
        second_message = next(item for item in history() if item['id'] == second_task['id'])
        actual_prompt = Bridge(config).prompt(read_json(context), {'message': second_message})
        assert marker not in actual_prompt, 'Memory test invalid: marker present in new prompt'
        (runtime / 'resume-input.txt').write_text(actual_prompt, encoding='utf-8')
        report['memory_marker_absent_from_resume_prompt'] = True
        start_bridge()
        assert bridge_pids[0] != bridge_pids[1]
        second_reply = wait_reply(second_task['id'])
        assert second_reply['body'].startswith('@tester') and 'RESUMED ' + marker in second_reply['body']
        second_native = read_json(context)['native_id']
        assert second_native == first_native
        report['second_round_trip'] = True
        report['second_native_id'] = second_native
        report['same_native_session'] = True
        report['native_memory_preserved'] = True

        outputs = sorted((context.parent / 'logs').glob('*.stdout.log'), key=lambda path: path.stat().st_mtime)
        assert len(outputs) == 2, 'Expected exactly two harness invocations'
        envelopes = [json.loads(path.read_text(encoding='utf-8')) for path in outputs]
        assert all(item.get('session_id') == first_native and not item.get('is_error') for item in envelopes)
        assert all(item.get('num_turns') == 1 for item in envelopes), 'Memory test must not call tools'
        report['native_success_envelopes'] = 2
        if args.switch_model:
            models=[list(item.get('modelUsage',{})) for item in envelopes]
            assert all(models) and models[0]!=models[1], 'Native CLI did not report a different model'
            assert read_json(context)['definition']['model']==args.switch_model
            report['native_models_by_turn']=models
            report['managed_model_change_verified']=True
        report['bridge_pids'] = bridge_pids
        report['messages'] = [
            {'id': item['id'], 'sender': item['sender_name'], 'body': item['body'], 'reply_to': item['reply_to']}
            for item in (first_reply, second_reply)
        ]
        (runtime / 'messages.json').write_text(json.dumps(history(), ensure_ascii=False, indent=2), encoding='utf-8')
        report['production_untouched'] = True
        report['passed'] = True
        print('Bridge restart + exact native session + isolated memory check passed.', flush=True)
    except Exception as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        cleanup_errors = []
        try:
            stop_bridge()
            report['test_bridge_stopped'] = True
        except Exception as exc:
            cleanup_errors.append('bridge: ' + str(exc))
        if client:
            client.close()
        if tunnel:
            tunnel.terminate()
            try:
                tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel.kill()
                tunnel.wait(timeout=5)
        if container_id and re.fullmatch('[0-9a-f]{64}', container_id):
            try:
                ssh(f'docker rm -f {container_id}')
                report['disposable_container_removed'] = True
            except Exception as exc:
                cleanup_errors.append('container: ' + str(exc))
        report['finished_utc'] = datetime.now(timezone.utc).isoformat()
        if cleanup_errors:
            report['cleanup_errors'] = cleanup_errors
            report['passed'] = False
        (runtime / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        if cleanup_errors:
            raise RuntimeError('Test cleanup incomplete: ' + '; '.join(cleanup_errors))


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    main()
