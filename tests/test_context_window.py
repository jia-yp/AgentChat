import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from bridge.common import read_json
from bridge.context_window import ContextWindow, inference_started, prompt_limit
from bridge.harness_errors import HarnessFailure
from bridge.runner import Bridge
from test_system import system, send


def message(mid, text=None, **extra):
    return {'id': mid, 'body': text or f'unique-body-{mid:04d}', 'sender': 'owner',
            'sender_name': 'owner', 'targets': ['worker'], 'created': mid, **extra}


def session(tmp_path, **extra):
    return {'id': 'worker', 'name': 'worker', 'native_id': 'native-one',
            'context': str(tmp_path / 'session.json'), 'temp_dir': str(tmp_path),
            'definition': {'harness': 'command', 'role': 'implement', 'command': ['fixture']}, **extra}


class History:
    def __init__(self, messages):
        self.messages = messages
        self.requests = []

    def call(self, path):
        self.requests.append(path)
        if '/messages?' not in path:
            return {'room': {'name': 'test'}, 'members': []}
        q = parse_qs(urlsplit(path).query)
        after = int(q['after'][0]); through = int(q['through'][0])
        return [m for m in self.messages if after < m['id'] <= through][:int(q['limit'][0])]


def prepare(s, history, current, **delivery):
    bridge = Bridge.__new__(Bridge)
    bridge.config = {'room': {'id': 'room'}}; bridge.workspace = Path(s['temp_dir'])
    bridge.client = history
    return bridge.prompt(s, {'id': 'delivery-' + str(current['id']), 'message': current, **delivery})


def accepted(prompt):
    prompt.context.submit(); prompt.context.finish()


def test_full_increment_paginates_and_survives_restart_without_repeating(tmp_path):
    s = session(tmp_path)
    s['definition']['max_prompt_chars'] = 200000
    rows = [message(i) for i in range(1, 451)]
    first = prepare(s, History(rows), rows[9]); accepted(first)
    s = json.loads(json.dumps(s))  # New process/session object; cursor is on disk.
    history = History(rows)
    second = prepare(s, history, rows[-1]); accepted(second)
    assert len([r for r in history.requests if '/messages?' in r]) == 3
    assert 'after=10&through=450' in history.requests[1]
    assert all(second.count(m['body']) == 1 for m in rows[10:])
    assert all(m['body'] not in second for m in rows[:10])
    assert read_json(second.context.path)['through'] == 450


def test_task_quotes_and_bundled_inbox_appear_once_then_only_references(tmp_path):
    s = session(tmp_path)
    rows = [message(1), message(2, quote={'id': 1, 'body': 'unique-body-0001'})]
    sync = {'cutoff': 2, 'previous_messages': rows, 'inbox': rows, 'quota': 1, 'used': 0}
    first = prepare(s, History(rows), rows[-1], synchronous=sync)
    assert all(first.count(m['body']) == 1 for m in rows)
    accepted(first)
    retry = prepare(s, History(rows), rows[-1], synchronous=sync, recovery_mode='continue')
    assert all(m['body'] not in retry for m in rows)
    assert '继续' in retry and '本轮完整收件箱任务 ID' in retry
    assert json.loads((retry.context.bundle / 'tasks.json').read_text()) == [1, 2]


def test_out_of_order_steer_does_not_skip_intervening_messages(tmp_path):
    s = session(tmp_path)
    rows = [message(i) for i in range(1, 11)]
    first = prepare(s, History(rows), rows[1]); accepted(first)
    first.context.mark_steers([{'message': 9}])
    own = message(4, 'already-authored', sender='worker')
    rows[3] = own
    second = prepare(s, History(rows), rows[-1])
    assert 'unique-body-0009' not in second and 'already-authored' not in second
    assert all(rows[i-1]['body'] in second for i in [3, 5, 6, 7, 8, 10])
    accepted(second)
    assert read_json(second.context.path)['seen'] == []


