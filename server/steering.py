"""Durable, at-most-once attempts to add notes to an exact running task.

An injected receipt confirms the harness accepted input, not that the model read it.
No path here creates a new task or redirects a note to a later attempt.
"""
import secrets
import time

from . import uploads


TABLE = '''
CREATE TABLE {table}(
 id TEXT PRIMARY KEY, message INTEGER NOT NULL, member TEXT NOT NULL,
 delivery TEXT NOT NULL, attempt INTEGER NOT NULL, lease TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '',
 claim_nonce TEXT NOT NULL DEFAULT '', claimed REAL, created REAL NOT NULL,
 source_delivery TEXT UNIQUE, requested_by TEXT NOT NULL DEFAULT '',
 UNIQUE(message,member));
'''


def initialize(db):
    """Keep existing receipts while allowing separate recipients of one message."""
    if not db.in_transaction:db.execute('BEGIN IMMEDIATE')
    columns={r['name'] for r in db.execute('PRAGMA table_info(steers)')}
    if not columns:
        db.execute(TABLE.format(table='steers'))
    elif 'source_delivery' not in columns:
        db.execute(TABLE.format(table='steers_next'))
        fields='id,message,member,delivery,attempt,lease,status,error,claim_nonce,claimed,created'
        db.execute(f'INSERT INTO steers_next({fields}) SELECT {fields} FROM steers')
        db.execute('DROP TABLE steers')
        db.execute('ALTER TABLE steers_next RENAME TO steers')
    db.execute('CREATE INDEX IF NOT EXISTS steers_delivery ON steers(delivery,attempt,status)')


def settle(db):
    # Never assume an unacknowledged RPC was not accepted. A lost acknowledgment
    # is visibly uncertain and must not be silently resent.
    db.execute("""UPDATE steers SET status=CASE WHEN status='pending' THEN 'rejected' ELSE 'unknown' END,
        error=CASE WHEN status='pending' THEN '目标任务已结束、停止或租约失效；未转入下一任务'
        ELSE '注入结果未确认；不会自动重发' END
        WHERE status IN ('pending','injecting') AND (
          (status='injecting' AND claimed<?) OR NOT EXISTS (
            SELECT 1 FROM deliveries d JOIN members m ON m.id=d.member JOIN rooms r ON r.id=d.room
            WHERE d.id=steers.delivery AND d.attempts=steers.attempt AND d.lease=steers.lease
            AND d.status='running' AND d.lease_until>=? AND m.enabled=1 AND m.paused=0
            AND m.revoked=0 AND r.paused=0))""", (time.time()-45,time.time()))


def validate(db, room, actor, targets, body, task):
    from .core import Problem, visible_message
    if len(targets)!=1 or targets[0]['kind']!='agent' or targets[0]['id']==actor['id']:
        raise Problem('Steer 需要单独指定一个正在工作的 agent')
    if len(body)>4000:raise Problem('Steer 补充信息最多 4000 字')
    target=targets[0]
    if room['paused'] or target['paused'] or not target['enabled'] or target['revoked'] or target.get('departing'):
        raise Problem('目标 session 尚未启用、已关闭或群聊已暂停；Steer 不会激活它',409)
    if not target['steer_supported'] or time.time()-target['last_seen']>45:
        raise Problem('目标 harness 不支持运行中 Steer，或 bridge 已离线',409)
    parts=(task or '').split(':')
    if len(parts)!=2 or not parts[1].isdigit():raise Problem('Steer 必须指定 members 中的 active_task.ref')
    delivery=db.execute('SELECT * FROM deliveries WHERE id=? AND member=? AND room=?',
                        (parts[0],target['id'],room['id'])).fetchone()
    if not delivery or delivery['attempts']!=int(parts[1]) or delivery['status']!='running' or delivery['lease_until']<time.time():
        raise Problem('绑定的目标任务已结束或已被重试；未转入下一任务',409)
    message=db.execute('SELECT * FROM messages WHERE id=?',(delivery['message'],)).fetchone()
    root=db.execute('SELECT stopped FROM messages WHERE id=?',(message['root'] or message['id'],)).fetchone()
    if not visible_message(actor,message) or root['stopped']:
        raise Problem('目标任务不可见或本轮协作已停止',403)
    stats=db.execute('''SELECT COUNT(*) AS count,COALESCE(SUM(LENGTH(m.body)),0) AS size,
        MAX(CASE WHEN m.sender=? OR s.requested_by=? THEN s.created ELSE 0 END) AS last
        FROM steers s JOIN messages m ON m.id=s.message WHERE s.delivery=? AND s.attempt=?''',
        (actor['id']+':'+room['id'] if actor['kind']=='user' else actor['id'],actor['id'],delivery['id'],delivery['attempts'])).fetchone()
    if stats['count']>=8 or stats['size']+len(body)>16000:raise Problem('本次任务的 Steer 额度已用完（最多 8 条、合计 16000 字）',429)
    if stats['last'] and time.time()-stats['last']<2:raise Problem('请合并补充信息；对同一任务的 Steer 至少间隔 2 秒',429)
    return delivery


