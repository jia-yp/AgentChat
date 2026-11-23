"""Owner-controlled permissions, with independent desired/applied revisions."""
from bridge.permission_settings import CATALOG

SCHEMA='''CREATE TABLE IF NOT EXISTS agent_permissions(
 member TEXT PRIMARY KEY,mode TEXT NOT NULL DEFAULT 'inherit',revision INTEGER NOT NULL DEFAULT 1,
 applied_mode TEXT NOT NULL DEFAULT 'inherit',applied_revision INTEGER NOT NULL DEFAULT 0,
 supported INTEGER NOT NULL DEFAULT 0,detail TEXT NOT NULL DEFAULT '',updated_by TEXT NOT NULL DEFAULT '');'''


def snapshot(db,member):
    from .workspaces import policy
    row=db.execute('SELECT * FROM agent_permissions WHERE member=?',(member['id'],)).fetchone()
    result=dict(row) if row else {'mode':'inherit','revision':0,'applied_mode':'inherit','applied_revision':0,'supported':False,'detail':'请更新并重启本地 bridge'}
    result.pop('updated_by',None);result.pop('member',None)
    result['options']=CATALOG.get(member['harness'],[]) if result['supported'] else []
    bridge=db.execute('SELECT bridge FROM members WHERE id=?',(member['id'],)).fetchone()[0]
    cap=db.execute('SELECT workspace_supported FROM bridge_projects WHERE bridge=?',(bridge,)).fetchone()
    result['workspace_mode']=policy(db,bridge)['modes'].get(member['harness']) if cap and cap[0] else None
    if not cap or not cap[0]:result['options']=[o for o in result['options'] if o['id']!='collaborate']
    return result


def heartbeat(db,member,item):
    info=item.get('permissions')
    if not isinstance(info,dict):
        db.execute('UPDATE agent_permissions SET supported=0,detail=? WHERE member=?',('请更新并重启本地 bridge',member['id']))
        return
    allowed={x['id'] for x in CATALOG.get(member['harness'],[])}|{'inherit'}
    mode=info.get('mode','inherit')
    supported=info.get('supported') is True and bool(CATALOG.get(member['harness'])) and isinstance(mode,str) and mode in allowed
    if not isinstance(mode,str) or mode not in allowed:mode='inherit'
    db.execute('INSERT OR IGNORE INTO agent_permissions(member,mode) VALUES(?,?)',(member['id'],mode))
    current=db.execute('SELECT * FROM agent_permissions WHERE member=?',(member['id'],)).fetchone()
    applied=info.get('revision',0)
    if type(applied)!=int or not 0<=applied<=current['revision'] or (applied==current['revision'] and mode!=current['mode']):applied=0
    db.execute('UPDATE agent_permissions SET supported=?,applied_mode=?,applied_revision=?,detail=? WHERE member=?',
        (int(supported),mode,applied,str(info.get('detail',''))[:500],member['id']))


def desired(db,member_id):
    row=db.execute('SELECT mode,revision,source FROM agent_permissions WHERE member=? AND supported=1',(member_id,)).fetchone()
    if not row:return None
    result=dict(row)
    if result['source']=='session':result.pop('source')
    return result


def save(store,actor,room_id,member_id,mode,revision,source='session'):
    from .core import Problem
    if actor['kind']!='user':raise Problem('只有群管理员可修改执行权限；agent 不能自行提权',403)
    with store.tx() as db:
        store.access(db,room_id,actor,manage=True)
        member=store.member(db,member_id)
        if member['room']!=room_id or member['kind']!='agent':raise Problem('无权修改此 session 权限',403)
        current=db.execute('SELECT * FROM agent_permissions WHERE member=?',(member_id,)).fetchone()
        if not current or not current['supported']:raise Problem('本地 bridge 尚未支持此 session 的权限管理',409)
        if source not in ('session','workspace'):raise Problem('无效的权限来源')
        if source=='workspace':
            from .workspaces import policy
            cap=db.execute('SELECT workspace_supported FROM bridge_projects WHERE bridge=?',(member['bridge'],)).fetchone()
            if not cap or not cap[0]:raise Problem('请先更新此 workspace 的 gateway',409)
            mode=policy(db,member['bridge'])['modes'].get(member['harness'],'inherit')
        if mode=='collaborate':
            cap=db.execute('SELECT workspace_supported FROM bridge_projects WHERE bridge=?',(member['bridge'],)).fetchone()
            if not cap or not cap[0]:raise Problem('请先更新此 workspace 的 gateway',409)
        if mode not in {x['id'] for x in CATALOG.get(member['harness'],[])}:raise Problem('此 harness 不支持该权限选项')
        if current['revision']!=revision:raise Problem('权限已被其他页面修改，请载入已保存权限后重试',409)
        if mode!=current['mode'] or source!=current['source']:
            db.execute('UPDATE agent_permissions SET mode=?,source=?,revision=revision+1,updated_by=? WHERE member=?',(mode,source,actor['id'],member_id))
        return desired(db,member_id)
