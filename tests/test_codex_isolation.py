import hashlib,json,sqlite3,threading,time
from pathlib import Path

import pytest

from bridge.common import ProcessLock,read_json,write_json
from bridge import codex_isolation as isolation


def source_home(tmp_path):
    source=tmp_path/'desktop';source.mkdir()
    (source/'config.toml').write_text('model="test"\nsqlite_home="old"\nlog_dir="old"\n[windows]\nsandbox="elevated"\n')
    (source/'auth.json').write_text('DO NOT COPY ROTATING TOKEN')
    rollout=source/'sessions/2026/11/18/thread.jsonl';rollout.parent.mkdir(parents=True)
    rollout.write_text(json.dumps({'type':'session_meta','payload':{'id':'original'}})+'\n'+
                       json.dumps({'type':'message','body':'previous task and answer'})+'\n'+'unfinished',encoding='utf-8')
    with sqlite3.connect(source/'state_5.sqlite') as db:
        db.executescript('''CREATE TABLE _sqlx_migrations(version INTEGER PRIMARY KEY);
          CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,project_id TEXT);
          CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT);
          CREATE TABLE project_roots(project_id TEXT,path TEXT);
          CREATE TABLE thread_dynamic_tools(thread_id TEXT,name TEXT);
          CREATE TABLE private_data(secret TEXT);''')
        db.execute('INSERT INTO _sqlx_migrations VALUES(1)')
        db.execute('INSERT INTO threads VALUES(?,?,?)',('original',str(rollout),'project'))
        db.execute('INSERT INTO threads VALUES(?,?,?)',('unrelated',str(rollout),'other'))
        db.execute('INSERT INTO projects VALUES(?,?)',('project','Same Project'))
        db.execute('INSERT INTO project_roots VALUES(?,?)',('project',str(tmp_path)))
        db.execute('INSERT INTO thread_dynamic_tools VALUES(?,?)',('original','tool'))
        db.execute('INSERT INTO private_data VALUES(?)',('unrelated secret',))
    return source,rollout


def test_selective_immutable_snapshot_retains_history_without_auth_or_locks(tmp_path):
    source,rollout=source_home(tmp_path)
    original=rollout.read_bytes();dbhash=hashlib.sha256((source/'state_5.sqlite').read_bytes()).hexdigest()
    target=isolation.initialize_home(tmp_path/'independent',source)
    assert not (target/'auth.json').exists()
    assert not (target/'thread-writer-locks').exists()
    result=isolation.snapshot_thread(source,target,'original')
    assert Path(result['copied_rollout']).read_bytes()==original[:original.rfind(b'\n')+1]
    with sqlite3.connect(target/'state_5.sqlite') as db:
        assert db.execute('SELECT id FROM threads').fetchall()==[('original',)]
        assert db.execute('SELECT name FROM projects').fetchone()==('Same Project',)
        assert db.execute('SELECT COUNT(*) FROM private_data').fetchone()==(0,)
    assert hashlib.sha256((source/'state_5.sqlite').read_bytes()).hexdigest()==dbhash
    rollout.write_bytes(original+b'-changed\n')
    assert isolation.snapshot_thread(source,target,'original')==result
    assert Path(result['copied_rollout']).read_bytes()!=rollout.read_bytes()


def test_missing_source_or_auth_never_changes_binding(tmp_path):
    source,_=source_home(tmp_path)
    target=isolation.initialize_home(tmp_path/'independent',source)
    s={'native_id':'original','definition':{'harness':'codex'},'codex_source_home':str(source)}
    with pytest.raises(ValueError,match='登录一次'):isolation.prepare(s,target)
    assert s['native_id']=='original'
    with pytest.raises(ValueError,match='未找到指定'):isolation.snapshot_thread(source,target,'missing')
    with pytest.raises(ValueError,match='分开'):isolation.initialize_home(source,source)