def record(db, message, delivery, source_delivery=None, requested_by=''):
    db.execute('INSERT INTO steers(id,message,member,delivery,attempt,lease,created,source_delivery,requested_by) VALUES(?,?,?,?,?,?,?,?,?)',
        (secrets.token_hex(12),message,delivery['member'],delivery['id'],delivery['attempts'],delivery['lease'],time.time(),source_delivery,requested_by))


def receipts(db, message):
    return [dict(r) for r in db.execute('''SELECT s.id,m.name,s.status,s.error,
        s.delivery || ':' || s.attempt AS task_ref,d.message AS task_message,s.source_delivery
        FROM steers s JOIN members m ON m.id=s.member JOIN deliveries d ON d.id=s.delivery
        WHERE s.message=?''',(message,))]


def can_convert(actor, message):
    from .guests import owner
    if message['message_kind']=='session_change_request':return False
    return actor and (owner(actor,message['room']) or (actor['kind']=='user' and actor.get('_room_role')=='participant' and message['sender']==actor['id']+':'+message['room']) or (actor['kind']=='agent' and actor['id']==message['sender']))


def offer(db, actor, message, delivery):
    """Expose only a concrete, current conversion option; never guess a turn."""
    from .core import Problem
    if delivery['status'] not in ('pending','round_waiting') or not can_convert(actor,message):return None
    target=dict(db.execute('SELECT * FROM members WHERE id=?',(delivery['member'],)).fetchone())
    if not target['uploads_supported'] and uploads.metadata(db,message['id']):
        return {'reason':'请先更新并重启目标 session 的 Bridge，再注入附件','task_ref':None}
    active=db.execute("SELECT id,attempts,message FROM deliveries WHERE member=? AND status='running' AND lease_until>=?",(target['id'],time.time())).fetchone()
    if not active:return {'reason':'目标目前没有正在运行的任务','task_ref':None}
    ref=active['id']+':'+str(active['attempts'])
    try:
        room=dict(db.execute('SELECT * FROM rooms WHERE id=?',(message['room'],)).fetchone())
        validate(db,room,actor,[target],message['body'],ref)
    except Problem as exc:return {'reason':exc.message,'task_ref':None}
    return {'reason':'','task_ref':ref,'task_message':active['message']}