@pytest.mark.parametrize('limit', [4096, 24000])
def test_long_input_bounded_and_every_original_preserved(tmp_path, limit):
    s = session(tmp_path); s['definition']['max_prompt_chars'] = limit
    rows = [message(i, f'message-{i}:' + '模型📖' * 7000) for i in range(1, 81)]
    prompt = prepare(s, History(rows), rows[-1])
    assert len(prompt) <= limit
    assert '全文' in prompt and '材料目录' in prompt
    saved = [json.loads(line) for line in (prompt.context.bundle / 'messages.jsonl').read_text(encoding='utf-8').splitlines()]
    assert [m['text'] for m in saved] == [m['body'] for m in rows]
    accepted(prompt)
    next_row = message(81)
    following = prepare(s, History(rows + [next_row]), next_row)
    assert following.context.messages == [{'id': 81, 'sender': 'owner', 'time': 81, 'text': next_row['body']}]


def test_large_roster_and_thousands_of_task_ids_still_obey_limit(tmp_path):
    s = session(tmp_path); s['definition']['max_prompt_chars'] = 4096
    window = ContextWindow(s, 'room', {'message': message(1)})
    prompt = window.render('large-role-and-roster' * 5000, 'last-round-rule' * 5000,
                           tasks=list(range(1, 9000)), payload={'body': 'long' * 30000})
    assert len(prompt) <= 4096
    assert (window.bundle / 'instructions.txt').read_text().startswith('large-role')
    assert len(read_json(window.bundle / 'tasks.json')) == 8999
    assert len(read_json(window.bundle / 'task.json')['body']) == 120000


def test_rejected_input_is_not_consumed_but_unknown_submission_is_not_replayed(tmp_path):
    s = session(tmp_path); row = message(1)
    never_sent = prepare(s, History([row]), row)
    assert not never_sent.context.path.exists()
    assert row['body'] in prepare(s, History([row]), row)
    never_sent.context.submit(); never_sent.context.finish(rejected=True)
    assert row['body'] in prepare(s, History([row]), row)
    again = prepare(s, History([row]), row); again.context.submit()
    # Crash after a successful pipe write / native turn acceptance. Recovery
    # uses the same native session and archived task; do not append it twice.
    assert row['body'] not in prepare(s, History([row]), row)


def test_new_native_session_gets_context_and_old_native_adopts_initial_id(tmp_path):
    s = session(tmp_path, native_id=''); row = message(1)
    prompt = prepare(s, History([row]), row); prompt.context.submit()
    s['native_id'] = 'created-native'; prompt.context.finish()
    assert row['body'] not in prepare(s, History([row]), row)
    s['native_id'] = 'deliberately-new-native'
    assert row['body'] in prepare(s, History([row]), row)


def test_legacy_native_migrates_from_completed_chat_not_new_private_jobs(tmp_path):
    s = session(tmp_path)
    rows = [message(1), message(2, deliveries=[{'member': 'worker', 'status': 'done'}]),
            message(3), message(4, message_kind='summary_request', deliveries=[{'member': 'worker', 'status': 'done'}]), message(5)]
    prompt = prepare(s, History(rows), rows[-1]); accepted(prompt)
    assert rows[0]['body'] not in prompt and rows[1]['body'] not in prompt
    assert rows[2]['body'] in prompt
    assert read_json(prompt.context.path)['migration'] == 'last_completed_chat_task'


@pytest.mark.parametrize('value', [0, -1, 4095, 200001, True, '24000', None])
def test_invalid_limits_cannot_silently_disable_protection(value):
    with pytest.raises(ValueError): prompt_limit({'max_prompt_chars': value})


