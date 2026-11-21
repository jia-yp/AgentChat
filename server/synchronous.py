"""Persisted group rounds: frozen history, bundled inboxes, one task per member.

All functions run inside the caller's SQLite write transaction. A public room
has at most one open synchronous conversation; private managers stay independent.
"""
import json
import time
from . import recovery

SCHEMA='''
CREATE TABLE IF NOT EXISTS sync_runs(
 root INTEGER PRIMARY KEY,room TEXT NOT NULL,round INTEGER NOT NULL DEFAULT 1,
 maximum INTEGER NOT NULL,allowances TEXT NOT NULL,cutoff INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'running',reason TEXT NOT NULL DEFAULT '',created REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS sync_one_open ON sync_runs(room) WHERE status IN ('running','blocked');
CREATE TABLE IF NOT EXISTS sync_participants(
 root INTEGER NOT NULL,round INTEGER NOT NULL,member TEXT NOT NULL,delivery TEXT NOT NULL UNIQUE,
 quota INTEGER NOT NULL,used INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'pending',
 error TEXT NOT NULL DEFAULT '',PRIMARY KEY(root,round,member));
CREATE INDEX IF NOT EXISTS sync_room ON sync_runs(room,status);
'''


def active(db,room):
    row=db.execute("SELECT * FROM sync_runs WHERE room=? AND status IN ('running','blocked')",(room,)).fetchone()
    return dict(row) if row else None


def validate_allowances(db,room,values):
    from .core import Problem
    if not isinstance(values,dict) or len(values)>100:raise Problem('无效的每轮发言额度')
    members={r['id'] for r in db.execute("SELECT id FROM members WHERE room=? AND kind='agent' AND session_type!='manager' AND revoked=0",(room,))}
    if any(k not in members or type(v)!=int or not 1<=v<=100 for k,v in values.items()):
        raise Problem('每轮发言次数需为 1–100，且属于本群 worker；未设置时为 1')
    return values


def gate(db,actor):
    if actor['kind']!='agent' or actor.get('session_type')=='manager':return None
    return active(db,actor['room'])


def readable(db,actor,message):
    run=gate(db,actor)
    if not run or message['id']<=run['cutoff'] or message['sender']==actor['id']:return True
    return bool(db.execute('SELECT 1 FROM steers WHERE message=? AND member=?',(message['id'],actor['id'])).fetchone())


def prepare(db,room,actor,parent,mode,maximum,allowances,private_to,delivery_mode,targets):
    """Resolve a message's round before writing; never silently reset its budget."""
    from .core import Problem
    if private_to:return {'mode':'global' if mode=='synchronous' else mode,'root':None,'round':0}
    if delivery_mode=='steer':return {'mode':mode,'root':None,'round':0}
    run=active(db,room['id'])
    if actor['kind']=='user' and run:
        if mode!='synchronous' or maximum!=run['maximum'] or allowances!=json.loads(run['allowances']):
            raise Problem('当前同步对话的预算已固定；请使用当前预算补充，或结束后开始新对话',409)
        if run['maximum'] and run['round']>=run['maximum']:
            raise Problem('当前同步对话正在最后一轮收尾；请结束后发起新任务，或用 Steer 补充',409)
        return {'mode':'synchronous','root':run['root'],'round':run['round']}
    if actor['kind']=='agent' and parent and parent['round_mode']=='synchronous':
        root=parent['root'] or parent['id']
        participant=db.execute("""SELECT p.* FROM sync_participants p JOIN deliveries d ON d.id=p.delivery
            WHERE p.root=? AND p.member=? AND p.delivery IN (SELECT id FROM deliveries WHERE message=?)
            AND p.status='running' AND d.status='running' AND d.lease_until>=?""",(root,actor['id'],parent['id'],time.time())).fetchone()
        if not run or run['root']!=root or not participant or participant['round']!=run['round']:
            raise Problem('此同步轮次已结束或任务不再运行；不能继续发言',409)
        if participant['used']>=participant['quota']:raise Problem('本轮正式发言额度已用完；请直接结束本次任务',409)
        return {'mode':'synchronous','root':root,'round':run['round']}
    if mode=='synchronous':
        if actor['kind']!='user':raise Problem('只有管理员能发起同步对话',403)
        if db.execute("SELECT 1 FROM deliveries d JOIN messages m ON m.id=d.message WHERE d.room=? AND d.status IN ('pending','running') AND m.private_to=''",(room['id'],)).fetchone():
            raise Problem('请等待当前公开任务结束，或停止所有后再启动同步对话',409)
        unavailable=[m['name'] for m in targets if m['kind']=='agent' and not m['paused'] and not m['sync_supported']]
        if unavailable:raise Problem('请先更新并重启这些 session 的 bridge：'+', '.join(unavailable),409)
        return {'mode':mode,'root':None,'round':0}
    if run and actor['kind']=='agent':raise Problem('同步对话进行中，旧的异步任务不能追加公开消息',409)
    return {'mode':mode,'root':None,'round':0}


