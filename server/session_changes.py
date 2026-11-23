"""Owner-authored session changes, privately delegated to an existing manager."""
import hashlib
import json
import secrets
import time
from contextlib import nullcontext

from bridge.roles import worker_roles
from bridge.model_settings import configured_model

SCHEMA='''
CREATE TABLE IF NOT EXISTS bridge_projects(
 bridge TEXT PRIMARY KEY,supported INTEGER NOT NULL DEFAULT 0,workspace TEXT NOT NULL DEFAULT '',project TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS session_changes(
 id TEXT PRIMARY KEY,room TEXT NOT NULL,bridge TEXT NOT NULL,manager TEXT NOT NULL,nonce TEXT NOT NULL,
 additions TEXT NOT NULL,removals TEXT NOT NULL,added TEXT NOT NULL DEFAULT '[]',
 status TEXT NOT NULL DEFAULT 'pending',error TEXT NOT NULL DEFAULT '',request INTEGER,delivery TEXT,
 created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(room,nonce));
CREATE UNIQUE INDEX IF NOT EXISTS changes_one_active ON session_changes(bridge) WHERE status IN ('pending','running','applying');
'''
ACTIVE=('pending','running','applying')


def problem(message,status=400):
    from .core import Problem
    raise Problem(message,status)


def revision(db,bridge):
    rows=[tuple(r) for r in db.execute("SELECT id,name,harness,role,revoked FROM members WHERE bridge=? AND kind='agent' ORDER BY id",(bridge,))]
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:20]


def view(db,row):
    r=dict(row)
    for key in ('additions','removals','added'):r[key]=json.loads(r[key])
    r['manager_name']=db.execute('SELECT name FROM members WHERE id=?',(r['manager'],)).fetchone()[0]
    r['can_apply']=bool(available(db,r['bridge']))
    return r


def inventory(store,room,actor):
    if actor['kind']!='user':problem('只有群聊管理员可管理 session 清单',403)
    with store.tx() as db:
        store.access(db,room,actor);settle(db,room)
        managers=[]
        for row in db.execute("""SELECT m.id,m.name,m.bridge,m.paused,b.name AS bridge_name,b.last_seen,
            b.paused AS bridge_paused,p.supported,p.workspace,p.project
            FROM members m JOIN members b ON b.id=m.bridge LEFT JOIN bridge_projects p ON p.bridge=b.id
            WHERE m.room=? AND m.session_type='manager' AND m.revoked=0 AND b.revoked=0 ORDER BY m.created""",(room,)):
            m=dict(row);m['online']=time.time()-m.pop('last_seen')<45;m['revision']=revision(db,m['bridge'])
            m['sessions']=[dict(s) for s in db.execute("SELECT id,name,harness,role FROM members WHERE bridge=? AND session_type='worker' AND kind='agent' AND revoked=0 ORDER BY created",(m['bridge'],))]
            managers.append(m)
        jobs=[view(db,r) for r in db.execute('SELECT * FROM session_changes WHERE room=? ORDER BY created DESC LIMIT 20',(room,))]
        return {'managers':managers,'changes':jobs}


def enqueue(db,r):
    manager=db.execute('SELECT name FROM members WHERE id=?',(r['manager'],)).fetchone()[0]
    owner=db.execute("SELECT id FROM members WHERE room=? AND id=(SELECT owner||':'||id FROM rooms WHERE id=members.room)",(r['room'],)).fetchone()[0]
    names=[x['name'] for x in json.loads(r['additions'])]
    removed=[x['name'] for x in json.loads(r['removals'])]
    body=f'@{manager} 请处理我在设置中提交的 session 变更：'+('新增 '+ '、'.join(names)+'；' if names else '')+('移除 '+'、'.join(removed)+'；' if removed else '')+'沿用当前项目，保留历史和文件。'
    request=db.execute("""INSERT INTO messages(room,sender,body,targets,nonce,created,private_to,max_rounds,message_kind)
        VALUES(?,?,?,?,?,?,?,0,'session_change_request')""",(r['room'],owner,body,json.dumps([manager]),secrets.token_hex(12),time.time(),r['manager'])).lastrowid
    did=secrets.token_hex(12)
    db.execute('INSERT INTO deliveries(id,room,member,message,created) VALUES(?,?,?,?,?)',(did,r['room'],r['manager'],request,time.time()))
    db.execute("UPDATE session_changes SET request=?,delivery=?,source='owner',status='pending',error='',updated=? WHERE id=?",(request,did,time.time(),r['id']))
    # Submitting a task explicitly addressed to this manager is an owner activation.
    db.execute("UPDATE members SET enabled=1 WHERE id=?",(r['manager'],))


