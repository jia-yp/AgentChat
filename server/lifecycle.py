"""Durable hand-off of a session out of (and back into) gateway ownership."""
import hmac
import json
import time

SCHEMA = '''CREATE TABLE IF NOT EXISTS session_transitions(
 id INTEGER PRIMARY KEY AUTOINCREMENT, member TEXT NOT NULL, bridge TEXT NOT NULL, room TEXT NOT NULL,
 action TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', immediate INTEGER NOT NULL DEFAULT 0,
 nonce TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(member,nonce));
CREATE UNIQUE INDEX IF NOT EXISTS transition_one_pending ON session_transitions(member) WHERE status='pending';
'''


def current(db, member):
    row = db.execute('SELECT * FROM session_transitions WHERE member=? ORDER BY id DESC LIMIT 1', (member,)).fetchone()
    return dict(row) if row else None


def view(row):
    if not row:
        return None
    return {**dict(row), 'state': 'cancelled' if row['action']=='cancel-leave' else ('left' if row['action'] == 'leave' else 'joined')
            if row['status'] == 'complete' else 'cancelled' if row['status'] == 'cancelled'
            else 'leaving' if row['action'] == 'leave' else 'joining'}


def request_in(store, db, room, member, action, nonce, immediate=False):
    from .core import Problem
    from . import synchronous, steering
    target = db.execute("SELECT * FROM members WHERE id=? AND room=? AND kind='agent'", (member, room)).fetchone()
    if not target:
        raise Problem('session 不存在', 404)
    old = db.execute('SELECT * FROM session_transitions WHERE member=? AND nonce=?', (member, nonce)).fetchone()
    if old:
        if old['action'] != action:
            raise Problem('此操作编号已用于其他操作', 409)
        if action == 'leave' and immediate and old['status'] == 'pending':
            db.execute('UPDATE session_transitions SET immediate=1 WHERE id=?', (old['id'],))
            db.execute("UPDATE deliveries SET status='cancelled',error='管理员要求立即退出托管' WHERE member=? AND status='running'", (member,))
            synchronous.settle(db, room);steering.settle(db)
            return view(current(db, member))
        return view(old)
    cap = db.execute('SELECT workspace_supported FROM bridge_projects WHERE bridge=?', (target['bridge'],)).fetchone()
    if not cap or not cap[0]:
        raise Problem('请先更新并重启此 workspace 的 gateway', 409)
    previous = current(db, member)
    if action == 'cancel-leave':
        if not previous or previous['status'] != 'pending' or previous['action'] != 'leave':
            raise Problem('没有等待中的退出操作', 409)
        db.execute("UPDATE session_transitions SET status='cancelled',updated=? WHERE id=?", (time.time(), previous['id']))
        db.execute('UPDATE members SET departing=0 WHERE id=?', (member,))
        now=time.time()
        db.execute('''INSERT INTO session_transitions(member,bridge,room,action,status,nonce,created,updated)
            VALUES(?,?,?,'cancel-leave','complete',?,?,?)''',(member,target['bridge'],room,nonce,now,now))
        return view(current(db, member))
    if previous and previous['status'] == 'pending':
        raise Problem('此 session 正在退出或重新加入，请等待本地确认', 409)
    if action == 'leave':
        if target['revoked']:
            raise Problem('此 session 已不在托管中', 409)
        db.execute('UPDATE members SET departing=1 WHERE id=?', (member,))
        statuses = ('pending', 'bundled', 'round_waiting', 'running') if immediate else ('pending', 'bundled', 'round_waiting')
        marks = ','.join('?' for _ in statuses)
        db.execute(f"UPDATE deliveries SET status='cancelled',error='session 退出托管，不再接收新任务' WHERE member=? AND status IN ({marks})", (member, *statuses))
    elif action == 'rejoin':
        if not target['revoked'] or not target['deleted_at']:
            raise Problem('只能重新加入已退出或已删除的 session', 409)
        bridge = db.execute('SELECT revoked FROM members WHERE id=?', (target['bridge'],)).fetchone()
        if not bridge or bridge[0]:
            raise Problem('原 gateway 已撤销，不能重新加入', 409)
        if target['session_type'] == 'manager' and db.execute("SELECT 1 FROM members WHERE bridge=? AND session_type='manager' AND revoked=0", (target['bridge'],)).fetchone():
            raise Problem('此 workspace 已有 manager', 409)
    else:
        raise Problem('未知的 session 生命周期操作')
    now = time.time()
    db.execute('INSERT INTO session_transitions(member,bridge,room,action,immediate,nonce,created,updated) VALUES(?,?,?,?,?,?,?,?)',
               (member, target['bridge'], room, action, int(immediate), nonce, now, now))
    synchronous.settle(db, room);steering.settle(db)
    return view(current(db, member))