def enroll(db,run,number):
    """Coalesce mentions into a single native invocation per recipient."""
    rows=db.execute("SELECT d.*,m.paused,m.revoked,m.enabled,m.sync_supported,b.paused AS bridge_paused FROM deliveries d JOIN members m ON m.id=d.member LEFT JOIN members b ON b.id=m.bridge WHERE d.sync_root=? AND d.sync_round=? AND d.status='round_waiting' ORDER BY d.created,d.id",(run['root'],number)).fetchall()
    groups={}
    for row in rows:
        if row['paused'] or row['revoked'] or not row['enabled'] or row['bridge_paused']:
            db.execute("UPDATE deliveries SET status='skipped',error='session 或 bridge 已关闭；未加入下一轮' WHERE id=?",(row['id'],));continue
        groups.setdefault(row['member'],[]).append(row)
    allowances=json.loads(run['allowances'])
    for mid,items in groups.items():
        first=items[0];status='pending' if first['sync_supported'] else 'failed'
        error='' if first['sync_supported'] else '本地 bridge 不支持同步轮次，请更新后重试'
        db.execute('INSERT INTO sync_participants(root,round,member,delivery,quota,status,error) VALUES(?,?,?,?,?,?,?)',
            (run['root'],number,mid,first['id'],allowances.get(mid,1),status,error))
        for index,item in enumerate(items):
            db.execute('UPDATE deliveries SET status=?,error=? WHERE id=?',(status if index==0 or status=='failed' else 'bundled',error,item['id']))
    return bool(groups)


def posted(db,message_id,actor,plan,maximum,allowances):
    if plan['mode']!='synchronous':return
    root=plan['root'] or message_id;number=plan['round']+1
    db.execute('UPDATE deliveries SET sync_root=?,sync_round=?,status=CASE WHEN status=\'pending\' THEN \'round_waiting\' ELSE status END WHERE message=?',(root,number,message_id))
    if plan['root'] is None:
        room=db.execute('SELECT room FROM messages WHERE id=?',(message_id,)).fetchone()['room']
        db.execute('INSERT INTO sync_runs(root,room,maximum,allowances,cutoff,created) VALUES(?,?,?,?,?,?)',
            (root,room,maximum,json.dumps(allowances),message_id,time.time()))
        run=active(db,room)
        if not enroll(db,run,1):db.execute("UPDATE sync_runs SET status='completed',reason='没有可参与的 session' WHERE root=?",(root,))
    else:
        if actor['kind']=='agent':
            db.execute('UPDATE sync_participants SET used=used+1 WHERE root=? AND round=? AND member=?',(root,plan['round'],actor['id']))
        if maximum and plan['round']>=maximum:
            db.execute("UPDATE deliveries SET status='skipped',error='最后一轮只收尾，不再触发下一轮' WHERE message=? AND status='round_waiting'",(message_id,))


