"""Durable room state and routing. All writes use a single SQLite transaction."""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

from bridge.names import NAME_PATTERN, normalize_name
from bridge.roles import worker_roles
from . import steering, permissions, synchronous, reading, recall, recovery, session_changes, guests, workspaces, lifecycle, uploads

LEASE_SECONDS=90


class Problem(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


def token():
    return secrets.token_urlsafe(32)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt=None):
    if not 12 <= len(password) <= 1024:
        raise Problem('密码长度需为 12–1024 个字符')
    salt = salt or secrets.token_hex(16)
    return salt + ':' + hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def password_ok(password, stored):
    try:
        return hmac.compare_digest(password_hash(password, stored.split(':')[0]), stored)
    except (ValueError, Problem):
        return False


def valid_name(value):
    try:
        return normalize_name(value)
    except ValueError as exc:
        raise Problem(str(exc)) from exc


def valid_model(value):
    value = value.strip()
    if value and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@+\[\]\-]{0,199}', value):
        raise Problem('模型 ID 最多 200 位，以字母或数字开头，可含 . _ : / @ + -')
    return value


def is_manager(actor):
    return actor.get('kind')=='agent' and actor.get('session_type')=='manager'


def manages_member(actor, member):
    return guests.owner(actor,member['room']) or (actor['kind']=='bridge' and member.get('bridge')==actor['id']) or (
        is_manager(actor) and member.get('bridge')==actor.get('bridge') and member['kind']=='agent')


def visible_message(actor, row):
    return not row['private_to'] or guests.owner(actor,row['room']) or (is_manager(actor) and row['private_to']==actor['id'])