def test_bounded_history_retains_privacy_and_sync_gate(system):
    c, room, b, bh, ss, store = system
    one = send(c, room, '@claude1 first').json()
    two = send(c, room, '@claude1 second').json()
    manager = c.post('/api/bridge/sessions', headers=bh, json={'name': 'manager', 'role': 'private', 'harness': 'command', 'session_type': 'manager'}).json()
    hidden = send(c, room, '@manager secret').json()
    three = send(c, room, '@claude1 third').json()
    url = f'/api/rooms/{room["id"]}/messages?after={one["id"]}&through={three["id"]}&limit=200'
    worker = c.get(url, headers={'Authorization': 'Bearer ' + ss[0]['token']}).json()
    assert [m['id'] for m in worker] == [two['id'], three['id']]
    private = c.get(url, headers={'Authorization': 'Bearer ' + manager['token']}).json()
    assert [m['id'] for m in private] == [two['id'], hidden['id'], three['id']]
    from test_synchronous import enable_sync, claim
    # Finish earlier ordinary tasks before claiming the sync run.
    while task := claim(c, bh, ss[0]):
        c.post('/api/bridge/deliveries/' + task['id'] + '/finish', headers=bh, json={'lease': task['lease'], 'body': '@owner done'}).raise_for_status()
    enable_sync(c, bh, ss)
    result = send(c, room, '@all start round', round_mode='synchronous', max_rounds=3)
    result.raise_for_status(); root = result.json()
    assert claim(c, bh, ss[0])['synchronous']
    send(c, room, '@all hidden next round', round_mode='synchronous', max_rounds=3).raise_for_status()
    data = c.get(f'/api/rooms/{room["id"]}/messages?after={root["id"]}&through=999999', headers={'Authorization': 'Bearer ' + ss[1]['token']}).json()
    assert not any('hidden next round' in m['body'] for m in data)


@pytest.mark.parametrize('text', ["You've hit your 5-hour limit · resets 12:10pm (America/New_York)",
                               "You’ve reached your weekly limit", "You've hit your daily limit"])
def test_real_subscription_limit_messages_are_classified(text):
    assert HarnessFailure(text).kind == 'rate_limit'


def test_only_pre_inference_errors_are_safe_to_roll_back():
    assert not inference_started(json.dumps({'type': 'result', 'is_error': True, 'usage': {'input_tokens': 0}}))
    assert inference_started(json.dumps({'type': 'result', 'is_error': True, 'usage': {'cache_read_input_tokens': 123}}))
    assert inference_started('{"type":"thread.started"}\n{"type":"item.completed"}')
    assert HarnessFailure('prompt is too long').kind == 'context_limit'


@pytest.mark.parametrize('outcome', ['success', 'quota', 'partial-quota', 'context-limit', 'missing-executable'])
def test_real_subprocess_cursor_acceptance_and_recovery(tmp_path, outcome):
    import sys
    import threading
    from bridge.common import write_json
    s = session(tmp_path)
    s.update(requested_id='native-one', token='fixture')
    script = Path(__file__).with_name('context_harness.py')
    s['definition']['command'] = ['missing-fixture-executable'] if outcome == 'missing-executable' else [sys.executable, str(script), outcome]
    write_json(s['context'], s)
    bridge = Bridge.__new__(Bridge); bridge.workspace = tmp_path; bridge.stop = threading.Event()
    bridge.steer_capabilities = {}; bridge.sessions = {'worker': s}
    bridge.config = {'room': {'id': 'room'}, 'token': 'fixture'}
    row = message(1); bridge.client = History([row])
    args = (s, {'id': 'first', 'lease': 'fixture', 'message': row}, {'cancel': threading.Event()}, tmp_path)
    if outcome == 'success':
        assert bridge.execute(*args)['body'] == '@owner fixture complete'
    elif outcome == 'missing-executable':
        with pytest.raises(OSError): bridge.execute(*args)
    else:
        with pytest.raises(HarnessFailure) as error: bridge.execute(*args)
        assert error.value.kind == ('context_limit' if outcome == 'context-limit' else 'rate_limit')
    again = prepare(read_json(s['context']), History([row]), row)
    assert (row['body'] in again) == (outcome in ('quota', 'context-limit', 'missing-executable'))
    if outcome != 'missing-executable':
        actual = (tmp_path / 'received-prompt.txt').read_text(encoding='utf-8')
        assert actual.count(row['body']) == 1 and len(actual) <= 24000