def settle(db,room_id):
    recovery.reconcile(db,room_id)
    run=active(db,room_id)
    if not run:return
    root=run['root'];number=run['round'];now=time.time()
    members=db.execute('''SELECT p.*,d.status AS delivery_status,d.error AS delivery_error,d.lease_until,
        m.revoked,m.paused,m.disabled_reason,b.paused AS bridge_paused
        FROM sync_participants p JOIN deliveries d ON d.id=p.delivery JOIN members m ON m.id=p.member
        LEFT JOIN members b ON b.id=m.bridge WHERE p.root=? AND p.round=?''',(root,number)).fetchall()
    for p in members:
        if p['status'] in ('done','skipped'):continue
        status=p['status'];error=p['error']
        if p['delivery_status']=='failed':status,error='failed',p['delivery_error']
        elif p['revoked'] or p['bridge_paused'] or (p['paused'] and p['disabled_reason']!='rate_limit') or p['delivery_status']=='cancelled':
            status,error='skipped','此 session 或 bridge 已关闭'
        elif p['delivery_status']=='running' and p['lease_until']<now:status,error='failed','服务器任务租约已到期；本轮参与已结束'
        elif p['delivery_status']=='done':status,error='done',''
        if status!=p['status']:
            db.execute('UPDATE sync_participants SET status=?,error=? WHERE root=? AND round=? AND member=?',(status,error,root,number,p['member']))
            if status in ('failed','skipped'):
                db.execute("UPDATE deliveries SET status=?,error=? WHERE sync_root=? AND sync_round=? AND member=? AND status IN ('pending','running','bundled')",(status,error,root,number,p['member']))
    for p in db.execute("SELECT delivery FROM sync_participants WHERE root=? AND round=? AND status='failed'",(root,number)).fetchall():
        recovery.queue(db,p['delivery'])
    if recovery.waiting(db,root,number):
        db.execute("UPDATE sync_runs SET status='running',reason='正在处理本轮执行异常' WHERE root=?",(root,))
        return
    statuses=[r['status'] for r in db.execute('SELECT status FROM sync_participants WHERE root=? AND round=?',(root,number))]
    # A failed participant cannot hold future messages hostage when no manager
    # can handle it, or after the manager explicitly chooses to move on.
    reason='有 session 失败；等待其余成员结束后继续' if 'failed' in statuses else ''
    db.execute("UPDATE sync_runs SET status='running',reason=? WHERE root=?",(reason,root))
    if any(s not in ('done','skipped','failed') for s in statuses):return
    if run['maximum'] and number>=run['maximum']:
        db.execute("UPDATE sync_runs SET status='completed',reason='最后一轮已结束' WHERE root=?",(root,));return
    cutoff=db.execute('SELECT COALESCE(MAX(id),0) FROM messages WHERE room=?',(room_id,)).fetchone()[0]
    if enroll(db,run,number+1):
        db.execute("UPDATE sync_runs SET round=?,cutoff=?,status='running',reason='' WHERE root=?",(number+1,cutoff,root))
        # Unsupported bridges stay visibly failed. The next settle also treats
        # those failures as terminal, rather than opening another blocked run.
    else:db.execute("UPDATE sync_runs SET status='completed',reason='没有下一轮待处理的点名，已提前结束' WHERE root=?",(root,))


def claimed(db,delivery):
    if not delivery['sync_root']:return
    db.execute("UPDATE sync_participants SET status='running' WHERE delivery=?",(delivery['id'],))


def quota_exhausted(db,delivery):
    if not delivery['sync_root']:return False
    p=db.execute('SELECT used,quota FROM sync_participants WHERE delivery=?',(delivery['id'],)).fetchone()
    return bool(p and p['used']>=p['quota'])


def finished(db,delivery,error):
    if not delivery['sync_root']:return
    status='failed' if error else 'done'
    db.execute('UPDATE sync_participants SET status=?,error=? WHERE delivery=?',(status,error or '',delivery['id']))
    db.execute("UPDATE deliveries SET status=?,error=? WHERE sync_root=? AND sync_round=? AND member=? AND status='bundled'",
        (status,error or '',delivery['sync_root'],delivery['sync_round'],delivery['member']))
    settle(db,delivery['room'])


def payload(store,db,delivery,actor):
    root=delivery['sync_root'];number=delivery['sync_round']
    run=db.execute('SELECT * FROM sync_runs WHERE root=?',(root,)).fetchone()
    p=db.execute('SELECT * FROM sync_participants WHERE delivery=?',(delivery['id'],)).fetchone()
    inbox=db.execute('SELECT m.* FROM messages m JOIN deliveries d ON d.message=m.id WHERE d.sync_root=? AND d.sync_round=? AND d.member=? AND m.recalled_at=0 AND d.status IN (\'running\',\'bundled\') ORDER BY m.id',(root,number,actor['id'])).fetchall()
    previous=db.execute("SELECT * FROM messages WHERE (root=? OR id=?) AND sync_round=? AND private_to='' AND recalled_at=0 AND id<=? ORDER BY id",(root,root,number-1,run['cutoff'])).fetchall()
    return {'root':root,'round':number,'cutoff':run['cutoff'],'quota':p['quota'],'used':p['used'],
        'inbox':[store.message_dict(db,m,actor) for m in inbox],
        'previous_messages':[store.message_dict(db,m,actor) for m in previous]}