def test_paginated_history_copies_only_matching_thread_rows(tmp_path):
    source,rollout=source_home(tmp_path)
    with sqlite3.connect(source/'state_5.sqlite') as db:
        db.execute('ALTER TABLE threads ADD COLUMN history_mode TEXT DEFAULT "paginated"')
        db.execute('ALTER TABLE threads ADD COLUMN has_user_event INTEGER DEFAULT 1')
    target=isolation.initialize_home(tmp_path/'independent',source)
    with pytest.raises(ValueError,match='分页历史索引缺失'):isolation.snapshot_thread(source,target,'original')
    with sqlite3.connect(source/'thread_history_1.sqlite') as db:
        db.executescript('''CREATE TABLE _sqlx_migrations(version INTEGER PRIMARY KEY);
          CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT);
          CREATE TABLE thread_items(thread_id TEXT,item_json TEXT);
          CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY,next_rollout_byte_offset INTEGER);''')
        db.execute('INSERT INTO _sqlx_migrations VALUES(1)')
        for native in ('original','unrelated'):
            db.execute('INSERT INTO thread_turns VALUES(?,?)',(native,'turn'))
            db.execute('INSERT INTO thread_items VALUES(?,?)',(native,'history-'+native))
        db.execute('INSERT INTO thread_history_projection_state VALUES(?,?)',('original',rollout.read_bytes().rfind(b'\n')+1))
    result=isolation.snapshot_thread(source,target,'original')
    assert result['history_counts']['thread_turns']==1
    with sqlite3.connect(target/'thread_history_1.sqlite') as db:
        assert db.execute('SELECT * FROM thread_items').fetchall()==[('original','history-original')]


def test_native_fork_and_cursor_survive_restart_without_reforking(tmp_path):
    source,_=source_home(tmp_path);target=isolation.initialize_home(tmp_path/'independent',source)
    (target/'auth.json').write_text('{}')
    s={'native_id':'original','name':'worker','definition':{'harness':'codex','executable':'fake','permissions':'full'},
       'codex_source_home':str(source),'context':str(tmp_path/'worker/session.json'),'workspace':str(tmp_path)}
    cursor=tmp_path/'worker/chat-context/cursor.json'
    write_json(cursor,{'native_id':'original','through':123,'seen':[130]})
    calls=[]
    class Rpc:
        def __init__(self,cmd,cwd,env,*logs):
            assert env['CODEX_HOME']==env['CODEX_SQLITE_HOME']==str(target)
            assert cwd==tmp_path
        def request(self,method,params):calls.append((method,params));return method,params
        def wait(self,request,cancelled):
            method,params=request
            return {'thread':{'id':'forked' if method=='thread/fork' else params['threadId']}} if method in ('thread/fork','thread/read') else {}
        def send(self,*args):pass
        def close(self):pass
    result=isolation.prepare(s,target,rpc_factory=Rpc)
    assert s['native_id']=='forked' and result['source_native_id']=='original'
    assert read_json(cursor)=={'native_id':'forked','through':123,'seen':[130]}
    fork_params=next(params for method,params in calls if method=='thread/fork')
    assert fork_params['threadId']=='original'
    assert 'config' not in fork_params and result['verified_history']
    # Simulate a crash after the journal and cursor write but before binding save.
    s['native_id']='original'
    isolation.prepare(s,target,rpc_factory=lambda *a:pytest.fail('must reuse durable fork'))
    assert s['native_id']=='forked'
    isolation.prepare(read_json(s['context']),target,rpc_factory=lambda *a:pytest.fail('no second fork'))
    assert len([m for m,p in calls if m=='thread/fork'])==1


