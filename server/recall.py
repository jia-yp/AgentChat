"""Owner recall of an unclaimed message, serialized against bridge claim/steer."""
import time

QUEUED={'pending','round_waiting','bundled'}
NOTICE='消息已撤回'


def option(db,actor,message):
    if not actor or actor['kind']!='user' or message['sender']!=actor['id']+':'+message['room']:
        return None
    if message['recalled_at'] or message['message_kind']!='chat':return None
    deliveries=db.execute('SELECT * FROM deliveries WHERE message=?',(message['id'],)).fetchall()
    if not any(d['status'] in QUEUED for d in deliveries):return None
    if message['delivery_mode']!='normal' or db.execute('SELECT 1 FROM steers WHERE message=?',(message['id'],)).fetchone():
        return {'available':False,'reason':'消息已有 Steer 投递，不能整条撤回'}
    if any(d['attempts'] or d['status'] not in QUEUED|{'skipped','cancelled'} for d in deliveries):
        return {'available':False,'reason':'已有 session 开始执行或接收过这条消息，不能撤回'}
    for d in deliveries:
        if d['status']=='bundled':
            primary=db.execute('''SELECT d.status,d.attempts FROM sync_participants p JOIN deliveries d ON d.id=p.delivery
                WHERE p.root=? AND p.round=? AND p.member=?''',(d['sync_root'],d['sync_round'],d['member'])).fetchone()
            if not primary or primary['status']!='pending' or primary['attempts']:
                return {'available':False,'reason':'这条消息已随合并任务交给 session，不能撤回'}
    return {'available':True,'reason':'撤销本条消息的所有排队投递'}


def withdraw(store,actor,room_id,message_id):
    from .core import Problem
    from . import synchronous,reading
    if actor['kind']!='user':raise Problem('只有发送消息的用户可以撤回',403)
    with store.tx() as db:
        store.access(db,room_id,actor,manage=False)
        message=db.execute('SELECT * FROM messages WHERE room=? AND id=?',(room_id,message_id)).fetchone()
        if not message:raise Problem('消息不存在',404)
        if message['sender']!=actor['id']+':'+room_id or message['message_kind']!='chat':
            raise Problem('只能撤回自己发送的聊天消息',403)
        if message['recalled_at']:return store.message_dict(db,message,actor)
        offer=option(db,actor,message)
        if not offer or not offer['available']:
            raise Problem(offer['reason'] if offer else '消息已开始执行或不在排队中，不能撤回',409)
        # BEGIN IMMEDIATE also protects claim and queued-to-steer conversion.
        deliveries=db.execute('SELECT * FROM deliveries WHERE message=?',(message_id,)).fetchall()
        db.execute('UPDATE messages SET recalled_at=? WHERE id=?',(time.time(),message_id))
        db.execute("UPDATE deliveries SET status='cancelled',error=? WHERE message=? AND status IN ('pending','round_waiting','bundled')",(NOTICE,message_id))
        for d in deliveries:
            if not d['sync_root'] or d['status']!='pending':continue
            primary=db.execute('SELECT * FROM sync_participants WHERE delivery=?',(d['id'],)).fetchone()
            if not primary:continue
            # Removing a primary inbox item must preserve the other messages
            # bundled into the same invocation and its remaining turn quota.
            successor=db.execute("""SELECT d.id FROM deliveries d JOIN messages m ON m.id=d.message
                WHERE d.sync_root=? AND d.sync_round=? AND d.member=? AND d.status='bundled'
                AND m.recalled_at=0 ORDER BY d.created,d.id LIMIT 1""",(d['sync_root'],d['sync_round'],d['member'])).fetchone()
            if successor:
                db.execute("UPDATE deliveries SET status='pending' WHERE id=?",(successor['id'],))
                db.execute("UPDATE sync_participants SET delivery=?,status='pending',error='' WHERE delivery=?",(successor['id'],d['id']))
            else:db.execute("UPDATE sync_participants SET status='skipped',error=? WHERE delivery=?",(NOTICE,d['id']))
        synchronous.settle(db,room_id)
        reading.settle(store,db,room_id)
        return store.message_dict(db,db.execute('SELECT * FROM messages WHERE id=?',(message_id,)).fetchone(),actor)