def view(db,room_id,actor):
    from .guests import owner
    if actor.get('session_type')=='manager':return None
    row=db.execute('SELECT * FROM sync_runs WHERE room=? ORDER BY root DESC LIMIT 1',(room_id,)).fetchone()
    if not row:return None
    result=dict(row);result['allowances']=json.loads(result['allowances'])
    if owner(actor,room_id):result['recovering']=recovery.waiting(db,row['root'],row['round'])
    result['participants']=[dict(p) for p in db.execute('''SELECT p.member,p.delivery,m.name,p.quota,p.used,p.status,p.error
        FROM sync_participants p JOIN members m ON m.id=p.member WHERE p.root=? AND p.round=? ORDER BY m.name''',(row['root'],row['round']))]
    return result


def stop(db,room_id):
    db.execute("UPDATE sync_runs SET status='stopped',reason='管理员停止了同步对话' WHERE room=? AND status IN ('running','blocked')",(room_id,))
    db.execute("UPDATE sync_participants SET status='skipped',error='管理员停止了同步对话' WHERE root IN (SELECT root FROM sync_runs WHERE room=? AND status='stopped') AND status IN ('pending','running','failed')",(room_id,))
    return db.execute("UPDATE deliveries SET status='cancelled',error='管理员停止了同步对话' WHERE room=? AND sync_root IS NOT NULL AND status IN ('pending','running','bundled','round_waiting')",(room_id,)).rowcount


def continue_failed(store,actor,room_id,root,number):
    from .core import Problem
    if actor['kind']!='user':raise Problem('只有管理员能跳过失败并继续',403)
    with store.tx() as db:
        store.access(db,room_id,actor);settle(db,room_id);run=active(db,room_id)
        if not run or run['root']!=root or run['round']!=number:return {'ok':True}
        recovery.cancel(db,root=root,number=number,reason='管理员选择直接进入下一轮')
        failed=db.execute("SELECT member FROM sync_participants WHERE root=? AND round=? AND status='failed'",(root,number)).fetchall()
        for p in failed:
            db.execute("UPDATE sync_participants SET status='skipped',error='管理员选择跳过：'||error WHERE root=? AND round=? AND member=?",(root,number,p['member']))
            db.execute("UPDATE deliveries SET status='skipped',error='管理员已跳过本轮失败' WHERE sync_root=? AND sync_round=? AND member=? AND status='failed'",(root,number,p['member']))
        settle(db,room_id)
        return {'ok':True}


def retryable(db,delivery_id):
    delivery=db.execute('SELECT sync_root,sync_round,room,status FROM deliveries WHERE id=?',(delivery_id,)).fetchone()
    if not delivery or delivery['status']!='failed':return False
    if not delivery['sync_root']:return True
    run=active(db,delivery['room'])
    return bool(run and run['root']==delivery['sync_root'] and run['round']==delivery['sync_round']
        and db.execute("SELECT 1 FROM sync_participants WHERE delivery=? AND status='failed'",(delivery_id,)).fetchone())


def retry(db,delivery):
    from .core import Problem
    run=active(db,delivery['room'])
    p=db.execute('SELECT * FROM sync_participants WHERE root=? AND round=? AND member=?',(delivery['sync_root'],delivery['sync_round'],delivery['member'])).fetchone()
    if not run or run['root']!=delivery['sync_root'] or run['round']!=delivery['sync_round'] or not p or p['status']!='failed':raise Problem('这一同步轮次已结束，不能重试旧任务',409)
    recovery.cancel(db,p['delivery'])
    db.execute("UPDATE sync_participants SET status='pending',error='' WHERE delivery=?",(p['delivery'],))
    db.execute("UPDATE deliveries SET status=CASE WHEN id=? THEN 'pending' ELSE 'bundled' END,error='',lease=NULL WHERE sync_root=? AND sync_round=? AND member=? AND status='failed'",(p['delivery'],run['root'],run['round'],delivery['member']))
    settle(db,delivery['room'])
