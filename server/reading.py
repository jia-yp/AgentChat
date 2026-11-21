"""Owner-only reading index, literal history search and private manager summaries.

Reading groups never change message visibility or the worker's round barrier.
Automatic summaries use the normal durable delivery/lease mechanism.
"""
import hashlib
import json
import secrets
import time

ROUND = "CASE WHEN m.round_mode='synchronous' THEN m.sync_round ELSE m.depth END"
ROOT = 'COALESCE(m.root,m.id)'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS reading_state(id INTEGER PRIMARY KEY,auto_after INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS round_summaries(
 root INTEGER NOT NULL,number INTEGER NOT NULL,room TEXT NOT NULL,
 source_end INTEGER NOT NULL,source_count INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'waiting',manager TEXT,request INTEGER,delivery TEXT,result INTEGER,
 error TEXT NOT NULL DEFAULT '',created REAL NOT NULL,updated REAL NOT NULL,
 PRIMARY KEY(root,number));
CREATE INDEX IF NOT EXISTS summaries_room ON round_summaries(room);
'''


def initialize(db):
    db.executescript(SCHEMA)
    # Installing a new feature must not spend the manager's budget on all old chats.
    db.execute('INSERT OR IGNORE INTO reading_state VALUES(1,(SELECT COALESCE(MAX(id),0) FROM messages))')


def owner(store, db, room, actor):
    from .core import Problem
    if actor['kind'] != 'user':raise Problem('阅读索引和搜索仅群聊管理员可用',403)
    return store.access(db,room,actor)


def rounds(db, room):
    groups=[dict(r) for r in db.execute(f'''SELECT {ROOT} AS root,{ROUND} AS number,
        m.round_mode AS mode,COUNT(*) AS count,MIN(m.id) AS first,MAX(m.id) AS last
        FROM messages m WHERE m.room=? AND m.private_to='' AND m.message_kind='chat'
        AND m.delivery_mode='normal' AND m.recalled_at=0 AND {ROUND}>0 GROUP BY {ROOT},{ROUND} ORDER BY first''',(room,))]
    runs={r['root']:dict(r) for r in db.execute('SELECT * FROM sync_runs WHERE room=?',(room,))}
    unresolved={}
    for r in db.execute(f'''SELECT {ROOT} AS root,MIN(m.depth+1) AS first FROM deliveries d
        JOIN messages m ON m.id=d.message WHERE d.room=? AND m.private_to=''
        AND d.status IN ('pending','running','failed') GROUP BY {ROOT}''',(room,)):
        unresolved[r['root']]=r['first']
    stopped={r['id'] for r in db.execute('SELECT id FROM messages WHERE room=? AND stopped=1',(room,))}
    for g in groups:
        run=runs.get(g['root'])
        g['stopped']=g['root'] in stopped or bool(run and run['status']=='stopped')
        if g['mode']=='synchronous':
            g['complete']=bool(run and (run['round']>g['number'] or run['status'] in ('completed','stopped')))
        else:g['complete']=g['stopped'] or unresolved.get(g['root'],float('inf'))>g['number']
        g['key']=f"{g['root']}:{g['number']}"
    return groups


def settle(store, db, room):
    now=time.time();watermark=db.execute('SELECT auto_after FROM reading_state WHERE id=1').fetchone()[0]
    paused=db.execute('SELECT paused FROM rooms WHERE id=?',(room,)).fetchone()[0]
    for g in rounds(db,room):
        job=db.execute('SELECT * FROM round_summaries WHERE root=? AND number=?',(g['root'],g['number'])).fetchone()
        if job and (job['source_end']!=g['last'] or job['source_count']!=g['count']):
            if job['delivery']:db.execute("UPDATE deliveries SET status='cancelled',error='轮次内容已更新，旧摘要任务已取消' WHERE id=? AND status IN ('pending','running')",(job['delivery'],))
            db.execute("UPDATE round_summaries SET source_end=?,source_count=?,status='waiting',request=NULL,delivery=NULL,result=NULL,error='',updated=? WHERE root=? AND number=?",(g['last'],g['count'],now,g['root'],g['number']))
            job=db.execute('SELECT * FROM round_summaries WHERE root=? AND number=?',(g['root'],g['number'])).fetchone()
        if not job and g['count']>5 and g['complete'] and not g['stopped'] and g['last']>watermark:
            db.execute('INSERT INTO round_summaries(root,number,room,source_end,source_count,created,updated) VALUES(?,?,?,?,?,?,?)',(g['root'],g['number'],room,g['last'],g['count'],now,now))
            job=db.execute('SELECT * FROM round_summaries WHERE root=? AND number=?',(g['root'],g['number'])).fetchone()
        if not job:continue
        if job['delivery']:
            delivery=db.execute('SELECT status,error FROM deliveries WHERE id=?',(job['delivery'],)).fetchone()
            status={'pending':'queued','running':'running','failed':'failed','cancelled':'cancelled'}.get(delivery['status']) if delivery else 'failed'
            if status and status!=job['status']:
                db.execute('UPDATE round_summaries SET status=?,error=?,updated=? WHERE root=? AND number=?',(status,delivery['error'] if delivery else '摘要任务丢失',now,g['root'],g['number']))
            continue
        if job['status']!='waiting' or not g['complete'] or paused:continue
        if g['stopped']:
            db.execute("UPDATE round_summaries SET status='cancelled',error='协作已停止',updated=? WHERE root=? AND number=?",(now,g['root'],g['number']));continue
        enqueue(store,db,g,room)


def enqueue(store, db, group, room, explicit=False):
    from .core import Problem
    # Prefer this conversation's own bridge, and never activate a dormant/disabled manager.
    manager=db.execute('''SELECT m.* FROM members m JOIN members b ON b.id=m.bridge
        WHERE m.room=? AND m.session_type='manager' AND m.revoked=0 AND m.paused=0 AND m.enabled=1
        AND m.summary_supported=1 AND b.revoked=0 AND b.paused=0
        ORDER BY CASE WHEN m.bridge=(SELECT s.bridge FROM messages x JOIN members s ON s.id=x.sender WHERE x.id=?) THEN 0 ELSE 1 END,m.created,m.id LIMIT 1''',(room,group['first'])).fetchone()
    if not manager:
        if explicit:raise Problem('需要已启用的 manager 和新版 bridge；请先 @ manager，并确认它未关闭',409)
        return
    admin=db.execute("SELECT id,name FROM members WHERE room=? AND kind='user'",(room,)).fetchone()
    body=f"@{manager['name']} 请对任务 #{group['root']} 的第 {group['number']} 轮作简短阅读摘要，仅回复 @{admin['name']}。这是自动摘要，不是新的项目任务；不要执行工具、修改文件或分派工作。"
    request=db.execute('''INSERT INTO messages(room,sender,body,targets,nonce,created,private_to,max_rounds,message_kind,summary_root,summary_round)
        VALUES(?,?,?,?,?,?,?,0,'summary_request',?,?)''',(room,admin['id'],body,json.dumps([manager['name']]),'summary:'+secrets.token_hex(12),time.time(),manager['id'],group['root'],group['number'])).lastrowid
    delivery=secrets.token_hex(12)
    db.execute('INSERT INTO deliveries(id,room,member,message,created) VALUES(?,?,?,?,?)',(delivery,room,manager['id'],request,time.time()))
    db.execute("UPDATE round_summaries SET status='queued',manager=?,request=?,delivery=?,result=NULL,error='',updated=? WHERE root=? AND number=?",(manager['id'],request,delivery,time.time(),group['root'],group['number']))


def payload(db, message):
    if message['message_kind']!='summary_request':return None
    rows=db.execute(f'''SELECT m.id,m.body,m.created,s.name AS sender FROM messages m JOIN members s ON s.id=m.sender
        WHERE m.room=? AND {ROOT}=? AND {ROUND}=? AND m.private_to='' AND m.message_kind='chat'
        AND m.delivery_mode='normal' AND m.recalled_at=0 ORDER BY m.id''',(message['room'],message['summary_root'],message['summary_round'])).fetchall()
    return {'root':message['summary_root'],'round':message['summary_round'],'messages':[dict(r) for r in rows]}


def finish(store, db, delivery, member, body, runtime):
    request=db.execute('SELECT * FROM messages WHERE id=?',(delivery['message'],)).fetchone()
    if request['message_kind']!='summary_request':return False
    from .core import Problem
    job=db.execute('SELECT * FROM round_summaries WHERE request=? AND delivery=?',(request['id'],delivery['id'])).fetchone()
    if not job:raise Problem('摘要已过期',409)
    # A summary has one final private result. It cannot route or spawn another task.
    admin=db.execute("SELECT name FROM members WHERE room=? AND kind='user'",(delivery['room'],)).fetchone()[0]
    import re
    text=re.sub(r'^(?:\s*@[\w\-]+[\s,，:：;；]*)+','',body).strip()
    if not text:raise Problem('摘要为空')
    result=store.post_in(db,delivery['room'],member,f'@{admin} '+text[:1500],
                         'delivery:'+delivery['id'],cause=request['id'],runtime=runtime,_summary=True)
    db.execute("UPDATE messages SET message_kind='round_summary',summary_root=?,summary_round=? WHERE id=?",(job['root'],job['number'],result['id']))
    db.execute("UPDATE round_summaries SET status='done',result=?,error='',updated=? WHERE root=? AND number=?",(result['id'],time.time(),job['root'],job['number']))
    return True


def timeline(store, room, actor, revision=''):
    from . import synchronous
    with store.tx() as db:
        owner(store,db,room,actor);synchronous.settle(db,room);settle(store,db,room)
        jobs=[dict(r) for r in db.execute('''SELECT j.*,s.name AS manager_name,m.body AS body FROM round_summaries j
            LEFT JOIN members s ON s.id=j.manager LEFT JOIN messages m ON m.id=j.result WHERE j.room=?''',(room,))]
        groups=rounds(db,room)
        recalls=[dict(r) for r in db.execute('SELECT id,recalled_at FROM messages WHERE room=? AND recalled_at>0 ORDER BY id',(room,))]
        stamp=[tuple(db.execute('SELECT COUNT(*),MAX(id) FROM messages WHERE room=?',(room,)).fetchone()),groups,jobs,recalls]
        current=hashlib.sha256(json.dumps(stamp,sort_keys=True).encode()).hexdigest()[:20]
        if revision==current:return {'unchanged':True,'revision':current}
        groupmap={g['key']:g for g in groups};summaries={f"{j['root']}:{j['number']}":j for j in jobs}
        rows=db.execute(f'''SELECT m.id,m.created,m.private_to,m.message_kind,m.summary_root,m.summary_round,m.recalled_at,
            {ROOT} AS root,{ROUND} AS number,m.round_mode,m.delivery_mode,s.name AS sender,s.kind,
            CASE WHEN m.recalled_at>0 THEN '消息已撤回' ELSE substr(m.body,1,220) END AS preview FROM messages m JOIN members s ON s.id=m.sender
            WHERE m.room=? AND m.message_kind IN ('chat','recovery_request','recovery_followup','session_change_request','session_change_result') ORDER BY m.id''',(room,)).fetchall()
        blocks=[];segment=0
        for r in rows:
            r=dict(r)
            if r['message_kind'] in ('recovery_request','recovery_followup','session_change_request','session_change_result'):r['sender']='系统调度'
            if r['message_kind']=='recovery_request':r['preview']=r['preview'].split('\n\n',1)[0]
            if r['kind']=='user' and r['message_kind']=='chat':segment=r['id']
            kind='private' if r['message_kind'] in ('recovery_request','session_change_request','session_change_result') else 'human' if r['kind']=='user' else 'private' if r['private_to'] else 'round'
            key=f"{r['root']}:{r['number']}" if kind=='round' else ''
            # Preserve chronological order, including private messages inserted mid-round.
            if kind=='round' and blocks and blocks[-1]['type']==kind and blocks[-1]['round_key']==key and blocks[-1]['segment']==segment:
                block=blocks[-1];block['ids'].append(r['id']);block['last']=r['id']
                if r['sender'] not in block['senders']:block['senders'].append(r['sender'])
                continue
            group=groupmap.get(key,{})
            block={'key':f"b{r['id']}",'type':kind,'round_key':key,'root':r['root'],'number':r['number'],
                   'mode':r['round_mode'],'segment':segment,'ids':[r['id']],'first':r['id'],'last':r['id'],
                   'created':r['created'],'private_to':r['private_to'],'message_kind':r['message_kind'],'recalled_at':r['recalled_at'],'preview':r['preview'],'senders':[r['sender']],
                   'complete':group.get('complete',True),'round_count':group.get('count',0)}
            blocks.append(block)
        safe_summaries={key:{k:j[k] for k in ('status','error','manager_name','result','body','source_count')} for key,j in summaries.items()}
        return {'revision':current,'blocks':blocks,'summaries':safe_summaries,'rounds':groups,'recalls':recalls}


def messages(store, room, actor, ids):
    from .core import Problem
    if not ids or len(ids)>200 or any(not isinstance(i,int) or i<1 for i in ids):raise Problem('每次读取 1–200 个消息 ID')
    with store.tx() as db:
        owner(store,db,room,actor)
        rows=db.execute('SELECT * FROM messages WHERE room=? AND message_kind!=\'summary_request\' AND id IN ('+','.join('?' for _ in ids)+') ORDER BY id',(room,*ids)).fetchall()
        return [store.message_dict(db,r,actor) for r in rows]


def search(store, room, actor, query, before=0, scope='all', sender='', limit=40):
    from .core import Problem
    query=query.strip()
    if not query or len(query)>200:raise Problem('请输入 1–200 个字符')
    if scope not in ('all','public','private'):raise Problem('未知的搜索范围')
    with store.tx() as db:
        owner(store,db,room,actor)
        where=["m.room=?","m.message_kind!='summary_request'","m.recalled_at=0","instr(lower(m.body),lower(?))>0"]
        args=[room,query]
        if before:where.append('m.id<?');args.append(before)
        if scope!='all':where.append("m.private_to"+('=' if scope=='public' else '!=')+"''")
        if sender:where.append('s.id=?');args.append(sender)
        rows=db.execute(f'''SELECT m.id,m.body,m.created,m.private_to,m.message_kind,m.summary_root,m.summary_round,
            {ROOT} AS root,{ROUND} AS number,s.name AS sender FROM messages m JOIN members s ON s.id=m.sender
            WHERE {' AND '.join(where)} ORDER BY m.id DESC LIMIT ?''',(*args,limit+1)).fetchall()
        results=[]
        for row in rows[:limit]:
            r=dict(row);body=r.pop('body');pos=body.lower().find(query.lower());start=max(0,pos-65)
            r['excerpt']=('…' if start else '')+body[start:max(start+200,pos+len(query)+65)]+('…' if len(body)>max(start+200,pos+len(query)+65) else '')
            results.append(r)
        return {'results':results,'more':len(rows)>limit,'next':results[-1]['id'] if results else None}


def request_summary(store, room, actor, root, number):
    from .core import Problem
    with store.tx() as db:
        room_info=owner(store,db,room,actor)
        if room_info['paused']:raise Problem('群聊已暂停',409)
        group=next((g for g in rounds(db,room) if g['root']==root and g['number']==number),None)
        if not group or group['count']<=5 or not group['complete']:raise Problem('仅已结束且超过 5 条消息的轮次可生成摘要',409)
        job=db.execute('SELECT * FROM round_summaries WHERE root=? AND number=?',(root,number)).fetchone()
        if job and job['status'] in ('queued','running','done'):return {'ok':True}
        now=time.time()
        db.execute('''INSERT INTO round_summaries(root,number,room,source_end,source_count,created,updated) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(root,number) DO UPDATE SET source_end=excluded.source_end,source_count=excluded.source_count''',(root,number,room,group['last'],group['count'],now,now))
        enqueue(store,db,group,room,explicit=True)
        return {'ok':True}
