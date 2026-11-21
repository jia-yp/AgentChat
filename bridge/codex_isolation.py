"""Independent Codex storage, with read-only snapshots and native history forks.

Never share a rollout, SQLite store or writer lock with the Desktop. Original
sessions remain untouched. Authentication is an independent Codex login, not a
copy of a rotating Desktop refresh token.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from contextlib import contextmanager

from .common import ProcessLock, read_json, write_json
from .harness_environment import harness_environment


def default_home():
    base = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local/share'))
    return base / 'AgentChat' / 'codex-home'


def configured_home(config):
    if config.get('codex_home'):
        return str(Path(config['codex_home']).expanduser().resolve())
    folder = default_home()
    if (folder / 'agentchat-isolation.json').exists() and (folder / 'auth.json').exists():
        return str(folder.resolve())
    return None


def environment(session, source=None):
    env = harness_environment(source)
    if session.get('codex_home'):
        folder = str(Path(session['codex_home']).resolve())
        env['CODEX_HOME'] = folder
        env['CODEX_SQLITE_HOME'] = folder
    return env


def initialize_home(target, source):
    target, source = Path(target).resolve(), Path(source).resolve()
    if target == source:
        raise ValueError('群聊 Codex 存储必须与桌面 CODEX_HOME 分开')
    target.mkdir(parents=True, exist_ok=True)
    marker = target / 'agentchat-isolation.json'
    if not marker.exists():
        # Preserve the user's model/provider/tool configuration, while all
        # mutable native state belongs to this independent directory.
        config = source / 'config.toml'
        content = config.read_text(encoding='utf-8') if config.exists() else ''
        lines, section = [], False
        for line in content.splitlines():
            if line.lstrip().startswith('['):section = True
            if not section and line.split('=', 1)[0].strip() in ('sqlite_home', 'log_dir'):
                continue
            lines.append(line)
        prefix = 'sqlite_home = ' + json.dumps(str(target)) + '\nlog_dir = ' + json.dumps(str(target / 'log')) + '\n'
        (target / 'config.toml').write_text(prefix + '\n'.join(lines) + '\n', encoding='utf-8')
        write_json(marker, {'source_home': str(source), 'created': time.time(), 'auth': 'independent-login'})
    return target


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


@contextmanager
def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('BEGIN')
    try:yield db
    finally:db.close()


def schema(source, target):
    for row in source.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type='index'"):
        sql = row[0]
        if sql.startswith('CREATE TABLE '):sql = sql.replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ', 1)
        elif sql.startswith('CREATE UNIQUE INDEX '):sql = sql.replace('CREATE UNIQUE INDEX ', 'CREATE UNIQUE INDEX IF NOT EXISTS ', 1)
        elif sql.startswith('CREATE INDEX '):sql = sql.replace('CREATE INDEX ', 'CREATE INDEX IF NOT EXISTS ', 1)
        else:continue
        target.execute(sql)


def insert_rows(target, table, rows, changes=None, replace=False):
    for row in rows:
        values = dict(row)
        if changes:values.update(changes)
        columns = list(values)
        target.execute(('INSERT OR REPLACE INTO ' if replace else 'INSERT OR IGNORE INTO ') + identifier(table) + '(' + ','.join(map(identifier, columns)) +
                       ') VALUES (' + ','.join('?' for _ in columns) + ')', [values[k] for k in columns])


def snapshot_thread(source_home, target_home, native, ancestry=()):
    source_home, target_home = Path(source_home).resolve(), Path(target_home).resolve()
    if source_home == target_home:
        raise ValueError('不能在原始 Codex 存储中进行迁移')
    if native in ancestry:raise ValueError('原生会话的父历史存在循环，迁移已停止')
    receipt = target_home / 'agentchat-imports' / (native + '.json')
    if receipt.exists() and read_json(receipt).get('snapshot_version')==2:
        result = read_json(receipt)
        copied = Path(result['copied_rollout'])
        if result['source_home'] != str(source_home) or not copied.is_relative_to(target_home):
            raise ValueError('已有迁移快照来自不同 Codex 存储')
        if hashlib.sha256(copied.read_bytes()).hexdigest() != result['rollout_sha256']:
            raise ValueError('已有迁移快照被修改，原会话未改动')
        return result
    state_files = sorted(source_home.glob('state_*.sqlite'))
    if not state_files:
        raise ValueError('未找到原生 Codex 会话索引；原会话未改动')
    state_path = max(state_files, key=lambda p: int(p.stem.split('_')[-1]))
    with readonly(state_path) as source, sqlite3.connect(target_home / state_path.name) as target:
        row = source.execute('SELECT * FROM threads WHERE id=?', (native,)).fetchone()
        if not row:
            raise ValueError('未找到指定原生 Codex 会话；不会猜测最近会话或清空历史')
        source_rollout = Path(row['rollout_path']).resolve()
        # Windows extended paths resolve to the same canonical spelling.
        try:relative = source_rollout.relative_to(source_home)
        except ValueError:
            clean = Path(str(source_rollout).removeprefix('\\\\?\\')).resolve()
            relative = clean.relative_to(Path(str(source_home).removeprefix('\\\\?\\')).resolve())
        if relative.parts[0] not in ('sessions', 'archived_sessions'):
            raise ValueError('会话记录不在指定 Codex 存储内')
        # New Codex stores the display/fork history in a paginated SQLite
        # projection. A rollout-only import appears empty to thread/read.
        history_files=list(source_home.glob('thread_history_*.sqlite'))
        history_rows={};history_path=None;history_sql=[]
        if history_files:
            history_path=max(history_files,key=lambda p:int(p.stem.split('_')[-1]))
            with readonly(history_path) as history:
                history_sql=[r[0] for r in history.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type='index'")]
                for table in ('_sqlx_migrations','thread_turns','thread_items','thread_history_projection_state','thread_realtime_items'):
                    exists=history.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'",(table,)).fetchone()
                    if exists:
                        query='SELECT * FROM '+identifier(table)
                        history_rows[table]=[dict(r) for r in history.execute(query+(' WHERE thread_id=?' if table!='_sqlx_migrations' else ''),
                                                              (native,) if table!='_sqlx_migrations' else ())]
        if 'history_mode' in row.keys() and row['history_mode']=='paginated' and not history_path:
            raise ValueError('原生分页历史索引缺失；不会创建空历史的替代会话')
        target_rollout = target_home / relative
        target_rollout.parent.mkdir(parents=True, exist_ok=True)
        # Capture only complete JSONL records. Native history hydration catches
        # up from this immutable prefix; the original writer is never touched.
        with source_rollout.open('rb') as stream:
            raw = stream.read(os.fstat(stream.fileno()).st_size)
        raw = raw[:raw.rfind(b'\n') + 1]
        if not raw:raise ValueError('原生会话记录尚未落盘，请稍后继续')
        metadata = json.loads(raw.splitlines()[0]).get('payload', {})
        if metadata.get('id') != native:
            raise ValueError('原生会话记录与指定 ID 不匹配')
        base=metadata.get('history_base') or {}
        if base.get('thread_id'):
            parent=snapshot_thread(source_home,target_home,base['thread_id'],(*ancestry,native))
            if parent['rollout_bytes']<base.get('end_byte_offset',0):
                raise ValueError('已导入的父会话历史早于此分叉点，迁移已停止；原会话未改动')
        if ('history_mode' in row.keys() and row['history_mode']=='paginated' and row['has_user_event']
            and not history_rows.get('thread_turns') and not base.get('thread_id')):
            raise ValueError('原生分页历史尚不可读；不会创建空历史的替代会话')
        for projection in history_rows.get('thread_history_projection_state',[]):
            if projection['next_rollout_byte_offset']>len(raw):
                raise ValueError('会话日志尚未完整落盘；原会话未改动，请稍后继续迁移')
        temporary = target_rollout.with_suffix('.importing')
        temporary.write_bytes(raw)
        temporary.replace(target_rollout)
        schema(source, target)
        tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        insert_rows(target, '_sqlx_migrations', source.execute('SELECT * FROM _sqlx_migrations'))
        project = row['project_id'] if 'project_id' in row.keys() else None
        if project and 'projects' in tables:
            insert_rows(target, 'projects', source.execute('SELECT * FROM projects WHERE id=?', (project,)))
            if 'project_roots' in tables:
                insert_rows(target, 'project_roots', source.execute('SELECT * FROM project_roots WHERE project_id=?', (project,)))
        section = row['thread_section_id'] if 'thread_section_id' in row.keys() else None
        if section and 'thread_sections' in tables:
            insert_rows(target, 'thread_sections', source.execute('SELECT * FROM thread_sections WHERE id=?', (section,)))
        insert_rows(target, 'threads', [row], {'rollout_path': str(target_rollout)})
        for table in ('thread_dynamic_tools', 'thread_attachments', 'thread_read_receipts'):
            if table in tables:
                insert_rows(target, table, source.execute('SELECT * FROM ' + identifier(table) + ' WHERE thread_id=?', (native,)))
        if history_path:
            with sqlite3.connect(target_home/history_path.name) as target_history:
                for sql in history_sql:
                    if sql.startswith('CREATE TABLE '):sql=sql.replace('CREATE TABLE ','CREATE TABLE IF NOT EXISTS ',1)
                    elif sql.startswith('CREATE UNIQUE INDEX '):sql=sql.replace('CREATE UNIQUE INDEX ','CREATE UNIQUE INDEX IF NOT EXISTS ',1)
                    elif sql.startswith('CREATE INDEX '):sql=sql.replace('CREATE INDEX ','CREATE INDEX IF NOT EXISTS ',1)
                    else:continue
                    target_history.execute(sql)
                for table,rows in history_rows.items():insert_rows(target_history,table,rows,replace=table!='_sqlx_migrations')
    result = {'source_native_id': native, 'source_home': str(source_home), 'rollout_bytes': len(raw),
              'rollout_sha256': hashlib.sha256(raw).hexdigest(), 'copied_rollout': str(target_rollout),'snapshot_version':2,
              'history_counts':{table:len(rows) for table,rows in history_rows.items() if table!='_sqlx_migrations'},
              'creator_account_id':metadata.get('creator_account_id')}
    write_json(receipt, result)
    return result


@contextmanager
def migration_lock(home, cancelled):
    # First mentions can arrive concurrently across rooms and processes. Wait
    # for the short metadata migration, without failing another delivery.
    lock = ProcessLock(home / 'agentchat-migration.lock')
    while True:
        if cancelled():raise RuntimeError('Codex 会话迁移已取消；原会话未改动')
        try:
            lock.__enter__()
            break
        except RuntimeError:
            time.sleep(.1)
    try:yield
    finally:lock.__exit__()


def prepare(session, home, rpc_factory=None, cancelled=lambda: False):
    """Fork once in isolated storage, without inference or touching the Desktop."""
    if session['definition']['harness'] != 'codex':return
    home = Path(home).resolve()
    source = Path(session.get('codex_source_home') or os.environ.get('CODEX_HOME') or Path.home() / '.codex').resolve()
    marker=home/'agentchat-isolation.json'
    already_private=(home==source and marker.exists() and Path(read_json(marker)['source_home']).resolve()!=home)
    if not already_private:initialize_home(home, source)
    if not (home / 'auth.json').exists():
        raise ValueError(f'群聊 Codex 需要在独立目录登录一次：{home}；请设置 CODEX_HOME 后运行 codex login。原桌面登录与会话未改动。')
    folder = Path(session['context']).parent
    journal = folder / 'codex-isolation.json'
    with migration_lock(home, cancelled):
        result = read_json(journal) if journal.exists() else None
        if result and result.get('home') != str(home):
            raise ValueError('已有 Codex 迁移记录指向不同目录，请保留原配置')
        if not result:
            native = session.get('native_id', '')
            result = {'home': str(home), 'source_home': str(source), 'source_native_id': native}
            if native:
                if source!=home:result.update(snapshot_thread(source, home, native))
                account=(read_json(home/'auth.json').get('tokens') or {}).get('account_id')
                if result.get('creator_account_id') and account and account!=result['creator_account_id']:
                    raise ValueError('独立 Codex 登录的账户与原会话不同，请使用同一账户登录；原会话未改动')
                from .adapters import discover
                from .codex_live import Rpc
                from .permission_settings import codex_policy
                executable = session['definition'].get('executable') or discover()['codex']['executable']
                probe_session = {**session, 'codex_home': str(home)}
                logdir = folder / 'logs';logdir.mkdir(parents=True, exist_ok=True)
                rpc = (rpc_factory or Rpc)([executable, 'app-server', '--stdio'], Path(session['workspace']),
                    environment(probe_session), logdir / 'codex-isolation.stdout.log', logdir / 'codex-isolation.stderr.log')
                try:
                    rpc.wait(rpc.request('initialize', {'clientInfo': {'name': 'agentchat-isolation', 'version': '1.0'}}), cancelled)
                    rpc.send({'method': 'initialized'})
                    original=rpc.wait(rpc.request('thread/read',{'threadId':native,'includeTurns':True}),cancelled)['thread']
                    if original['id']!=native:raise ValueError('Codex 返回了不同的来源会话')
                    original_turns=original.get('turns',[])
                    result['expected_history_counts']={'thread_turns':len(original_turns),
                        'thread_items':sum(len(t.get('items',[])) for t in original_turns)}
                    sandbox, _ = codex_policy(session['definition'])
                    params = {'threadId': native, 'cwd': session['workspace'], 'excludeTurns': True,
                              'approvalPolicy': 'never', 'sandbox': sandbox}
                    if session['definition'].get('model'):params['model'] = session['definition']['model']
                    fork = rpc.wait(rpc.request('thread/fork', params), cancelled)
                    result['native_id'] = fork['thread']['id']
                    if result['native_id'] == native:raise ValueError('Codex 未建立独立会话')
                    copied=rpc.wait(rpc.request('thread/read',{'threadId':result['native_id'],'includeTurns':True}),cancelled)['thread']
                    counts=result['expected_history_counts']
                    turns=copied.get('turns',[])
                    if (copied['id']!=result['native_id'] or len(turns)<counts.get('thread_turns',0)
                        or sum(len(t.get('items',[])) for t in turns)<counts.get('thread_items',0)):
                        raise ValueError('独立会话的历史核验未通过；群聊绑定和桌面原会话未改动')
                    result['verified_history']=True
                finally:rpc.close()
            else:result['native_id'] = ''
            result['created'] = time.time()
            write_json(journal, result)
        # A crash after the native fork can resume from this durable record.
        if session.get('native_id') in ('', result['source_native_id']):
            session['native_id'] = result['native_id']
            cursor = folder / 'chat-context/cursor.json'
            if cursor.exists():
                state = read_json(cursor)
                if state.get('native_id') == result['source_native_id']:
                    state['native_id'] = result['native_id'];write_json(cursor, state)
            session.pop('native_named', None)
        session['codex_home'] = str(home)
        session['codex_source_home'] = str(source)
        write_json(session['context'], session)
    return result


def login(home=None):
    from .adapters import discover
    from .processes import background_options
    target=initialize_home(home or default_home(),os.environ.get('CODEX_HOME') or Path.home()/'.codex')
    executable=discover()['codex']['executable']
    if not executable:raise ValueError('未找到 Codex CLI')
    subprocess.run([executable,'login'],env=environment({'codex_home':str(target)}),check=True,
                   **background_options(new_session=False))
    print('群聊 Codex 独立登录完成：'+str(target))


def migrate_bridge(path, home=None):
    path=Path(path).resolve()
    # The lifetime lock makes this safe against a restart racing migration.
    with ProcessLock(path.with_suffix('.lock')), ProcessLock(path.with_suffix('.sessions.lock')):
        config=read_json(path)
        target=str(Path(home or config.get('codex_home') or default_home()).resolve())
        results=[]
        for entry in config['sessions']:
            session=read_json(entry['context'])
            if session['definition']['harness']!='codex' or session.get('revoked'):continue
            result=prepare(session,target)
            results.append({'name':session['name'],'source_native_id':result['source_native_id'],'native_id':session['native_id']})
        config['codex_home']=target;write_json(path,config)
    return {'home':target,'sessions':results}