def request(store, room, member, actor, action, nonce, immediate=False):
    from .core import Problem
    if actor['kind'] != 'user':
        raise Problem('请由创建者操作，或通过 manager 的专用工具处理', 403)
    with store.tx() as db:
        store.access(db, room, actor, manage=True)
        return request_in(store, db, room, member, action, nonce, immediate)


def states(db, bridge):
    return [view(row) for row in db.execute('''SELECT t.* FROM session_transitions t WHERE t.bridge=?
        AND t.id=(SELECT MAX(x.id) FROM session_transitions x WHERE x.member=t.member)''', (bridge,))]


def acknowledge(store, actor, operation, credential=''):
    from .core import Problem, digest
    from . import synchronous, steering
    with store.tx() as db:
        row = db.execute('SELECT * FROM session_transitions WHERE id=?', (operation,)).fetchone()
        if not row or actor['kind'] != 'bridge' or row['bridge'] != actor['id']:
            raise Problem('无权确认此退出或加入操作', 403)
        if current(db, row['member'])['id'] != operation or row['status'] == 'cancelled':
            raise Problem('此操作已被后续操作替代', 409)
        target = db.execute('SELECT * FROM members WHERE id=?', (row['member'],)).fetchone()
        if row['status'] == 'complete':
            if row['action'] == 'rejoin' and not hmac.compare_digest(target['token_hash'] or '', digest(credential)):
                raise Problem('重新加入的凭证不匹配', 409)
            return view(row)
        now = time.time()
        if row['action'] == 'leave':
            if db.execute("SELECT 1 FROM deliveries WHERE member=? AND status='running' AND lease_until>=?", (target['id'], now)).fetchone():
                raise Problem('当前任务尚未结束，不能确认退出', 409)
            db.execute("UPDATE deliveries SET status='cancelled',error='session 已退出托管' WHERE member=? AND status IN ('pending','running','bundled','round_waiting')", (target['id'],))
            db.execute("UPDATE members SET departing=0,revoked=1,token_hash=NULL,enabled=0,paused=1,status='left',disabled_reason='left',deleted_at=? WHERE id=?", (now, target['id']))
        else:
            if not 32 <= len(credential) <= 128:
                raise Problem('重新加入必须使用新的 session 凭证')
            bridge = db.execute('SELECT revoked FROM members WHERE id=?', (actor['id'],)).fetchone()
            if not bridge or bridge[0]:raise Problem('gateway 已撤销', 409)
            if target['session_type']=='manager' and db.execute("SELECT 1 FROM members WHERE bridge=? AND session_type='manager' AND revoked=0 AND id!=?", (actor['id'],target['id'])).fetchone():
                raise Problem('此 workspace 已有 manager',409)
            db.execute("UPDATE members SET token_hash=?,revoked=0,departing=0,enabled=0,paused=0,status='dormant',disabled_reason='',deleted_at=0,last_seen=0,detail='' WHERE id=?", (digest(credential), target['id']))
        db.execute("UPDATE session_transitions SET status='complete',updated=? WHERE id=?", (now, operation))
        synchronous.settle(db, row['room']);steering.settle(db)
        manager = db.execute("SELECT id FROM members WHERE bridge=? AND session_type='manager' AND revoked=0", (actor['id'],)).fetchone()
        if manager:
            owner = db.execute("SELECT id,name FROM members WHERE room=? AND id=(SELECT owner||':'||id FROM rooms WHERE id=?)", (row['room'],row['room'])).fetchone()
            text = '已退出托管；历史、文件和原生会话保留。' if row['action']=='leave' else '已重新加入，等待首次点名；旧任务不会重放。'
            db.execute('''INSERT OR IGNORE INTO messages(room,sender,body,targets,nonce,created,private_to,max_rounds,message_kind)
                VALUES(?,?,?,?,?,?,?,0,'session_lifecycle_result')''',
                       (row['room'],owner['id'],f"@{owner['name']} {target['name']} {text}",json.dumps([owner['name']]),'session-transition:'+str(operation),now,manager['id']))
        return view(current(db, target['id']))
