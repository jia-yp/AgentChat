"""Owner-managed defaults for a room's local workspace (one per bridge today)."""
import json

from bridge.permission_settings import CATALOG

DEFAULTS = {'codex': 'network', 'claude': 'edits', 'antigravity': 'collaborate'}
SCHEMA = '''CREATE TABLE IF NOT EXISTS workspace_policies(
 bridge TEXT PRIMARY KEY, modes TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 manager_sessions INTEGER NOT NULL DEFAULT 1);
'''


def policy(db, bridge):
    row = db.execute('SELECT * FROM workspace_policies WHERE bridge=?', (bridge,)).fetchone()
    return {**dict(row), 'modes': json.loads(row['modes'])} if row else {
        'bridge': bridge, 'modes': dict(DEFAULTS), 'revision': 0, 'manager_sessions': True}


def ensure(db, bridge):
    db.execute('INSERT OR IGNORE INTO workspace_policies(bridge,modes) VALUES(?,?)',
               (bridge, json.dumps(DEFAULTS)))
    return policy(db, bridge)


def inventory(store, room, actor):
    from .core import Problem
    if actor['kind'] != 'user':
        raise Problem('只有群聊创建者可设置 workspace 权限', 403)
    with store.tx() as db:
        store.access(db, room, actor, manage=True)
        rows = db.execute('''SELECT b.id,b.name,b.last_seen,p.workspace,p.project,p.supported,
            p.workspace_supported FROM members b JOIN bridge_projects p ON p.bridge=b.id
            WHERE b.room=? AND b.kind='bridge' AND b.revoked=0 ORDER BY b.created''', (room,))
        return {'workspaces': [{**dict(row), **policy(db, row['id'])} for row in rows],
                'options': CATALOG}


def save(store, room, actor, bridge, modes, revision, manager_sessions):
    from .core import Problem
    if actor['kind'] != 'user':
        raise Problem('只有群聊创建者可修改 workspace 默认权限', 403)
    if set(modes) != set(DEFAULTS) or any(
            mode not in {x['id'] for x in CATALOG[harness]} for harness, mode in modes.items()):
        raise Problem('workspace 权限配置无效')
    with store.tx() as db:
        store.access(db, room, actor, manage=True)
        target = db.execute("SELECT id FROM members WHERE id=? AND room=? AND kind='bridge' AND revoked=0",
                            (bridge, room)).fetchone()
        if not target:
            raise Problem('workspace 不存在', 404)
        current = policy(db, bridge)
        if current['revision'] != revision:
            raise Problem('workspace 设置已变化，请重新载入', 409)
        cap = db.execute('SELECT workspace_supported FROM bridge_projects WHERE bridge=?', (bridge,)).fetchone()
        if not cap or not cap[0]:
            raise Problem('请先更新并重启此 workspace 的 gateway', 409)
        db.execute('''INSERT INTO workspace_policies(bridge,modes,revision,manager_sessions) VALUES(?,?,?,?)
            ON CONFLICT(bridge) DO UPDATE SET modes=excluded.modes,revision=excluded.revision,
            manager_sessions=excluded.manager_sessions''',
                   (bridge, json.dumps(modes), revision + 1, int(manager_sessions)))
        # Existing installations remain pinned unless their owner explicitly
        # selected workspace inheritance. Running jobs keep their original policy.
        for harness, mode in modes.items():
            db.execute('''UPDATE agent_permissions SET mode=?,revision=revision+1,updated_by=?
                WHERE source='workspace' AND mode!=? AND member IN
                (SELECT id FROM members WHERE bridge=? AND harness=?)''',
                       (mode, actor['id'], mode, bridge, harness))
        return policy(db, bridge)


def register_permissions(db, member, bridge, harness, source, mode):
    from .core import Problem
    if source not in ('session', 'workspace'):
        raise Problem('无效的权限来源')
    if source == 'workspace':
        mode = ensure(db, bridge)['modes'].get(harness, 'inherit')
    if mode not in {x['id'] for x in CATALOG.get(harness, [])} | {'inherit'}:
        raise Problem('此 harness 不支持该权限')
    db.execute('INSERT OR IGNORE INTO agent_permissions(member,mode,source) VALUES(?,?,?)',
               (member, mode, source))
    row = db.execute('SELECT mode,revision,source FROM agent_permissions WHERE member=?', (member,)).fetchone()
    return dict(row)