def test_new_session_uses_private_home_without_inference_or_history_copy(tmp_path):
    source,_=source_home(tmp_path);target=isolation.initialize_home(tmp_path/'independent',source)
    (target/'auth.json').write_text('{}')
    s={'native_id':'','definition':{'harness':'codex'},'context':str(tmp_path/'session.json'),'codex_source_home':str(source)}
    isolation.prepare(s,target,rpc_factory=lambda *a:pytest.fail('new dormant session must not invoke model'))
    s['native_id']='later-created'
    isolation.prepare(s,target,rpc_factory=lambda *a:pytest.fail('must not reset native conversation'))
    assert s['native_id']=='later-created' and not (target/'state_5.sqlite').exists()
    env=isolation.environment(s,{'CODEX_HOME':'desktop','CODEX_SQLITE_HOME':'desktop','CODEX_THREAD_ID':'old','HTTPS_PROXY':'keep'})
    assert env['CODEX_HOME']==env['CODEX_SQLITE_HOME']==str(target)
    assert 'CODEX_THREAD_ID' not in env and env['HTTPS_PROXY']=='keep'


def test_migration_waits_for_concurrent_metadata_work_and_can_cancel(tmp_path):
    cancel=threading.Event();result=[]
    def waiter():
        try:
            with isolation.migration_lock(tmp_path,cancel.is_set):result.append('acquired')
        except RuntimeError:result.append('cancelled')
    with ProcessLock(tmp_path/'agentchat-migration.lock'):
        thread=threading.Thread(target=waiter);thread.start();time.sleep(.15)
        assert not result
        cancel.set();thread.join(2)
    assert result==['cancelled']


def test_model_metadata_and_catalog_follow_the_independent_home(tmp_path,monkeypatch):
    from bridge.runtime_metadata import configured_runtime
    from bridge.model_catalog import ModelCatalogs
    source,_=source_home(tmp_path);target=isolation.initialize_home(tmp_path/'independent',source)
    (target/'config.toml').write_text('model="private-model"\nmodel_reasoning_effort="high"\n')
    monkeypatch.setenv('CODEX_HOME',str(source))
    s={'definition':{'harness':'codex'},'workspace':str(tmp_path),'codex_home':str(target)}
    assert configured_runtime(s)=={'harness':'codex','model':'private-model','think_level':'high'}
    assert ModelCatalogs().key(s)!=ModelCatalogs().key({**s,'codex_home':str(source)})


def test_imported_fork_keeps_its_parent_history_in_the_independent_home(tmp_path):
    source,rollout=source_home(tmp_path);target=isolation.initialize_home(tmp_path/'independent',source)
    child=rollout.with_name('child.jsonl')
    child.write_text(json.dumps({'type':'session_meta','payload':{'id':'child','history_base':{
        'thread_id':'original','end_byte_offset':rollout.read_bytes().rfind(b'\n')+1}}})+'\n')
    with sqlite3.connect(source/'state_5.sqlite') as db:
        db.execute('INSERT INTO threads VALUES(?,?,?)',('child',str(child),'project'))
    isolation.snapshot_thread(source,target,'child')
    with sqlite3.connect(target/'state_5.sqlite') as db:
        assert {r[0] for r in db.execute('SELECT id FROM threads')}=={'child','original'}
    assert (target/'agentchat-imports/original.json').exists()


def test_manager_already_in_private_home_forks_without_copying_itself(tmp_path):
    source,_=source_home(tmp_path);target=isolation.initialize_home(tmp_path/'independent',source)
    (target/'auth.json').write_text('{}')
    s={'native_id':'private-manager','name':'manager','definition':{'harness':'codex','executable':'fake'},
       'codex_source_home':str(target),'context':str(tmp_path/'manager/session.json'),'workspace':str(tmp_path)}
    class Rpc:
        def __init__(self,*args):pass
        def request(self,method,params):return method,params
        def wait(self,request,cancelled):
            method,params=request
            return {'thread':{'id':'new-private-manager' if method=='thread/fork' else params['threadId']}} if method in ('thread/read','thread/fork') else {}
        def send(self,*args):pass
        def close(self):pass
    isolation.prepare(s,target,rpc_factory=Rpc)
    assert s['native_id']=='new-private-manager'
    assert not (target/'agentchat-imports').exists()
