"""Room-scoped human membership and revocable invitation links."""
import time

SCHEMA = '''
CREATE TABLE IF NOT EXISTS guest_invitations(
 room TEXT PRIMARY KEY,token TEXT UNIQUE NOT NULL,role TEXT NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS human_aliases(
 room TEXT NOT NULL,member TEXT NOT NULL,name TEXT NOT NULL,PRIMARY KEY(room,member,name));
'''
ROLES = {'observer':'观察者','participant':'参与者'}


def owner(actor, room):
    return actor.get('kind')=='user' and actor.get('_access_room')==room and actor.get('_room_role')=='owner'


def invitation(store, room, actor, role=None, rotate=False):
    from .core import Problem, token
    with store.tx() as db:
        store.access(db,room,actor,manage=True)
        if actor['kind']!='user':raise Problem('只有创建者可以邀请用户',403)
        row=db.execute('SELECT * FROM guest_invitations WHERE room=?',(room,)).fetchone()
        if role is not None:
            if role not in ROLES:raise Problem('请选择观察者或参与者')
            secret=token() if rotate or not row else row['token']
            db.execute('''INSERT INTO guest_invitations VALUES(?,?,?,?) ON CONFLICT(room)
                DO UPDATE SET token=excluded.token,role=excluded.role,updated=excluded.updated''',(room,secret,role,time.time()))
            row=db.execute('SELECT * FROM guest_invitations WHERE room=?',(room,)).fetchone()
        return dict(row) if row else None


def inspect_invitation(store, actor, secret, join=False, name=None):
    from .core import Problem, valid_name
    if actor['kind']!='user':raise Problem('请先登录注册账号',403)
    with store.tx() as db:
        link=db.execute('''SELECT i.*,r.name,r.owner FROM guest_invitations i
            JOIN rooms r ON r.id=i.room WHERE i.token=?''',(secret,)).fetchone()
        if not link:raise Problem('邀请链接已失效，请向创建者获取新链接',404)
        mid=actor['id']+':'+link['room']
        member=db.execute('SELECT * FROM members WHERE id=?',(mid,)).fetchone()
        active=bool(member and not member['revoked'])
        role='owner' if link['owner']==actor['id'] else member['human_role'] if active else link['role']
        if join and not active:
            alias=valid_name(name or (member['name'] if member else actor['name']))
            if db.execute('SELECT 1 FROM members WHERE room=? AND name=? AND id!=?',(link['room'],alias,mid)).fetchone():
                raise Problem('这个群聊已有同名成员，请换一个群聊名称',409)
            if member:
                remember_alias(db,member,alias)
                db.execute("UPDATE members SET name=?,human_role=?,role=?,revoked=0,status='online',enabled=1 WHERE id=?",(alias,role,ROLES[role],mid))
            else:
                db.execute('''INSERT INTO members(id,room,name,kind,role,human_role,enabled,status,created)
                    VALUES(?,?,?,'user',?,?,1,'online',?)''',(mid,link['room'],alias,ROLES[role],role,time.time()))
        return {'room':link['room'],'name':link['name'],'role':role,'joined':active or join,
                'member_name':member['name'] if member else name or actor['name']}


def edit_member(store, room, actor, member_id, name=None, role=None, kick=False):
    from .core import Problem, valid_name
    with store.tx() as db:
        room_info=store.access(db,room,actor,manage=False)
        if actor['kind']!='user':raise Problem('权限不足',403)
        member=store.member(db,member_id)
        if member['room']!=room or member['kind']!='user':raise Problem('成员不存在',404)
        creator=owner(actor,room)
        if not creator and member_id!=actor['id']+':'+room:raise Problem('只能修改自己的群聊资料',403)
        if role is not None or kick:
            if not creator:raise Problem('只有创建者可以管理成员权限',403)
            if member_id==room_info['owner']+':'+room:raise Problem('不能移除创建者或修改其权限')
            if role is not None and role not in ROLES:raise Problem('请选择观察者或参与者')
        if name is not None:
            alias=valid_name(name)
            if db.execute('SELECT 1 FROM members WHERE room=? AND name=? AND id!=?',(room,alias,member_id)).fetchone():raise Problem('此名称已被使用',409)
            remember_alias(db,member,alias)
            db.execute('UPDATE members SET name=? WHERE id=?',(alias,member_id))
        if role is not None:db.execute('UPDATE members SET human_role=?,role=? WHERE id=?',(role,ROLES[role],member_id))
        if kick:db.execute("UPDATE members SET revoked=1,enabled=0,status='removed' WHERE id=?",(member_id,))
        return {'ok':True}


def can_edit_appearance(actor, room, member):
    return owner(actor,room) or (actor['kind']=='user' and member['kind']=='user' and member['id']==actor['id']+':'+room)


def remember_alias(db, member, name):
    if member['name']!=name:
        db.execute('INSERT OR IGNORE INTO human_aliases VALUES(?,?,?)',(member['room'],member['id'],member['name']))


def reply_recipient(db, actor, parent, name):
    """A running task can still reply to its original human after rename/removal.

    This does not restore access or route new human messages through old aliases.
    Bind the alias to this task's sender, never to another account using that name.
    """
    if actor['kind']!='agent' or not parent:return None
    member=db.execute("SELECT * FROM members WHERE id=? AND kind='user'",(parent['sender'],)).fetchone()
    if member and (member['name']==name or db.execute('SELECT 1 FROM human_aliases WHERE room=? AND member=? AND name=?',(parent['room'],member['id'],name)).fetchone()):
        return dict(member)
    return None