def valid_credential(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',value):
        raise Problem('接入凭证格式无效')
    return value


def valid_runtime(value, harness=''):
    value=value or {}
    if not isinstance(value,dict):raise Problem('运行信息必须是对象')
    result={}
    for key,limit in (('model',200),('think_level',80)):
        text=value.get(key,'')
        if not isinstance(text,str) or len(text)>limit or any(ord(c)<32 for c in text):
            raise Problem('无效运行信息：'+key)
        result[key]=text.strip()
    result['harness']=harness
    return result


def recipients(text):
    """Only contiguous mentions at the start are recipients; body mentions are inert."""
    remaining = text.lstrip()
    names = []
    while remaining.startswith('@'):
        match = re.match(r'@('+NAME_PATTERN+r')(?=$|[\s,，、:：;；.!！?？。])', remaining)
        if not match:
            raise Problem('无效的 @ 名称；名称后请用空格或标点分隔')
        names.append(match[1].lower())
        remaining = remaining[match.end():].lstrip(' \t,，、:：;；')
    if not names:
        raise Problem('消息必须以 @收件人 开头，也可使用 @all')
    if not remaining.strip():
        raise Problem('请填写消息正文')
    return list(dict.fromkeys(names))


SCHEMA = '''
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,name TEXT UNIQUE,password TEXT,created REAL);
CREATE TABLE IF NOT EXISTS logins(hash TEXT PRIMARY KEY,user_id TEXT,expires REAL);
CREATE TABLE IF NOT EXISTS rooms(id TEXT PRIMARY KEY,name TEXT,owner TEXT,join_hash TEXT UNIQUE,paused INTEGER DEFAULT 0,created REAL);
CREATE TABLE IF NOT EXISTS members(id TEXT PRIMARY KEY,room TEXT,name TEXT,kind TEXT,role TEXT,harness TEXT DEFAULT '',bridge TEXT,
 token_hash TEXT UNIQUE,enabled INTEGER DEFAULT 0,paused INTEGER DEFAULT 0,revoked INTEGER DEFAULT 0,status TEXT DEFAULT 'dormant',
 last_seen REAL DEFAULT 0,native_id TEXT DEFAULT '',detail TEXT DEFAULT '',created REAL,UNIQUE(room,name));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,room TEXT,sender TEXT,body TEXT,targets TEXT,
 reply_to INTEGER,cause INTEGER,root INTEGER,depth INTEGER DEFAULT 0,nonce TEXT,created REAL,UNIQUE(room,sender,nonce));
CREATE INDEX IF NOT EXISTS messages_room ON messages(room,id);
CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,room TEXT,member TEXT,message INTEGER,status TEXT DEFAULT 'pending',
 lease TEXT,lease_until REAL DEFAULT 0,attempts INTEGER DEFAULT 0,error TEXT DEFAULT '',created REAL,UNIQUE(member,message));
CREATE INDEX IF NOT EXISTS deliveries_pending ON deliveries(member,status,created);
CREATE TABLE IF NOT EXISTS agent_settings(member TEXT PRIMARY KEY,model TEXT NOT NULL DEFAULT '',
 revision INTEGER NOT NULL DEFAULT 1,applied_revision INTEGER NOT NULL DEFAULT 0,
 applied_model TEXT NOT NULL DEFAULT '',supported INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS role_templates(id TEXT PRIMARY KEY,owner TEXT NOT NULL,name TEXT NOT NULL,
 roles TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,created REAL NOT NULL,updated REAL NOT NULL,
 UNIQUE(owner,name));
CREATE TABLE IF NOT EXISTS setup_profiles(room TEXT PRIMARY KEY,workspace TEXT NOT NULL,roles TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS setup_requests(id TEXT PRIMARY KEY,room TEXT NOT NULL,nonce TEXT NOT NULL,
 token_hash TEXT NOT NULL,workspace TEXT NOT NULL,roles TEXT NOT NULL,profile_revision INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',bridge TEXT,claim_id TEXT,detail TEXT NOT NULL DEFAULT '',
 created REAL NOT NULL,expires REAL NOT NULL,started REAL,completed REAL,token_rotated INTEGER NOT NULL DEFAULT 0,
 UNIQUE(room,nonce));
CREATE INDEX IF NOT EXISTS setup_requests_room ON setup_requests(room,created);
'''


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as db:
            db.executescript(SCHEMA)
            db.executescript(uploads.SCHEMA)
            db.executescript(guests.SCHEMA)
            db.executescript(permissions.SCHEMA)
            db.executescript(workspaces.SCHEMA)
            db.executescript(lifecycle.SCHEMA)
            db.executescript(synchronous.SCHEMA)
            db.executescript(recovery.SCHEMA)
            db.executescript(session_changes.SCHEMA)
            steering.initialize(db)
            for table,columns in {
                'rooms':{'max_rounds':'INTEGER NOT NULL DEFAULT 8','round_mode':"TEXT NOT NULL DEFAULT 'global'",'session_rounds':"TEXT NOT NULL DEFAULT '{}'",'session_speaks':"TEXT NOT NULL DEFAULT '{}'"},
                'members':{'human_role':"TEXT NOT NULL DEFAULT ''",'runtime':"TEXT NOT NULL DEFAULT '{}'",'session_type':"TEXT NOT NULL DEFAULT 'worker'",'disabled_reason':"TEXT NOT NULL DEFAULT ''",'steer_supported':'INTEGER NOT NULL DEFAULT 0','sync_supported':'INTEGER NOT NULL DEFAULT 0','uploads_supported':'INTEGER NOT NULL DEFAULT 0',
                           'deleted_at':'REAL NOT NULL DEFAULT 0','lifecycle_supported':'INTEGER NOT NULL DEFAULT 0','summary_supported':'INTEGER NOT NULL DEFAULT 0','bubble_color':"TEXT NOT NULL DEFAULT ''",'avatar':"TEXT NOT NULL DEFAULT ''",'claims_paused_until':'REAL NOT NULL DEFAULT 0','departing':'INTEGER NOT NULL DEFAULT 0'},
                'messages':{'max_rounds':'INTEGER NOT NULL DEFAULT 8','stopped':'INTEGER NOT NULL DEFAULT 0',
                            'runtime':"TEXT NOT NULL DEFAULT '{}'",'recipient_names':"TEXT NOT NULL DEFAULT ''",'private_to':"TEXT NOT NULL DEFAULT ''",
                            'round_mode':"TEXT NOT NULL DEFAULT 'global'",'session_rounds':"TEXT NOT NULL DEFAULT '{}'",
                            'delivery_mode':"TEXT NOT NULL DEFAULT 'normal'",'steer_task':"TEXT NOT NULL DEFAULT ''",'sync_round':'INTEGER NOT NULL DEFAULT 0','session_speaks':"TEXT NOT NULL DEFAULT '{}'",
                            'message_kind':"TEXT NOT NULL DEFAULT 'chat'",'summary_root':'INTEGER','summary_round':'INTEGER','recalled_at':'REAL NOT NULL DEFAULT 0'},
                'deliveries':{'round_index':'INTEGER NOT NULL DEFAULT 0','sync_root':'INTEGER','sync_round':'INTEGER NOT NULL DEFAULT 0',
                              'recovery_mode':"TEXT NOT NULL DEFAULT ''",'recovery_id':"TEXT NOT NULL DEFAULT ''"},
                'agent_settings':{'catalog':"TEXT NOT NULL DEFAULT '[]'",'catalog_source':"TEXT NOT NULL DEFAULT ''",'catalog_updated':'REAL NOT NULL DEFAULT 0','catalog_refresh':'REAL NOT NULL DEFAULT 0','reasoning_effort':'TEXT','timeout_seconds':'INTEGER'},
                'setup_profiles':{'inherit_manager':'INTEGER NOT NULL DEFAULT 0','project_name':"TEXT NOT NULL DEFAULT ''"},
                'agent_permissions':{'source':"TEXT NOT NULL DEFAULT 'session'"},
                'bridge_projects':{'workspace_supported':'INTEGER NOT NULL DEFAULT 0'},
                'session_changes':{'source':"TEXT NOT NULL DEFAULT 'owner'",'removal_mode':"TEXT NOT NULL DEFAULT 'legacy'"},
                'setup_requests':{'manager_required':'INTEGER NOT NULL DEFAULT 0','inherit_manager':'INTEGER NOT NULL DEFAULT 0',
                                  'project_name':"TEXT NOT NULL DEFAULT ''",'resolved_workspace':"TEXT NOT NULL DEFAULT ''",'resolved_project_name':"TEXT NOT NULL DEFAULT ''"},
            }.items():
                existing={r['name'] for r in db.execute('PRAGMA table_info('+table+')')}
                for name,definition in columns.items():
                    if name not in existing:db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
            reading.initialize(db)

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def member(self, db, member_id):
        row = db.execute('SELECT * FROM members WHERE id=? AND revoked=0', (member_id,)).fetchone()
        if not row:
            raise Problem('成员不存在或已撤销', 404)
        return dict(row)

    def room(self, db, room_id):
        row = db.execute('SELECT * FROM rooms WHERE id=?', (room_id,)).fetchone()
        if not row:
            raise Problem('群聊不存在', 404)
        return dict(row)

    def identity(self, bearer=None, cookie=None):
        with self.tx() as db:
            if bearer:
                row = db.execute('SELECT * FROM members WHERE token_hash=? AND revoked=0', (digest(bearer),)).fetchone()
                if row:
                    return dict(row)
            elif cookie:
                row = db.execute('SELECT u.* FROM logins l JOIN users u ON u.id=l.user_id WHERE l.hash=? AND l.expires>?', (digest(cookie), time.time())).fetchone()
                if row:
                    return {'id': row['id'], 'name': row['name'], 'kind': 'user'}
        raise Problem('请登录或提供有效 token', 401)

    def access(self, db, room_id, actor, manage=True):
        room = self.room(db, room_id)
        if actor['kind'] == 'user':
            actor['_access_room']=room_id
            actor['_room_role']=''
            if room['owner']==actor['id']:actor['_room_role']='owner'
            else:
                member=db.execute("SELECT human_role FROM members WHERE id=? AND room=? AND kind='user' AND revoked=0",(actor['id']+':'+room_id,room_id)).fetchone()
                if member:actor['_room_role']=member['human_role']
            if not actor['_room_role'] or (manage and actor['_room_role']!='owner'):
                raise Problem('无权访问这个群聊', 403)
        elif actor['room'] != room_id or (manage and actor['kind'] != 'bridge' and not is_manager(actor)):
            raise Problem('权限不足', 403)
        return room

    def create_room(self, actor, name):
        if actor['kind'] != 'user':
            raise Problem('只有用户能创建群聊', 403)
        if not name.strip() or len(name) > 100:
            raise Problem('群名需为 1–100 个字符')
        room_id, join = secrets.token_hex(12), token()
        with self.tx() as db:
            db.execute('INSERT INTO rooms(id,name,owner,join_hash,created) VALUES(?,?,?,?,?)', (room_id,name.strip(),actor['id'],digest(join),time.time()))
            db.execute('INSERT INTO members(id,room,name,kind,role,enabled,status,created) VALUES(?,?,?,?,?,?,?,?)', (actor['id']+':'+room_id,room_id,actor['name'],'user','管理员',1,'online',time.time()))
        return {'id': room_id, 'name': name.strip(), 'join_token': join}

    def templates(self, actor):
        if actor['kind'] != 'user':
            raise Problem('需要管理员登录', 403)
        with self.tx() as db:
            rows = db.execute('SELECT id,name,roles,revision,created,updated FROM role_templates WHERE owner=? ORDER BY updated DESC,id', (actor['id'],))
            return [{**dict(row), 'roles': json.loads(row['roles'])} for row in rows]

    def save_template(self, actor, name, roles, template_id=None, revision=None):
        if actor['kind'] != 'user':
            raise Problem('需要管理员登录', 403)
        name = name.strip()
        if not 1 <= len(name) <= 100:
            raise Problem('模板名称需为 1–100 个字符')
        try:
            roles = worker_roles(roles)
        except ValueError as exc:
            raise Problem(str(exc)) from exc
        now = time.time()
        with self.tx() as db:
            if template_id:
                current = db.execute('SELECT revision FROM role_templates WHERE id=? AND owner=?', (template_id, actor['id'])).fetchone()
                if not current:
                    raise Problem('模板不存在', 404)
                if revision != current['revision']:
                    raise Problem('模板已在别处更新，请重新载入后再保存', 409)
            duplicate = db.execute('SELECT id FROM role_templates WHERE owner=? AND name=?', (actor['id'], name)).fetchone()
            if duplicate and duplicate['id'] != template_id:
                raise Problem('已有同名模板，请载入后更新，或使用其他名称', 409)
            if template_id:
                db.execute('UPDATE role_templates SET name=?,roles=?,revision=revision+1,updated=? WHERE id=? AND owner=?', (name,json.dumps(roles,ensure_ascii=False),now,template_id,actor['id']))
            else:
                template_id = secrets.token_hex(12)
                db.execute('INSERT INTO role_templates(id,owner,name,roles,created,updated) VALUES(?,?,?,?,?,?)', (template_id,actor['id'],name,json.dumps(roles,ensure_ascii=False),now,now))
            saved = dict(db.execute('SELECT id,name,roles,revision,created,updated FROM role_templates WHERE id=?', (template_id,)).fetchone())
            saved['roles'] = json.loads(saved['roles'])
            return saved

    def join(self, join, name, machine):
        name = valid_name(name)
        with self.tx() as db:
            room = db.execute('SELECT * FROM rooms WHERE join_hash=?', (digest(join),)).fetchone()
            if not room:
                raise Problem('群聊 token 无效或已轮换', 401)
            if db.execute('SELECT 1 FROM members WHERE room=? AND name=?', (room['id'],name)).fetchone():
                raise Problem('这个副管理员名称已存在；使用原配置恢复，或换一个名称', 409)
            mid, secret = secrets.token_hex(12), token()
            db.execute('INSERT INTO members(id,room,name,kind,role,token_hash,enabled,status,detail,created) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (mid,room['id'],name,'bridge','副管理员',digest(secret),1,'online',machine[:500],time.time()))
            return {'room': {'id': room['id'], 'name': room['name']}, 'member_id': mid, 'token': secret, 'name': name}

    def delete_template(self, actor, template_id, revision):
        if actor['kind']!='user':raise Problem('需要管理员登录',403)
        with self.tx() as db:
            row=db.execute('SELECT revision FROM role_templates WHERE id=? AND owner=?',(template_id,actor['id'])).fetchone()
            if not row:raise Problem('模板不存在',404)
            if revision!=row['revision']:raise Problem('模板已更新，请重新载入后再删除',409)
            db.execute('DELETE FROM role_templates WHERE id=? AND owner=?',(template_id,actor['id']))
        return {'ok':True}

    def round_settings(self, db, room_id, mode, limits):
        if mode not in ('global','individual','synchronous'):raise Problem('未知轮数模式')
        if not isinstance(limits,dict) or len(limits)>100:raise Problem('无效的 session 轮数设置')
        members={r['id'] for r in db.execute("SELECT id FROM members WHERE room=? AND kind='agent' AND revoked=0",(room_id,))}
        if any(k not in members or type(v)!=int or not 0<=v<=10000 for k,v in limits.items()):raise Problem('每个 session 的轮数需为 0–10000，且属于当前群聊')
        return limits

    def set_round_limit(self, room_id, actor, max_rounds, round_mode='global', session_rounds=None, session_speaks=None):
        if actor['kind']!='user':raise Problem('需要管理员登录',403)
        if type(max_rounds)!=int or not 0<=max_rounds<=10000:raise Problem('最大轮数需为 0–10000 的整数')
        with self.tx() as db:
            self.access(db,room_id,actor)
            limits=self.round_settings(db,room_id,round_mode,session_rounds or {})
            speaks=synchronous.validate_allowances(db,room_id,session_speaks) if session_speaks is not None else json.loads(self.room(db,room_id)['session_speaks'])
            db.execute('UPDATE rooms SET max_rounds=?,round_mode=?,session_rounds=?,session_speaks=? WHERE id=?',(max_rounds,round_mode,json.dumps(limits),json.dumps(speaks),room_id))
        return {'max_rounds':max_rounds,'round_mode':round_mode,'session_rounds':limits,'session_speaks':speaks}

    def stop_all(self, room_id, actor):
        if actor['kind']!='user':raise Problem('需要管理员登录',403)
        with self.tx() as db:
            self.access(db,room_id,actor)
            db.execute('UPDATE messages SET stopped=1 WHERE room=? AND cause IS NULL',(room_id,))
            count=db.execute("UPDATE deliveries SET status='cancelled',error='管理员停止了本轮协作' WHERE room=? AND status IN ('pending','running')",(room_id,)).rowcount
            count+=synchronous.stop(db,room_id)
            session_changes.cancel_room(db,room_id)
            db.execute("UPDATE members SET status=CASE WHEN enabled=1 THEN 'idle' ELSE 'dormant' END,detail='' WHERE room=? AND kind='agent' AND revoked=0",(room_id,))
        return {'ok':True,'cancelled':count}

    def setup_summary(self, db, room_id):
        row=db.execute('SELECT id,status,bridge,detail,created,expires,started,completed,token_rotated,profile_revision,resolved_workspace,resolved_project_name FROM setup_requests WHERE room=? ORDER BY created DESC,id DESC LIMIT 1',(room_id,)).fetchone()
        if not row:return None
        result=dict(row)
        if result['status']=='pending' and result['expires']<time.time():result['status']='expired'
        return result

    def setup_profile(self, room_id, actor):
        if actor['kind']!='user':raise Problem('需要管理员登录',403)
        with self.tx() as db:
            room=self.access(db,room_id,actor)
            row=db.execute('SELECT workspace,project_name,inherit_manager,roles,revision,updated FROM setup_profiles WHERE room=?',(room_id,)).fetchone()
            profile={**dict(row),'inherit_manager':bool(row['inherit_manager']),'roles':json.loads(row['roles'])} if row else {'workspace':'','project_name':'','inherit_manager':True,'roles':[],'revision':0,'updated':None}
            if row and not row['inherit_manager'] and not profile['project_name']:profile['project_name']=room['name']
            return {**profile,'setup':self.setup_summary(db,room_id)}

    def save_setup(self, room_id, actor, workspace, roles, revision, nonce=None, credential=None, inherit_manager=False, project_name=None):
        if actor['kind']!='user':raise Problem('需要管理员登录',403)
        workspace='' if inherit_manager else workspace.strip()
        if not inherit_manager and (not workspace or len(workspace)>2000 or any(c in workspace for c in '\0\r\n')):
            raise Problem('请填写有效的本地 workspace 路径')
        if inherit_manager:project_name=''
        elif project_name is not None:
            project_name=project_name.strip()
            if not project_name or len(project_name)>200 or any(ord(c)<32 for c in project_name):raise Problem('请填写有效的 project 名称')
        try:roles=worker_roles(roles)
        except ValueError as exc:raise Problem(str(exc)) from exc
        encoded=json.dumps(roles,ensure_ascii=False)
        if nonce:valid_credential(credential)
        with self.tx() as db:
            room=self.access(db,room_id,actor)
            # Older clients omitted the project name and used the room name.
            if project_name is None:project_name=room['name']
            if nonce:
                previous=db.execute('SELECT * FROM setup_requests WHERE room=? AND nonce=?',(room_id,nonce)).fetchone()
                if previous:
                    previous_name=previous['project_name'] or ('' if previous['inherit_manager'] else room['name'])
                    if previous['workspace']!=workspace or bool(previous['inherit_manager'])!=inherit_manager or previous_name!=project_name or json.loads(previous['roles'])!=roles or not hmac.compare_digest(previous['token_hash'],digest(credential)):
                        raise Problem('这次复制请求已被其他配置使用',409)
                    return {'id':previous['id'],'revision':previous['profile_revision'],'status':previous['status'],'created':previous['created']}
            current=db.execute('SELECT * FROM setup_profiles WHERE room=?',(room_id,)).fetchone()
            actual=current['revision'] if current else 0
            if revision!=actual:raise Problem('接入配置已在别处更新，请重新载入后再保存',409)
            changed=not current or current['workspace']!=workspace or bool(current['inherit_manager'])!=inherit_manager or current['project_name']!=project_name or json.loads(current['roles'])!=roles
            new_revision=actual+int(changed)
            now=time.time()
            if changed:
                db.execute('INSERT INTO setup_profiles(room,workspace,project_name,inherit_manager,roles,revision,updated) VALUES(?,?,?,?,?,?,?) ON CONFLICT(room) DO UPDATE SET workspace=excluded.workspace,project_name=excluded.project_name,inherit_manager=excluded.inherit_manager,roles=excluded.roles,revision=excluded.revision,updated=excluded.updated',(room_id,workspace,project_name,int(inherit_manager),encoded,new_revision,now))
            if not nonce:return {'revision':new_revision,'updated':now if changed else current['updated']}
            request_id=secrets.token_hex(12)
            db.execute("UPDATE setup_requests SET status='superseded' WHERE room=? AND status='pending'",(room_id,))
            db.execute('UPDATE rooms SET join_hash=? WHERE id=?',(digest(credential),room_id))
            db.execute('INSERT INTO setup_requests(id,room,nonce,token_hash,workspace,project_name,inherit_manager,roles,profile_revision,created,expires,manager_required) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)',(request_id,room_id,nonce,digest(credential),workspace,project_name,int(inherit_manager),encoded,new_revision,now,now+7*86400))
            return {'id':request_id,'revision':new_revision,'status':'pending','created':now}

    def setup_request_access(self, db, request_id, credential):
        row=db.execute('SELECT * FROM setup_requests WHERE id=?',(request_id,)).fetchone()
        if not row:raise Problem('接入链接无效或已失效',401)
        room=self.room(db,row['room']);hashed=digest(credential)
        if row['bridge']:
            bridge=db.execute("SELECT * FROM members WHERE id=? AND kind='bridge' AND revoked=0",(row['bridge'],)).fetchone()
            if bridge and hmac.compare_digest(bridge['token_hash'],hashed):return dict(row),room
        if row['status'] not in ('pending','running') or row['expires']<time.time() or not hmac.compare_digest(row['token_hash'],hashed) or not hmac.compare_digest(room['join_hash'],hashed):
            raise Problem('接入链接已失效，请由管理员复制新的链接',401)
        return dict(row),room

    def setup_request_config(self, request_id, credential):
        with self.tx() as db:
            request,room=self.setup_request_access(db,request_id,credential)
            return {'id':request['id'],'room':{'id':room['id'],'name':room['name']},'workspace':request['workspace'],
                    'inherit_manager':bool(request['inherit_manager']),'project_name':request['project_name'] or ('' if request['inherit_manager'] else room['name']),
                    'resolved_workspace':request['resolved_workspace'],'resolved_project_name':request['resolved_project_name'],
                    'roles':json.loads(request['roles']),'status':request['status'],'profile_revision':request['profile_revision'],
                    'manager_policy':'setup-agent' if request['manager_required'] else 'legacy'}

    def claim_setup(self, request_id, credential, name, machine, claim_id, bridge_token, workspace=None, project_name=None):
        name=valid_name(name);valid_credential(bridge_token)
        with self.tx() as db:
            request,room=self.setup_request_access(db,request_id,credential)
            if request['inherit_manager'] and (not workspace or not project_name):
                raise Problem('请使用最新版客户端读取 manager 的 workspace 和所属 project 名称后接入')
            workspace=(workspace if workspace is not None else request['resolved_workspace'] or request['workspace']).strip()
            project_name=(project_name if project_name is not None else request['resolved_project_name'] or request['project_name'] or room['name']).strip()
            if not workspace or len(workspace)>2000 or any(c in workspace for c in '\0\r\n'):raise Problem('接入 workspace 无效')
            if not project_name or len(project_name)>200 or any(ord(c)<32 for c in project_name):raise Problem('接入 project 名称无效')
            if not request['inherit_manager'] and project_name!=(request['project_name'] or room['name']):raise Problem('project 名称与接入配置不一致',409)
            if request['bridge']:
                bridge=self.member(db,request['bridge'])
                if request['claim_id']!=claim_id or not hmac.compare_digest(bridge['token_hash'],digest(bridge_token)):
                    raise Problem('这个链接已被另一个 setup 进程领取',409)
                if request['resolved_workspace'] and (workspace!=request['resolved_workspace'] or project_name!=request['resolved_project_name']):
                    raise Problem('这次接入已绑定其他 workspace 或 project，请使用原配置恢复',409)
            else:
                existing=db.execute('SELECT * FROM members WHERE room=? AND name=?',(room['id'],name)).fetchone()
                if existing:
                    if existing['kind']!='bridge' or existing['revoked'] or not hmac.compare_digest(existing['token_hash'] or '',digest(bridge_token)):
                        raise Problem('副管理员名称已存在，请使用原 bridge 配置恢复，或指定新的名称',409)
                    bridge=dict(existing)
                else:
                    mid=secrets.token_hex(12)
                    db.execute('INSERT INTO members(id,room,name,kind,role,token_hash,enabled,status,detail,created) VALUES(?,?,?,?,?,?,?,?,?,?)',(mid,room['id'],name,'bridge','副管理员',digest(bridge_token),1,'online',machine[:500],time.time()))
                    bridge=self.member(db,mid)
                db.execute("UPDATE setup_requests SET bridge=?,claim_id=?,status='running',started=?,resolved_workspace=?,resolved_project_name=? WHERE id=?",(bridge['id'],claim_id,time.time(),workspace,project_name,request_id))
            return {'id':request_id,'member_id':bridge['id'],'name':bridge['name'],'room':{'id':room['id'],'name':room['name']},'workspace':workspace,'project_name':project_name}

    def report_setup(self, request_id, actor, detail):
        with self.tx() as db:
            row=db.execute('SELECT * FROM setup_requests WHERE id=?',(request_id,)).fetchone()
            if not row or actor['kind']!='bridge' or row['bridge']!=actor['id']:raise Problem('无权回报这次接入',403)
            if row['status']=='running':db.execute('UPDATE setup_requests SET detail=? WHERE id=?',(detail[:1000],request_id))
        return {'ok':True}

    def complete_setup(self, request_id, actor):
        with self.tx() as db:
            row=db.execute('SELECT * FROM setup_requests WHERE id=?',(request_id,)).fetchone()
            if not row or actor['kind']!='bridge' or row['bridge']!=actor['id']:raise Problem('无权完成这次接入',403)
            if row['status']=='complete':return {'ok':True,'completed':row['completed'],'token_rotated':bool(row['token_rotated'])}
            if row['status']!='running':raise Problem('这次接入已结束',409)
            bridge=self.member(db,actor['id']);room=self.room(db,row['room'])
            expected=json.loads(row['roles']);now=time.time()
            if bridge['paused'] or room['paused']:raise Problem('请先恢复 bridge 和群聊',409)
            if bridge['last_seen']<max(row['started'],now-45):raise Problem('等待 bridge 确认在线',425)
            manager=None
            if row['manager_required']:
                manager=db.execute("SELECT * FROM members WHERE bridge=? AND session_type='manager' AND revoked=0",(bridge['id'],)).fetchone()
                if not manager:raise Problem('setup agent 尚未登记为 manager；请使用链接中的最新版客户端继续接入',409)
                if manager['last_seen']<max(row['started'],now-45):raise Problem('等待 manager 心跳',425)
            for role in expected:
                member=db.execute("SELECT * FROM members WHERE room=? AND name=? AND bridge=? AND kind='agent' AND revoked=0",(row['room'],role['name'],bridge['id'])).fetchone()
                if not member or member['role']!=role['role'] or member['harness']!=role['harness'] or member['session_type']!=role.get('session_type','worker'):
                    raise Problem('接入配置尚未完整注册：'+role['name'],409)
                if member['last_seen']<max(row['started'],now-45):raise Problem('等待 session 心跳：'+role['name'],425)
            # A late completion must not invalidate a newer invitation.
            rotated=db.execute('UPDATE rooms SET join_hash=? WHERE id=? AND join_hash=?',(digest(token()),row['room'],row['token_hash'])).rowcount>0
            db.execute("UPDATE setup_requests SET status='complete',detail='',completed=?,token_rotated=? WHERE id=?",(now,int(rotated),request_id))
            owner=db.execute("SELECT name FROM members WHERE room=? AND id=(SELECT owner||':'||id FROM rooms WHERE id=members.room)",(row['room'],)).fetchone()
            manager_note=f"；@{manager['name']} 已就绪，可单独向它发送私有管理任务" if manager else ''
            self.post_in(db,row['room'],bridge,f"@{owner['name']} 本地 setup 已完成，{len(expected)} 个协作 session 已接入{manager_note}。未启用的 session 等待你的首次 @；本次接入链接已失效。",'setup-complete:'+request_id)
            return {'ok':True,'completed':now,'token_rotated':rotated}

    def add_session(self, actor, name, role, harness, model='', model_control=False, credential=None, session_type='worker', change_id='', permission_source='', permission_mode='inherit'):
        if actor['kind'] != 'bridge':
            raise Problem('需要 bridge token', 403)
        name = valid_name(name)
        if credential:valid_credential(credential)
        model = valid_model(model)
        if session_type not in ('worker','manager'):raise Problem('无效的 session 类型')
        if not role.strip() or len(role) > 4000 or not harness.strip() or len(harness)>80:
            raise Problem('必须提供分工和 harness（分工最多 4000 字）')
        with self.tx() as db:
            desired=session_changes.registration(db,actor,change_id,name,role,harness,model) if change_id else None
            if desired and desired.get('permission_source')=='workspace':permission_source='workspace'
            if change_id and session_type!='worker':raise Problem('只能注册批准的 worker',403)
            existing=db.execute('SELECT * FROM members WHERE room=? AND name=?',(actor['room'],name)).fetchone()
            if existing:
                if existing['deleted_at']:raise Problem('此名称属于已删除的 session；请在群聊管理中恢复，或使用其他名称',409)
                if credential and not existing['revoked'] and existing['bridge']==actor['id'] and existing['role']==role and existing['harness']==harness and existing['session_type']==session_type and hmac.compare_digest(existing['token_hash'] or '',digest(credential)):
                    if desired:session_changes.registered(db,change_id,existing['id'],desired)
                    result={'id':existing['id'],'name':name,'token':credential}
                    if permission_source:result['permission_settings']=workspaces.register_permissions(db,existing['id'],actor['id'],harness,permission_source,permission_mode)
                    return result
                raise Problem('session 名称已存在；请使用原配置恢复',409)
            mid, secret = secrets.token_hex(12), credential or token()
            if session_type=='manager' and db.execute("SELECT 1 FROM members WHERE bridge=? AND session_type='manager' AND revoked=0",(actor['id'],)).fetchone():raise Problem('此 bridge 已有 manager',409)
            db.execute('INSERT INTO members(id,room,name,kind,role,harness,bridge,token_hash,created,session_type) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (mid,actor['room'],name,'agent',role,harness,actor['id'],digest(secret),time.time(),session_type))
            if model_control or model:
                db.execute('INSERT INTO agent_settings(member,model,supported) VALUES(?,?,?)',(mid,model,int(model_control)))
            if desired:session_changes.registered(db,change_id,mid,desired)
            result={'id': mid, 'name': name, 'token': secret}
            if permission_source:result['permission_settings']=workspaces.register_permissions(db,mid,actor['id'],harness,permission_source,permission_mode)
            return result

    def set_model(self, room_id, member_id, actor, model, revision, reasoning_effort=None, timeout_seconds=None):
        model = valid_model(model)
        if reasoning_effort is not None and reasoning_effort not in ('','none','minimal','low','medium','high','xhigh','max'):raise Problem('不支持的思考等级')
        if timeout_seconds is not None and (type(timeout_seconds)!=int or (timeout_seconds!=0 and not 10<=timeout_seconds<=86400)):raise Problem('超时需为 0（不限时）或 10–86400 秒')
        with self.tx() as db:
            self.access(db,room_id,actor,manage=True)
            member = self.member(db,member_id)
            if member['room']!=room_id or member['kind']!='agent' or not manages_member(actor,member):
                raise Problem('无权修改此 agent 的模型',403)
            current = db.execute('SELECT * FROM agent_settings WHERE member=?',(member_id,)).fetchone()
            if not current or not current['supported']:
                raise Problem('此 agent 尚未支持网页模型设置，请更新本地 bridge；自定义命令需提供 {model} 参数',409)
            if current['revision']!=revision:
                raise Problem('模型配置已被其他页面修改，请重新载入后再保存',409)
            effort=current['reasoning_effort'] if reasoning_effort is None else reasoning_effort
            timeout=current['timeout_seconds'] if timeout_seconds is None else timeout_seconds
            if current['model']!=model or current['reasoning_effort']!=effort or current['timeout_seconds']!=timeout:
                db.execute('UPDATE agent_settings SET model=?,reasoning_effort=?,timeout_seconds=?,revision=revision+1 WHERE member=?',(model,effort,timeout,member_id))
            return dict(db.execute('SELECT model,revision FROM agent_settings WHERE member=?',(member_id,)).fetchone())

    def control_member(self, room_id, member_id, actor, action):
        if action in ('delete','restore'):return self.session_lifecycle(room_id,member_id,actor,action)
        with self.tx() as db:
            self.access(db,room_id,actor,manage=True)
            member=self.member(db,member_id)
            if member['room']!=room_id or member['kind']=='user' or not manages_member(actor,member):raise Problem('权限不足',403)
            if action not in ('pause','resume','disable','enable','revoke'):raise Problem('未知操作')
            ids=[member_id]
            if member['kind']=='bridge':ids += [r['id'] for r in db.execute('SELECT id FROM members WHERE bridge=?',(member_id,))]
            enabled=action in ('resume','enable')
            for mid in ids:
                if action=='revoke':db.execute("UPDATE members SET revoked=1,token_hash=NULL,status='revoked' WHERE id=?",(mid,))
                else:db.execute('UPDATE members SET paused=?,disabled_reason=? WHERE id=?',(int(not enabled),'' if enabled else 'manual',mid))
                if not enabled:db.execute("UPDATE deliveries SET status='cancelled',error='管理员关闭或撤销了此 session' WHERE member=? AND status IN ('pending','running')",(mid,))
            synchronous.settle(db,room_id)
        return {'ok':True}

    def session_lifecycle(self, room_id, member_id, actor, action, _db=None):
        if actor['kind']!='user':raise Problem('只有群聊管理员可以删除或恢复 session',403)
        with (nullcontext(_db) if _db is not None else self.tx()) as db:
            self.access(db,room_id,actor)
            member=db.execute("SELECT * FROM members WHERE id=? AND room=? AND kind='agent'",(member_id,room_id)).fetchone()
            if not member:raise Problem('session 不存在',404)
            if action=='delete':
                if member['revoked']:return {'ok':True}
                # Keep the hashed credential only for an explicit owner restore.
                # revoked=1 rejects it at every authentication boundary immediately.
                db.execute("UPDATE members SET deleted_at=?,revoked=1,enabled=0,paused=1,status='deleted',disabled_reason='deleted' WHERE id=?",(time.time(),member_id))
                db.execute("UPDATE deliveries SET status='cancelled',error='管理员删除了此 session' WHERE member=? AND status IN ('pending','running','bundled','round_waiting')",(member_id,))
                room=self.room(db,room_id)
                for field in ('session_rounds','session_speaks'):
                    limits=json.loads(room[field]);limits.pop(member_id,None)
                    db.execute('UPDATE rooms SET '+field+'=? WHERE id=?',(json.dumps(limits),room_id))
                synchronous.settle(db,room_id);steering.settle(db)
            else:
                if not member['revoked']:return {'ok':True,'requires_bridge_restart':not member['lifecycle_supported']}
                if not member['deleted_at'] or not member['token_hash']:raise Problem('此 session 的凭证已撤销，无法直接恢复；请重新接入',409)
                bridge=db.execute("SELECT id FROM members WHERE id=? AND revoked=0",(member['bridge'],)).fetchone()
                if not bridge:raise Problem('此 session 的 bridge 已撤销，无法恢复',409)
                if member['session_type']=='manager' and db.execute("SELECT 1 FROM members WHERE bridge=? AND session_type='manager' AND revoked=0",(member['bridge'],)).fetchone():
                    raise Problem('此 bridge 已有另一个 manager，请先处理当前 manager',409)
                db.execute("UPDATE members SET deleted_at=0,revoked=0,enabled=0,paused=0,status='dormant',disabled_reason='',last_seen=0,detail='' WHERE id=?",(member_id,))
                return {'ok':True,'requires_bridge_restart':not member['lifecycle_supported']}
        return {'ok':True}

    def used_rounds(self, db, root, member_id):
        return db.execute('SELECT COALESCE(MAX(d.round_index),0) AS used FROM deliveries d JOIN messages m ON m.id=d.message WHERE COALESCE(m.root,m.id)=? AND d.member=?',(root,member_id)).fetchone()['used']

    def message_dict(self, db, row, actor=None):
        msg = dict(row)
        if actor and actor['kind']=='user' and msg['message_kind']=='recovery_request':
            msg['body']=msg['body'].split('\n\n',1)[0]
        sender = db.execute('SELECT name,kind,harness FROM members WHERE id=?',(msg['sender'],)).fetchone()
        msg['sender_name'], msg['sender_kind'] = sender['name'],sender['kind']
        msg['runtime']={**json.loads(msg['runtime']),'harness':sender['harness']}
        msg['session_rounds']=json.loads(msg['session_rounds'])
        msg['session_speaks']=json.loads(msg['session_speaks'])
        if actor and not guests.owner(actor,msg['room']):
            hidden={r['id'] for r in db.execute("SELECT id FROM members WHERE room=? AND session_type='manager' AND id!=?",(msg['room'],actor['id']))}
            msg['session_rounds']={k:v for k,v in msg['session_rounds'].items() if k not in hidden}
        msg.pop('recipient_names',None)
        msg['targets'] = json.loads(msg['targets'])
        if not msg['recalled_at'] and (files := uploads.metadata(db, msg['id'])):
            msg['attachments'] = files
        if msg['reply_to']:
            quote = db.execute('SELECT m.id,m.body,m.created,s.name FROM messages m JOIN members s ON s.id=m.sender WHERE m.id=? AND m.room=?',(msg['reply_to'],msg['room'])).fetchone()
            original=db.execute('SELECT * FROM messages WHERE id=?',(msg['reply_to'],)).fetchone()
            msg['quote'] = dict(quote) if quote and (not actor or (visible_message(actor,original) and synchronous.readable(db,actor,original))) else None
            if msg['quote'] and original['recalled_at']:
                msg['quote']['body']=recall.NOTICE;msg['quote']['recalled_at']=original['recalled_at']
            elif msg['quote'] and (files := uploads.metadata(db, original['id'])):
                msg['quote'].update(attachments=files, private_to=original['private_to'])
        msg['deliveries'] = [dict(x) for x in db.execute('SELECT d.id,d.member,s.name,d.status,d.error,d.attempts FROM deliveries d JOIN members s ON s.id=d.member WHERE d.message=?',(msg['id'],))]
        for delivery in msg['deliveries']:
            delivery['steer_option']=steering.offer(db,actor,row,delivery)
            if delivery['status']=='failed':delivery['retryable']=bool(actor and guests.owner(actor,msg['room']) and msg['message_kind']!='session_change_request' and synchronous.retryable(db,delivery['id']))
        msg['steers']=steering.receipts(db,msg['id'])
        msg['recall_option']=recall.option(db,actor,row)
        if msg['recalled_at']:
            msg['body']=recall.NOTICE;msg['quote']=None;msg['targets']=[]
        return msg

    def post_in(self, db, room_id, actor, body, nonce, reply_to=None, cause=None, to=None, max_rounds=None, runtime=None, round_mode=None, session_rounds=None, delivery_mode='normal', steer_task='', session_speaks=None, _summary=False, _recovery=False, attachment_ids=None):
        room = self.access(db, room_id, actor,manage=False)
        if actor['kind']=='user' and actor['_room_role']=='observer':raise Problem('观察者只能阅读；请联系创建者调整为参与者',403)
        if attachment_ids and actor['kind']!='user':raise Problem('只有群聊用户可以发送附件',403)
        synchronous.settle(db,room_id)
        steering.settle(db)
        if delivery_mode not in ('normal','steer'):raise Problem('未知的消息投递模式')
        if delivery_mode=='normal' and steer_task:raise Problem('普通消息不能绑定 Steer 任务')
        if len(body)>24000:
            raise Problem('单条消息最多 24000 字')
        if any(x is not None for x in (to,max_rounds,round_mode,session_rounds,session_speaks)) and actor['kind']!='user':raise Problem('只有管理员能指定收件选择和轮数',403)
        if round_mode is not None or session_rounds is not None:self.round_settings(db,room_id,round_mode or room['round_mode'],session_rounds or {})
        if session_speaks is not None:synchronous.validate_allowances(db,room_id,session_speaks)
        if to is not None:
            if not isinstance(to,list) or not 1<=len(to)<=100 or any(not isinstance(n,str) or not re.fullmatch(NAME_PATTERN,n) for n in to):raise Problem('无效的收件人选择')
            names=list(dict.fromkeys(n.lower() for n in to))
            if not body.strip():raise Problem('请填写消息正文')
        else:names = recipients(body)
        if delivery_mode=='steer' and (len(names)!=1 or names[0] in ('all','everyone','所有人','全体')):
            raise Problem('Steer 请单独选择一个 agent；不能广播给所有人')
        if max_rounds is not None and (type(max_rounds)!=int or not 0<=max_rounds<=10000):raise Problem('最大轮数需为 0–10000 的整数')
        sender = actor['id']+':'+room_id if actor['kind']=='user' else actor['id']
        existing = db.execute('SELECT * FROM messages WHERE room=? AND sender=? AND nonce=?',(room_id,sender,nonce)).fetchone()
        if existing:
            if [file['id'] for file in uploads.metadata(db, existing['id'])] != (attachment_ids or []):
                raise Problem('幂等键已被其他附件使用', 409)
            if session_speaks is not None and session_speaks!=json.loads(existing['session_speaks']):raise Problem('幂等键已被其他发言额度使用',409)
            prior_names=json.loads(existing['recipient_names']) if existing['recipient_names'] else recipients(existing['body'])
            if existing['body'] != body or existing['reply_to'] != reply_to or existing['delivery_mode']!=delivery_mode or existing['steer_task']!=steer_task or (cause is not None and existing['cause'] != cause) or names!=prior_names or (max_rounds is not None and max_rounds!=existing['max_rounds']) or (round_mode is not None and round_mode!=existing['round_mode']) or (session_rounds is not None and session_rounds!=json.loads(existing['session_rounds'])):
                raise Problem('幂等键已被其他内容使用',409)
            return self.message_dict(db,existing,actor)
        if actor['kind']=='agent':
            current = self.member(db, actor['id'])
            if not current['enabled'] or current['paused'] or room['paused']:
                raise Problem('此 session 尚未启用或已暂停',403)
            count = db.execute('SELECT COUNT(*) FROM messages WHERE sender=? AND created>?',(sender,time.time()-60)).fetchone()[0]
            if count>=20:
                raise Problem('发送过快，请稍后再试',429)
            # All agent replies in a working turn inherit its causal chain, even if
            # the caller omits cause. This prevents accidental autonomous loops.
            last = db.execute("SELECT message FROM deliveries WHERE member=? ORDER BY CASE WHEN status='running' THEN 0 WHEN status IN ('done','failed') THEN 1 ELSE 2 END,created DESC LIMIT 1",(sender,)).fetchone()
            cause = cause or (last['message'] if last else None)
            if not cause or not db.execute('SELECT 1 FROM deliveries WHERE member=? AND message=?',(sender,cause)).fetchone():
                raise Problem('agent 消息必须关联已接收的任务',403)
        for reference in (reply_to,cause):
            if reference:
                ref=db.execute('SELECT * FROM messages WHERE id=? AND room=?',(reference,room_id)).fetchone()
                if not ref or ref['recalled_at'] or not visible_message(actor,ref) or not synchronous.readable(db,actor,ref):raise Problem('引用已撤回、不可见、不属于当前群聊，或尚未到下一同步轮次')
        parent = db.execute('SELECT * FROM messages WHERE id=?',(cause,)).fetchone() if cause else None
        if parent and parent['message_kind']=='session_change_request':
            raise Problem('session 变更请使用专用管理工具，结果由 bridge 回报',409)
        if parent and parent['message_kind']=='summary_request' and not _summary:
            raise Problem('自动摘要仅接受最终回复，不另行发送消息或分派任务',409)
        if parent and parent['message_kind']=='recovery_request' and not _recovery:
            raise Problem('异常处理仅接受最终决定，不另行发送重复的工作指令',409)
        depth = parent['depth']+1 if parent else 0
        root = parent['root'] or parent['id'] if parent else None
        limit=parent['max_rounds'] if parent else (room['max_rounds'] if max_rounds is None else max_rounds)
        mode=parent['round_mode'] if parent else (round_mode or room['round_mode'])
        limits=json.loads(parent['session_rounds']) if parent else (session_rounds if session_rounds is not None else json.loads(room['session_rounds']))
        speaks=json.loads(parent['session_speaks']) if parent else (session_speaks if session_speaks is not None else json.loads(room['session_speaks']))
        if root and db.execute('SELECT stopped FROM messages WHERE id=?',(root,)).fetchone()['stopped']:
            raise Problem('本轮协作已停止，请等待管理员发送新任务',409)
        meta=valid_runtime(runtime if runtime is not None else json.loads(actor.get('runtime','{}')),actor.get('harness','')) if actor['kind']=='agent' else {}
        members = [dict(r) for r in db.execute('SELECT * FROM members WHERE room=? AND revoked=0',(room_id,))]
        by_name = {m['name']:m for m in members if guests.owner(actor,room_id) or is_manager(actor) or (m['session_type']!='manager' and m['kind']!='bridge')}
        owner = next(m for m in members if m['id']==room['owner']+':'+room_id)
        targets = {}
        for name in names:
            if name in ('all','everyone','所有人','全体'):
                targets.update({m['id']:m for m in members if m['id']!=sender and m['session_type']!='manager' and m['kind']!='bridge'})
            else:
                target = owner if name in ('admin','管理员') else guests.reply_recipient(db,actor,parent,name) or by_name.get(name)
                if not target:
                    raise Problem('收件人不存在：@'+name)
                targets[target['id']]=target
        managers=[m for m in targets.values() if m['session_type']=='manager']
        private_to=actor['id'] if is_manager(actor) else (managers[0]['id'] if managers else '')
        if managers and (not guests.owner(actor,room_id) or len(managers)!=1 or any(m['id'] not in (owner['id'],private_to) for m in targets.values())):
            raise Problem('manager 只能由管理员单独点名，不能与 worker 或所有人混发')
        if is_manager(actor) and any(m['id']!=owner['id'] for m in targets.values()):raise Problem('manager 回复仅管理员可见，请发送给 @管理员')
        for reference in (parent,db.execute('SELECT * FROM messages WHERE id=?',(reply_to,)).fetchone() if reply_to else None):
            if reference and reference['private_to'] and reference['private_to']!=private_to:raise Problem('不能将 manager 私聊转发或引用到其他对话',403)
        target_names = [t['name'] for t in targets.values()]
        if attachment_ids:
            outdated = [m['name'] for m in targets.values() if m['kind']=='agent'
                        and not m['paused'] and not m['departing'] and not m['uploads_supported']]
            if outdated:
                raise Problem('请先更新并重启这些 session 的 Bridge，再发送附件：'+', '.join(outdated), 409)
        steered=steering.validate(db,room,actor,list(targets.values()),body,steer_task) if delivery_mode=='steer' else None
        if actor['kind']=='user' and not private_to and steered is None and (current_sync:=synchronous.active(db,room_id)):
            if round_mode is None:mode='synchronous'
            if max_rounds is None:limit=current_sync['maximum']
            if session_speaks is None:speaks=json.loads(current_sync['allowances'])
        plan=synchronous.prepare(db,room,actor,parent,mode,limit,speaks,private_to,delivery_mode,list(targets.values()))
        mode=plan['mode']
        if private_to and (round_mode=='synchronous' or room['round_mode']=='synchronous'):
            limit=0;limits={};speaks={}
        if steered is not None and steered['sync_root']:
            root=steered['sync_root'];mode='synchronous';plan['round']=steered['sync_round']
        if mode=='synchronous' and steered is None and plan['root']:
            root=plan['root']
            if actor['kind']=='user':cause=root
        message_id = db.execute('INSERT INTO messages(room,sender,body,targets,reply_to,cause,root,depth,nonce,created,max_rounds,runtime,recipient_names,round_mode,session_rounds,private_to) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                               (room_id,sender,body,json.dumps(target_names),reply_to,cause,root,depth,nonce,time.time(),limit,json.dumps(meta),json.dumps(names),mode,json.dumps(limits),private_to)).lastrowid
        if attachment_ids:
            uploads.bind(db, room_id, sender, message_id, attachment_ids)
        db.execute('UPDATE messages SET sync_round=?,session_speaks=? WHERE id=?',(plan['round'],json.dumps(speaks),message_id))
        if steered is not None:
            db.execute('UPDATE messages SET delivery_mode=?,steer_task=? WHERE id=?',(delivery_mode,steer_task,message_id))
            steering.record(db,message_id,steered)
            return self.message_dict(db,db.execute('SELECT * FROM messages WHERE id=?',(message_id,)).fetchone(),actor)
        for mid, member in targets.items():
            if member['kind']!='agent' or mid==sender:
                continue
            if actor['kind']=='user' and not member['enabled']:
                db.execute("UPDATE members SET enabled=1,status='idle' WHERE id=?",(mid,))
                member['enabled']=1
            reason = ''
            state = 'pending'
            if not member['enabled']:
                state,reason='skipped','等待管理员首次 @'
            elif member['departing']:
                state,reason='skipped','session 正在退出托管，不再接收新任务'
            elif member['paused'] or room['paused']:
                state,reason='skipped',('rate limit 已自动关闭' if member['disabled_reason']=='rate_limit' else 'session 已关闭') if member['paused'] else '群聊已暂停'
            elif mode=='individual' and limits.get(mid,limit) and self.used_rounds(db,root or message_id,mid)>=limits.get(mid,limit):
                state,reason='skipped','此 session 已达到本轮个人阈值，不再接收点名'
            elif mode=='global' and actor['kind']!='user' and limit and depth>=limit:
                state,reason='skipped','已达到本轮上限，最终决定与总结已记录，不再继续触发'
            db.execute('INSERT INTO deliveries(id,room,member,message,status,error,created) VALUES(?,?,?,?,?,?,?)',
                       (secrets.token_hex(12),room_id,mid,message_id,state,reason,time.time()))
        synchronous.posted(db,message_id,actor,plan,limit,speaks)
        return self.message_dict(db,db.execute('SELECT * FROM messages WHERE id=?',(message_id,)).fetchone(),actor)

    def post(self, room, actor, **kwargs):
        with self.tx() as db:
            result=self.post_in(db, room, actor, **kwargs)
            reading.settle(self,db,room)
            return result

    def snapshot(self, room_id, actor):
        with self.tx() as db:
            room = self.access(db,room_id,actor,manage=False)
            room.pop('join_hash')
            synchronous.settle(db,room_id)
            steering.settle(db)
            if actor['kind']=='user':reading.settle(self,db,room_id)
            session_changes.settle(db,room_id)
            members = [dict(m) for m in db.execute('SELECT id,name,kind,role,human_role,harness,bridge,enabled,paused,revoked,status,last_seen,native_id,detail,runtime,session_type,disabled_reason,steer_supported,sync_supported,created,deleted_at,lifecycle_supported,bubble_color,avatar,departing,token_hash IS NOT NULL AS has_credential FROM members WHERE room=?',(room_id,))]
            members=[m for m in members if m['session_type']!='manager' or guests.owner(actor,room_id) or m['id']==actor['id'] or (actor['kind']=='bridge' and m['bridge']==actor['id'])]
            guest=actor['kind']=='user' and not guests.owner(actor,room_id)
            if guest:members=[m for m in members if m['kind']!='bridge' and not m['revoked']]
            room['session_rounds']={k:v for k,v in json.loads(room['session_rounds']).items() if k in {m['id'] for m in members}}
            room['session_speaks']={k:v for k,v in json.loads(room['session_speaks']).items() if k in {m['id'] for m in members}}
            for m in members:
                if m['kind']=='user':m['human_role']='owner' if m['id']==room['owner']+':'+room_id else m['human_role']
                m['restorable']=bool(m['deleted_at'] and m.pop('has_credential') and any(b['id']==m['bridge'] and not b['revoked'] for b in members))
                m.pop('has_credential',None)
                m['runtime']=json.loads(m['runtime'])
                m['online'] = m['kind']=='user' or time.time()-m['last_seen']<45
                if m['kind']=='agent':
                    if guests.owner(actor,room_id) or (is_manager(actor) and m['bridge']==actor['bridge']):
                        m['lifecycle']=lifecycle.view(lifecycle.current(db,m['id']))
                        cap=db.execute('SELECT workspace_supported FROM bridge_projects WHERE bridge=?',(m['bridge'],)).fetchone()
                        m['workspace_supported']=bool(cap and cap[0])
                    active=db.execute("SELECT d.id,d.attempts,d.message FROM deliveries d WHERE d.member=? AND d.status='running' AND d.lease_until>=?",(m['id'],time.time())).fetchone()
                    m['active_task']={'ref':active['id']+':'+str(active['attempts']),'message':active['message']} if active else None
                    settings = db.execute('SELECT model,revision,applied_revision,applied_model,supported,catalog,catalog_source,catalog_updated,reasoning_effort,timeout_seconds FROM agent_settings WHERE member=?',(m['id'],)).fetchone()
                    m['model_settings'] = dict(settings) if settings else {'model':'','revision':0,'applied_revision':0,'applied_model':'','supported':0}
                    m['model_settings']['catalog']=json.loads(m['model_settings'].get('catalog','[]'))
                    m['permission_settings']=permissions.snapshot(db,m)
                if guest:
                    for field in ('native_id','bridge','permission_settings','model_settings','detail','restorable','lifecycle','workspace_supported'):m.pop(field,None)
            return {'room':room,'members':members,'viewer':{'role':actor.get('_room_role',''),'member_id':actor['id']+':'+room_id if actor['kind']=='user' else actor['id']},'synchronous':synchronous.view(db,room_id,actor),**({'setup':self.setup_summary(db,room_id)} if guests.owner(actor,room_id) else {})}

    def history(self, room_id, actor, after=0, before=None, limit=100, channel=None, through=None):
        with self.tx() as db:
            self.access(db,room_id,actor,manage=False)
            synchronous.settle(db,room_id)
            steering.settle(db)
            visibility="(private_to='' OR private_to=?)";params=[actor['id'] if is_manager(actor) else '']
            if guests.owner(actor,room_id):visibility='1=1';params=[]
            if actor['kind']!='user':visibility+=' AND recalled_at=0'
            if channel is not None:
                if channel and not (guests.owner(actor,room_id) or (is_manager(actor) and channel==actor['id'])):raise Problem('无权读取此管理会话',403)
                visibility+=' AND private_to=?';params.append(channel)
            if run:=synchronous.gate(db,actor):
                visibility+=' AND (id<=? OR sender=? OR id IN (SELECT message FROM steers WHERE member=?))'
                params.extend((run['cutoff'],actor['id'],actor['id']))
            if through is not None:
                visibility+=' AND id<=?';params.append(through)
            if before:
                rows = list(reversed(db.execute('SELECT * FROM messages WHERE room=? AND '+visibility+' AND id<? ORDER BY id DESC LIMIT ?',(room_id,*params,before,limit)).fetchall()))
            else:
                rows = db.execute('SELECT * FROM messages WHERE room=? AND '+visibility+' AND id>? ORDER BY id LIMIT ?',(room_id,*params,after,limit)).fetchall()
            return [self.message_dict(db,r,actor) for r in rows]

    def claim(self, actor, member_id):
        if actor['kind']!='bridge':
            raise Problem('需要 bridge token',403)
        with self.tx() as db:
            m = self.member(db,member_id)
            room = self.room(db,m['room'])
            if m['bridge']!=actor['id']:
                raise Problem('不能领取其他 bridge 的任务',403)
            synchronous.settle(db,m['room'])
            reading.settle(self,db,m['room'])
            bridge=self.member(db,actor['id'])
            # During an upgrade, busy workers can still enlist their peers. The
            # first claim after the bridge becomes idle holds the restart window
            # atomically. Expiry self-clears if the updater disconnects.
            if bridge['paused']:
                return None
            if bridge['claims_paused_until']>time.time() and not db.execute("SELECT 1 FROM deliveries d JOIN members b ON b.id=d.member WHERE b.bridge=? AND d.status='running' LIMIT 1",(actor['id'],)).fetchone():
                return None
            if not m['enabled'] or m['paused'] or room['paused'] or m['departing']:
                return None
            # Expiration is an uncertain result, for the manager to assess before
            # deciding whether to resume or replay work with possible side effects.
            expired=[d['id'] for d in db.execute("SELECT id FROM deliveries WHERE member=? AND status='running' AND lease_until<?",(member_id,time.time()))]
            db.execute("UPDATE deliveries SET status='failed',error='服务器任务租约已到期；可沿用原 session 继续' WHERE member=? AND status='running' AND lease_until<?",(member_id,time.time()))
            for did in expired:recovery.queue(db,did)
            recovery.reconcile(db,m['room'])
            session_changes.settle(db,m['room'])
            if db.execute("SELECT 1 FROM deliveries WHERE member=? AND status='running'",(member_id,)).fetchone():
                return None
            while True:
                row = db.execute("SELECT d.* FROM deliveries d JOIN messages m ON m.id=d.message WHERE d.member=? AND d.status='pending' AND m.recalled_at=0 ORDER BY (m.message_kind='recovery_request') DESC,(m.message_kind='summary_request'),d.created LIMIT 1",(member_id,)).fetchone()
                if not row:return None
                message=self.message_dict(db,db.execute('SELECT * FROM messages WHERE id=?',(row['message'],)).fetchone(),m)
                maximum=message['max_rounds'];current=message['depth']+1
                if row['sync_root']:
                    if not m['sync_supported']:return None
                    current=row['sync_round']
                if message['round_mode']=='individual':
                    maximum=message['session_rounds'].get(member_id,maximum)
                    current=row['round_index'] or (self.used_rounds(db,message['root'] or message['id'],member_id)+1)
                    if maximum and current>maximum:
                        db.execute("UPDATE deliveries SET status='skipped',error='此 session 已达到本轮个人阈值' WHERE id=?",(row['id'],));continue
                break
            lease = token()
            db.execute("UPDATE deliveries SET status='running',lease=?,lease_until=?,attempts=attempts+1,round_index=? WHERE id=?",(digest(lease),time.time()+LEASE_SECONDS,current,row['id']))
            synchronous.claimed(db,row)
            db.execute("UPDATE members SET status='running',last_seen=?,detail='' WHERE id=?",(time.time(),member_id))
            settings = db.execute('SELECT model,revision,reasoning_effort,timeout_seconds FROM agent_settings WHERE member=?',(member_id,)).fetchone()
            return {'id':row['id'],'lease':lease,'lease_seconds':LEASE_SECONDS,'message':message,'model_settings':dict(settings) if settings else None,
                    'permission_settings':permissions.desired(db,member_id),'recovery_mode':row['recovery_mode'],
                    **({'error_recovery':recovery.payload(db,message['id'])} if message['message_kind']=='recovery_request' else {}),
                    **({'session_change':session_changes.payload(db,message['id'])} if message['message_kind']=='session_change_request' else {}),
                    **({'round_summary':reading.payload(db,message)} if message['message_kind']=='summary_request' else {}),
                    **({'synchronous':synchronous.payload(self,db,row,m)} if row['sync_root'] else {}),
                    'round_budget':{'mode':message['round_mode'],'current':current,'max':maximum,'remaining':max(0,maximum-current+1) if maximum else None,'last':bool(maximum and current>=maximum)}}

    def delivery(self, db, actor, delivery_id, lease):
        row = db.execute('SELECT d.*,m.bridge FROM deliveries d JOIN members m ON m.id=d.member WHERE d.id=?',(delivery_id,)).fetchone()
        if not row or actor['kind']!='bridge' or row['bridge']!=actor['id'] or not hmac.compare_digest(row['lease'] or '',digest(lease)):
            raise Problem('无效任务租约',403)
        return dict(row)

    def finish(self, actor, delivery_id, lease, body=None, error=None, native_id='', runtime=None, error_kind=''):
        with self.tx() as db:
            d = self.delivery(db,actor,delivery_id,lease)
            if d['status']=='done':
                return {'ok':True}
            if d['status']!='running' or d['lease_until']<time.time():
                raise Problem('租约已结束；未重复执行或回传',409)
            m = self.member(db,d['member'])
            if body and not synchronous.quota_exhausted(db,d):
                if not session_changes.finish(db,d,body) and not recovery.finish(self,db,d,m,body,runtime) and not reading.finish(self,db,d,m,body,runtime):
                    self.post_in(db,d['room'],m,body,'delivery:'+delivery_id,cause=d['message'],reply_to=d['message'],runtime=runtime)
            elif not body and not error:
                raise Problem('harness 未返回消息')
            db.execute('UPDATE deliveries SET status=?,error=? WHERE id=?',('failed' if error else 'done',(error or '')[:1000],delivery_id))
            db.execute('UPDATE members SET status=?,detail=?,native_id=?,last_seen=? WHERE id=?',('error' if error else 'idle',(error or '')[:1000],native_id[:200],time.time(),m['id']))
            if error and error_kind=='rate_limit':
                db.execute("UPDATE members SET paused=1,disabled_reason='rate_limit' WHERE id=?",(m['id'],))
                db.execute("UPDATE deliveries SET status='cancelled',error='此 session 遇到 rate limit 已自动关闭' WHERE member=? AND status IN ('pending','running')",(m['id'],))
            if error:recovery.queue(db,d['id'])
            recovery.reconcile(db,d['room'])
            session_changes.settle(db,d['room'])
            synchronous.finished(db,d,error)
            synchronous.settle(db,d['room'])
            reading.settle(self,db,d['room'])
            return {'ok':True}