def submit(store,room,actor,manager,additions,removals,expected,nonce,removal_mode='legacy',_db=None,_source=None):
    if actor['kind']!='user':problem('只有群聊管理员可提交增减清单',403)
    try:roles=worker_roles(additions) if additions else []
    except ValueError as exc:problem(str(exc))
    if any(r.get('session_type')=='manager' for r in additions):problem('不能通过角色列表新增 manager')
    if not roles and not removals:problem('还没有新增或移除的 session')
    if len(removals)!=len(set(removals)):problem('移除清单重复')
    if removal_mode not in ('legacy','finish','immediate'):problem('无效的退出方式')
    with (nullcontext(_db) if _db is not None else store.tx()) as db:
        room_info=store.access(db,room,actor);settle(db,room)
        old=db.execute('SELECT * FROM session_changes WHERE room=? AND nonce=?',(room,nonce)).fetchone()
        if old:
            if old['manager']!=manager or old['removal_mode']!=removal_mode or json.loads(old['additions'])!=roles or [x['id'] for x in json.loads(old['removals'])]!=removals:problem('此提交编号已用于其他变更',409)
            return view(db,old)
        m=db.execute("SELECT * FROM members WHERE id=? AND room=? AND session_type='manager' AND revoked=0",(manager,room)).fetchone()
        if not m:problem('请选择本群已接入的 manager')
        b=db.execute('SELECT * FROM members WHERE id=?',(m['bridge'],)).fetchone()
        cap=db.execute('SELECT * FROM bridge_projects WHERE bridge=?',(m['bridge'],)).fetchone()
        if m['paused'] or b['paused'] or b['revoked'] or room_info['paused']:problem('请先恢复 manager、bridge 和群聊',409)
        if not cap or not cap['supported']:problem('请更新并重启该 manager 所在的 bridge 后再增减 session',409)
        if revision(db,m['bridge'])!=expected:problem('session 列表已变化，请重新载入后提交',409)
        if db.execute("SELECT 1 FROM session_changes WHERE bridge=? AND status IN ('pending','running','applying')",(m['bridge'],)).fetchone():problem('这个 manager 还有未完成的 session 变更，请等待或取消后再提交',409)
        removed=[]
        for mid in removals:
            target=db.execute("SELECT id,name FROM members WHERE id=? AND bridge=? AND room=? AND kind='agent' AND session_type='worker' AND revoked=0",(mid,m['bridge'],room)).fetchone()
            if not target:problem('只能移除所选 manager 的当前 worker',409)
            removed.append(dict(target))
        for role in roles:
            if db.execute('SELECT 1 FROM members WHERE room=? AND name=?',(room,role['name'])).fetchone():problem('名称已存在或曾被删除：'+role['name']+'；已删除的 session 请使用恢复功能',409)
        count=db.execute("SELECT COUNT(*) FROM members WHERE bridge=? AND kind='agent' AND session_type='worker' AND revoked=0",(m['bridge'],)).fetchone()[0]
        if count+len(roles)-len(removed)>100:problem('每个 bridge 最多 100 个协作 session')
        rid=secrets.token_hex(12);now=time.time()
        db.execute('INSERT INTO session_changes(id,room,bridge,manager,nonce,additions,removals,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                   (rid,room,m['bridge'],manager,nonce,json.dumps(roles,ensure_ascii=False),json.dumps(removed,ensure_ascii=False),now,now))
        db.execute('UPDATE session_changes SET removal_mode=? WHERE id=?',(removal_mode,rid))
        if _source:
            db.execute("UPDATE session_changes SET status='applying',source='manager',request=?,delivery=? WHERE id=?",(_source['message'],_source['id'],rid))
        else:enqueue(db,db.execute('SELECT * FROM session_changes WHERE id=?',(rid,)).fetchone())
        return view(db,db.execute('SELECT * FROM session_changes WHERE id=?',(rid,)).fetchone())


def access(db,actor,rid,bridge=False):
    r=db.execute('SELECT * FROM session_changes WHERE id=?',(rid,)).fetchone()
    if not r or (bridge and (actor['kind']!='bridge' or r['bridge']!=actor['id'])) or (not bridge and (actor.get('session_type')!='manager' or r['manager']!=actor['id'])):
        problem('无权处理这项 session 变更',403)
    return r


def manager_task_live(db,r):
    d=db.execute('SELECT status,lease_until FROM deliveries WHERE id=?',(r['delivery'],)).fetchone()
    if not d or d['status']!='running' or d['lease_until']<time.time():problem('这项管理任务不再运行，请由用户重试',409)


def approve(db,r):
    if r['status'] in ('applying','complete'):return
    if r['status'] not in ('pending','running'):problem('这项变更已结束，请由用户重试',409)
    manager_task_live(db,r)
    room=db.execute('SELECT paused FROM rooms WHERE id=?',(r['room'],)).fetchone()
    manager=db.execute('SELECT paused,revoked FROM members WHERE id=?',(r['manager'],)).fetchone()
    bridge=db.execute('SELECT paused,revoked FROM members WHERE id=?',(r['bridge'],)).fetchone()
    if room['paused'] or manager['paused'] or manager['revoked'] or bridge['paused'] or bridge['revoked']:problem('群聊、manager 或 bridge 已关闭',409)
    db.execute("UPDATE session_changes SET status='applying',error='',updated=? WHERE id=?",(time.time(),r['id']))


def manager_action(store,actor,rid,apply=False):
    with store.tx() as db:
        r=access(db,actor,rid)
        if apply:approve(db,r)
        return view(db,db.execute('SELECT * FROM session_changes WHERE id=?',(rid,)).fetchone())


def payload(db,message):
    row=db.execute('SELECT * FROM session_changes WHERE request=?',(message,)).fetchone()
    return view(db,row) if row else None


def finish(db,delivery,body):
    r=db.execute("SELECT * FROM session_changes WHERE delivery=? AND source!='manager'",(delivery['id'],)).fetchone()
    if not r:return False
    if r['status'] in ('applying','complete','cancelled','failed'):return True
    try:decision=json.JSONDecoder().raw_decode(body[body.index('{'):])[0]
    except (ValueError,TypeError):decision={}
    if isinstance(decision,dict) and decision.get('action')=='apply':approve(db,r)
    else:db.execute("UPDATE session_changes SET status='failed',error=?,updated=? WHERE id=?",(('manager 尚未执行变更：'+body)[:1000],time.time(),r['id']))
    return True


def settle(db,room):
    db.execute("""UPDATE session_changes SET status='failed',error='bridge 已撤销，无法继续本地变更',updated=?
        WHERE room=? AND status='applying' AND bridge IN (SELECT id FROM members WHERE revoked=1)""",(time.time(),room))
    for r in db.execute("""SELECT c.*,d.status AS task_status,d.error AS task_error,d.lease_until,m.paused,m.revoked
        FROM session_changes c JOIN deliveries d ON d.id=c.delivery JOIN members m ON m.id=c.manager
        WHERE c.room=? AND c.status IN ('pending','running')""",(room,)).fetchall():
        ended=r['task_status'] in ('failed','cancelled','done') or (r['task_status']=='running' and r['lease_until']<time.time()) or r['paused'] or r['revoked']
        if ended:
            db.execute("UPDATE session_changes SET status='failed',error=?,updated=? WHERE id=?",(r['task_error'] or 'manager 未完成变更，请检查后重试',time.time(),r['id']))
            db.execute("UPDATE deliveries SET status='cancelled',error='session 变更已结束' WHERE id=? AND status IN ('pending','running')",(r['delivery'],))
        elif r['task_status']=='running':db.execute("UPDATE session_changes SET status='running' WHERE id=?",(r['id'],))


def control(store,room,actor,rid,action):
    if actor['kind']!='user':problem('只有群聊管理员可重试或取消变更',403)
    with store.tx() as db:
        room_info=store.access(db,room,actor);settle(db,room)
        r=db.execute('SELECT * FROM session_changes WHERE id=? AND room=?',(rid,room)).fetchone()
        if not r:problem('变更不存在',404)
        if action=='cancel':
            if r['status']=='complete':problem('变更已完成，不能撤销已完成的操作',409)
            db.execute("UPDATE session_changes SET status='cancelled',error='用户取消；已完成的部分保留',updated=? WHERE id=?",(time.time(),rid))
            db.execute("UPDATE deliveries SET status='cancelled',error='用户取消 session 变更' WHERE id=? AND status IN ('pending','running')",(r['delivery'],))
            from . import lifecycle
            for op in db.execute("SELECT member FROM session_transitions WHERE nonce=? AND status='pending' AND action='leave'",('change:'+rid,)).fetchall():
                lifecycle.request_in(store,db,room,op['member'],'cancel-leave','cancel-change:'+rid)
        elif action=='retry':
            if r['status']!='failed':problem('只能重试失败的变更',409)
            if db.execute("SELECT 1 FROM session_changes WHERE bridge=? AND status IN ('pending','running','applying')",(r['bridge'],)).fetchone():problem('已有其他变更正在进行',409)
            m=db.execute('SELECT paused,revoked FROM members WHERE id=?',(r['manager'],)).fetchone()
            b=db.execute('SELECT paused,revoked FROM members WHERE id=?',(r['bridge'],)).fetchone()
            cap=db.execute('SELECT supported FROM bridge_projects WHERE bridge=?',(r['bridge'],)).fetchone()
            if m['paused'] or m['revoked'] or b['paused'] or b['revoked'] or room_info['paused']:problem('请先恢复原 manager、bridge 和群聊',409)
            if not cap or not cap['supported']:problem('请先更新并重启 bridge',409)
            enqueue(db,r)
        else:problem('未知操作')
        return view(db,db.execute('SELECT * FROM session_changes WHERE id=?',(rid,)).fetchone())


def pending(db,bridge):
    if not available(db,bridge):return []
    return [view(db,r) for r in db.execute("SELECT * FROM session_changes WHERE bridge=? AND status='applying' ORDER BY created",(bridge,))]


def available(db,bridge):
    row=db.execute('SELECT b.paused,b.revoked,r.paused AS room_paused FROM members b JOIN rooms r ON r.id=b.room WHERE b.id=?',(bridge,)).fetchone()
    return row and not (row['paused'] or row['revoked'] or row['room_paused'])


def cancel_room(db,room):
    db.execute("UPDATE session_changes SET status='cancelled',error='管理员停止所有任务；已完成的部分保留',updated=? WHERE room=? AND status IN ('pending','running','applying')",(time.time(),room))


def registration(db,actor,rid,name,role,harness,model):
    r=access(db,actor,rid,bridge=True)
    if r['status']!='applying':problem('这项 session 变更不在执行中',409)
    if not available(db,r['bridge']):problem('bridge 或群聊已暂停',409)
    desired=next((x for x in json.loads(r['additions']) if x['name']==name),None)
    if not desired or desired['role']!=role or desired['harness']!=harness or configured_model(desired)!=model:problem('注册内容不属于用户批准的新增清单',403)
    return desired


def registered(db,rid,mid,desired):
    added=json.loads(db.execute('SELECT added FROM session_changes WHERE id=?',(rid,)).fetchone()[0])
    if mid not in added:
        added.append(mid);db.execute('UPDATE session_changes SET added=?,updated=? WHERE id=?',(json.dumps(added),time.time(),rid))
        db.execute('INSERT OR IGNORE INTO agent_settings(member) VALUES(?)',(mid,))
        db.execute('UPDATE agent_settings SET reasoning_effort=?,timeout_seconds=? WHERE member=?',(desired.get('reasoning_effort'),desired.get('timeout_seconds'),mid))


def remove(store,actor,rid,mid):
    with store.tx() as db:
        r=access(db,actor,rid,bridge=True)
        if r['status']!='applying':problem('这项 session 变更不在执行中',409)
        if not available(db,r['bridge']):problem('bridge 或群聊已暂停',409)
        if mid not in {x['id'] for x in json.loads(r['removals'])}:problem('此 session 不在用户批准的移除清单中',403)
        if r['removal_mode']!='legacy':
            from . import lifecycle
            operation=lifecycle.request_in(store,db,r['room'],mid,'leave','change:'+rid,r['removal_mode']=='immediate')
            if operation['status']=='cancelled':problem('此 session 的退出已取消；请取消这份变更后重新提交',409)
            return {'ok':operation['status']=='complete','operation':operation}
        owner=db.execute('SELECT owner FROM rooms WHERE id=?',(r['room'],)).fetchone()[0]
        store.session_lifecycle(r['room'],mid,{'kind':'user','id':owner},'delete',_db=db)
        return {'ok':True}


def report(store,actor,rid,error=''):
    with store.tx() as db:
        r=access(db,actor,rid,bridge=True)
        if r['status']=='complete':return {'ok':True}
        if r['status']!='applying':problem('这项 session 变更不在执行中',409)
        if error:
            db.execute("UPDATE session_changes SET status='failed',error=?,updated=? WHERE id=?",(error[:1000],time.time(),rid));return {'ok':True}
        added=json.loads(r['added']);roles=json.loads(r['additions']);removals=json.loads(r['removals'])
        if len(added)!=len(roles):problem('新增 session 尚未全部注册',409)
        for mid in added:
            m=db.execute('SELECT revoked FROM members WHERE id=?',(mid,)).fetchone()
            if not m or m['revoked']:problem('新增 session 已被移除，不能报告完成',409)
        for item in removals:
            if not db.execute('SELECT revoked FROM members WHERE id=?',(item['id'],)).fetchone()[0]:problem('移除操作尚未完成',409)
        db.execute("UPDATE session_changes SET status='complete',error='',updated=? WHERE id=?",(time.time(),rid))
        owner=db.execute("SELECT id,name FROM members WHERE room=? AND id=(SELECT owner||':'||id FROM rooms WHERE id=members.room)",(r['room'],)).fetchone()
        body=f"@{owner['name']} session 变更已完成。"+('新增：'+ '、'.join('@'+x['name'] for x in roles)+'，等待你首次点名。' if roles else '')+('已移除：'+'、'.join('@'+x['name'] for x in removals)+'；历史、原生会话和本地文件保留。' if removals else '')
        db.execute("""INSERT INTO messages(room,sender,body,targets,nonce,created,private_to,max_rounds,message_kind)
            VALUES(?,?,?,?,?,?,?,0,'session_change_result')""",(r['room'],owner['id'],body,json.dumps([owner['name']]),'session-change-result:'+rid,time.time(),r['manager']))
        return {'ok':True}