def convert(store, actor, room_id, delivery_id, task_ref):
    from .core import Problem, visible_message
    with store.tx() as db:
        room=store.access(db,room_id,actor,manage=False)
        settle(db)
        queued=db.execute('SELECT * FROM deliveries WHERE id=? AND room=?',(delivery_id,room_id)).fetchone()
        if not queued:raise Problem('排队任务不存在',404)
        message=db.execute('SELECT * FROM messages WHERE id=?',(queued['message'],)).fetchone()
        if not visible_message(actor,message) or not can_convert(actor,message):
            raise Problem('只有管理员或原发送者可以把这条排队消息转为 Steer',403)
        if actor['kind']=='agent':
            sender=store.member(db,actor['id'])
            if sender['paused'] or not sender['enabled'] or room['paused']:raise Problem('此 session 尚未启用或已关闭',403)
        existing=db.execute('SELECT * FROM steers WHERE source_delivery=?',(delivery_id,)).fetchone()
        if existing:
            if existing['delivery']+':'+str(existing['attempt'])!=task_ref:raise Problem('这条消息已经绑定其他任务，未重复注入',409)
            return store.message_dict(db,message,actor)
        if queued['status'] not in ('pending','round_waiting'):raise Problem('这条消息已开始执行或已结束，不能转为 Steer',409)
        root=db.execute('SELECT stopped FROM messages WHERE id=?',(message['root'] or message['id'],)).fetchone()
        if root['stopped']:raise Problem('这条消息所属协作已停止',409)
        target=store.member(db,queued['member'])
        if not target['uploads_supported'] and uploads.metadata(db,message['id']):
            raise Problem('请先更新并重启目标 session 的 Bridge，再注入附件',409)
        active=validate(db,room,actor,[target],message['body'],task_ref)
        # The same write transaction arbitrates against claim(). A converted
        # delivery is never left in the normal queue, even if injection fails.
        record(db,message['id'],active,source_delivery=delivery_id,requested_by=actor['id'])
        db.execute("UPDATE deliveries SET status='steered',error='' WHERE id=?",(delivery_id,))
        return store.message_dict(db,message,actor)


def claim(store, actor, delivery_id, lease, nonce):
    from .core import Problem
    with store.tx() as db:
        d=store.delivery(db,actor,delivery_id,lease)
        settle(db)
        if d['status']!='running' or d['lease_until']<time.time():raise Problem('目标任务已结束',409)
        # A retry with the same nonce recovers a lost HTTP response. A new claim
        # never returns notes already handed to another bridge invocation.
        db.execute("UPDATE steers SET status='injecting',claim_nonce=?,claimed=? WHERE delivery=? AND lease=? AND status='pending'",
                   (nonce,time.time(),delivery_id,d['lease']))
        rows=db.execute("""SELECT s.id,m.id AS message,m.body,m.private_to,u.name AS sender,u.kind AS sender_kind
            FROM steers s JOIN messages m ON m.id=s.message JOIN members u ON u.id=m.sender
            WHERE s.delivery=? AND s.lease=? AND s.claim_nonce=? AND s.status='injecting' ORDER BY s.created""",
            (delivery_id,d['lease'],nonce)).fetchall()
        notes=[]
        for row in rows:
            note=dict(row);private_to=note.pop('private_to')
            if files:=uploads.metadata(db,note['message']):
                note.update(attachments=files,private_to=private_to)
            notes.append(note)
        return {'notes':notes}


def acknowledge(store, actor, delivery_id, lease, nonce, ids, status, error=''):
    from .core import Problem
    if status not in ('injected','rejected','unknown'):raise Problem('无效的 Steer 回执')
    with store.tx() as db:
        d=store.delivery(db,actor,delivery_id,lease)
        for sid in ids:
            row=db.execute('SELECT * FROM steers WHERE id=? AND delivery=? AND lease=? AND claim_nonce=?',(sid,delivery_id,d['lease'],nonce)).fetchone()
            if not row:raise Problem('无效的 Steer 投递',403)
            if row['status'] in ('injected','rejected'):
                if row['status']!=status:raise Problem('回执与已有结果冲突',409)
                continue
            db.execute('UPDATE steers SET status=?,error=? WHERE id=?',(status,error[:600],sid))
    return {'ok':True}
