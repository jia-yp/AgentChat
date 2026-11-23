"""Scoped manager tools, authorized by a live private instruction from the owner."""
import time
import uuid

from . import lifecycle, session_changes, workspaces


def access(store, db, actor, cause=None):
    from .core import Problem
    if actor['kind'] != 'agent' or actor.get('session_type') != 'manager':
        raise Problem('需要本 workspace 的 manager 身份', 403)
    room = store.access(db, actor['room'], actor, manage=True)
    cap = db.execute('SELECT * FROM bridge_projects WHERE bridge=?', (actor['bridge'],)).fetchone()
    if not cap or not cap['workspace_supported']:
        raise Problem('请先更新此 workspace 的 gateway', 409)
    if cause is None:return room,cap,None
    if not workspaces.policy(db, actor['bridge'])['manager_sessions']:
        raise Problem('创建者已关闭此 workspace 的 manager 成员管理能力', 403)
    delivery = db.execute('''SELECT d.* FROM deliveries d JOIN messages m ON m.id=d.message
        WHERE d.member=? AND d.message=? AND d.status='running' AND d.lease_until>=?
        AND m.private_to=? AND m.sender=? AND m.message_kind='chat' AND m.recalled_at=0''',
                          (actor['id'],cause,time.time(),actor['id'],room['owner']+':'+room['id'])).fetchone()
    bridge=db.execute('SELECT paused,revoked FROM members WHERE id=?',(actor['bridge'],)).fetchone()
    if not delivery or room['paused'] or actor['paused'] or actor['revoked'] or bridge['paused'] or bridge['revoked']:
        raise Problem('此操作需要正在执行的管理员私有指令；摘要、异常处理和群聊消息不能授权增减成员',403)
    return room,cap,dict(delivery)


def inventory(store, actor):
    with store.tx() as db:
        room,cap,_=access(store,db,actor)
        members=[{**dict(r),'lifecycle':lifecycle.view(lifecycle.current(db,r['id']))} for r in db.execute(
            "SELECT id,name,harness,role,native_id,enabled,paused,revoked,deleted_at,departing,session_type FROM members WHERE bridge=? AND kind='agent' ORDER BY created",(actor['bridge'],))]
        return {'workspace':cap['workspace'],'project':cap['project'],'revision':session_changes.revision(db,actor['bridge']),
                'permissions':workspaces.policy(db,actor['bridge']),'sessions':members,
                'changes':[session_changes.view(db,r) for r in db.execute('SELECT * FROM session_changes WHERE bridge=? ORDER BY created DESC LIMIT 10',(actor['bridge'],))]}


def operate(store, actor, action, cause, nonce, roles=None, members=None, immediate=False, expected=None):
    from .core import Problem
    with store.tx() as db:
        room,cap,delivery=access(store,db,actor,cause)
        if action=='create':
            if members or not roles:raise Problem('创建时需提供角色清单')
            allowed={'name','harness','role','model','reasoning_effort','timeout_seconds','max_prompt_chars','attach_native_id'}
            for role in roles:
                if set(role)-allowed:raise Problem('manager 新建角色只能继承 workspace 权限；不接受自定义命令、启动参数或权限覆盖',403)
                if role.get('harness') not in ('codex','claude','antigravity','deepseek'):raise Problem('此 harness 需由创建者通过接入配置添加')
                if native:=role.get('attach_native_id'):
                    try:uuid.UUID(native)
                    except (ValueError,TypeError,AttributeError):raise Problem('接入已有会话必须提供准确的原生 session ID')
                    if role['harness'] not in ('codex','claude'):raise Problem('此 harness 暂不支持从外部会话接续；已托管过的会话请用 rejoin')
            additions=[dict(role,permission_source='workspace') for role in roles]
            return session_changes.submit(store,room['id'],{'kind':'user','id':room['owner']},actor['id'],additions,[],
                expected or session_changes.revision(db,actor['bridge']),'manager:'+actor['id']+':'+nonce,_db=db,_source=delivery)
        if action not in ('leave','rejoin','cancel-leave') or roles or not members:
            raise Problem('请指定 create、leave、rejoin 或 cancel-leave 及对应参数')
        targets=[]
        for identifier in members:
            target=db.execute("SELECT id FROM members WHERE bridge=? AND session_type='worker' AND (id=? OR name=?)",
                              (actor['bridge'],identifier,identifier.lower())).fetchone()
            if not target:raise Problem('只能管理本 workspace 的指定 worker；manager 自身可在管理页退出',403)
            if target['id'] in targets:raise Problem('session 清单重复')
            targets.append(target['id'])
        return {'operations':[lifecycle.request_in(store,db,room['id'],mid,action,'manager:'+actor['id']+':'+nonce,immediate) for mid in targets]}
