import os
import base64
import binascii
import hashlib
import io
import re
import json
import math
import zipfile
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse,Response,PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .core import Store, Problem, digest, password_hash, password_ok, token, valid_name, valid_model, valid_runtime, manages_member, LEASE_SECONDS
from .limits import BodyLimit
from . import permissions, synchronous, reading, session_changes, guests, workspaces, lifecycle, manager


class Login(BaseModel):
    username: str = Field(max_length=40)
    password: str = Field(max_length=1024)
    setup_code: str = ''


class Registration(BaseModel):
    username: str = Field(min_length=1,max_length=40)
    password: str = Field(min_length=12,max_length=1024)


class RoomInput(BaseModel):
    name: str = Field(min_length=1,max_length=100)


class JoinInput(BaseModel):
    name: str
    machine: str = ''


class SessionInput(BaseModel):
    name: str
    role: str
    harness: str
    model: str = Field(default='',max_length=200)
    model_control: bool = False
    credential: str | None = Field(default=None,min_length=32,max_length=128)
    session_type: str = 'worker'
    change_id: str = Field(default='',max_length=80)
    permission_source: str = Field(default='',pattern='^(|session|workspace)$')
    permission_mode: str = Field(default='inherit',max_length=40)


class SessionChangeInput(BaseModel):
    manager: str = Field(max_length=80)
    additions: list[dict] = Field(default_factory=list,max_length=100)
    removals: list[str] = Field(default_factory=list,max_length=100)
    expected: str = Field(max_length=80)
    nonce: str = Field(min_length=1,max_length=80)
    removal_mode: str = Field(default='legacy',pattern='^(legacy|finish|immediate)$')


class SessionChangeReport(BaseModel):
    error: str = Field(default='',max_length=1000)


class SessionChangeRemoval(BaseModel):
    member: str = Field(max_length=80)


class ModelInput(BaseModel):
    model: str = Field(max_length=200)
    revision: int = Field(ge=0)
    reasoning_effort: str | None = None
    timeout_seconds: int | None = Field(default=None,ge=0,le=86400)


class PermissionInput(BaseModel):
    source: str = Field(default='session',pattern='^(session|workspace)$')
    mode: str = Field(min_length=1,max_length=40)
    revision: int = Field(ge=0)


class AppearanceInput(BaseModel):
    bubble_color: str = Field(pattern=r'^(#[0-9a-fA-F]{6})?$',max_length=7)


class GuestInvitationInput(BaseModel):
    role: str = Field(pattern='^(observer|participant)$')
    rotate: bool = False


class GuestJoinInput(BaseModel):
    token: str = Field(min_length=32,max_length=128)
    name: str | None = Field(default=None,max_length=40)


class GuestMemberInput(BaseModel):
    name: str | None = Field(default=None,min_length=1,max_length=40)
    role: str | None = Field(default=None,pattern='^(observer|participant)$')
    kick: bool = False


AVATAR_ICONS = {'codex','claude','antigravity','deepseek'}
AVATAR_MAX = 24000
IMAGE_MAGIC = {'png':b'\x89PNG\r\n\x1a\n','jpeg':b'\xff\xd8\xff','webp':b'RIFF'}


class AvatarInput(BaseModel):
    avatar: str = Field(max_length=AVATAR_MAX)


def valid_avatar(value:str)->str:
    """'' (default), icon:<harness>, text:<1-2 characters>, or a small base64 PNG/JPEG/WebP data URL."""
    if value=='':return value
    if value.startswith('icon:'):
        if value[5:] in AVATAR_ICONS:return value
        raise Problem('不支持的头像图标',422)
    if value.startswith('text:'):
        text=value[5:].strip()
        if 1<=len(text)<=2 and text.isprintable() and not any(c in text for c in '<>&"\''):return 'text:'+text
        raise Problem('文字头像需为 1–2 个字符',422)
    match=re.fullmatch(r'data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/]+={0,2})',value)
    if not match:raise Problem('头像图片格式无效',422)
    try:data=base64.b64decode(match.group(2),validate=True)
    except (binascii.Error,ValueError):raise Problem('头像图片格式无效',422) from None
    kind=match.group(1)
    if not data.startswith(IMAGE_MAGIC[kind]) or (kind=='webp' and data[8:12]!=b'WEBP'):raise Problem('头像图片内容与格式不符',422)
    return value


class TemplateInput(BaseModel):
    name: str = Field(min_length=1,max_length=100)
    roles: list[dict] = Field(min_length=1,max_length=100)
    revision: int | None = Field(default=None,ge=1)


class SetupProfileInput(BaseModel):
    workspace: str = Field(default='',max_length=2000)
    inherit_manager: bool = False
    project_name: str | None = Field(default=None,max_length=200)
    roles: list[dict] = Field(min_length=1,max_length=100)
    revision: int = Field(ge=0)


class SetupInvitationInput(SetupProfileInput):
    nonce: str = Field(min_length=1,max_length=80)
    credential: str = Field(min_length=32,max_length=128)


class SetupClaimInput(BaseModel):
    name: str
    machine: str = Field(default='',max_length=500)
    claim_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    bridge_token: str = Field(min_length=32,max_length=128)
    workspace: str | None = Field(default=None,max_length=2000)
    project_name: str | None = Field(default=None,max_length=200)


class SetupReportInput(BaseModel):
    detail: str = Field(max_length=1000)


class MessageInput(BaseModel):
    body: str = Field(min_length=1,max_length=24000)
    nonce: str = Field(min_length=1,max_length=120)
    reply_to: int | None = None
    cause: int | None = None
    to: list[str] | None = Field(default=None,min_length=1,max_length=100)
    max_rounds: int | None = Field(default=None,ge=0,le=10000)
    runtime: dict | None = None
    round_mode: str | None = None
    session_rounds: dict[str,int] | None = None
    session_speaks: dict[str,int] | None = None
    delivery_mode: str = 'normal'
    steer_task: str = Field(default='',max_length=120)


class SteerClaimInput(BaseModel):
    lease: str
    nonce: str = Field(min_length=1,max_length=120)


class SteerConvertInput(BaseModel):
    task_ref: str = Field(min_length=1,max_length=120)


class SteerAckInput(SteerClaimInput):
    ids: list[str] = Field(min_length=1,max_length=8)
    status: str
    error: str = Field(default='',max_length=600)


class RoundInput(BaseModel):
    max_rounds: int = Field(ge=0,le=10000)
    round_mode: str = 'global'
    session_rounds: dict[str,int] = Field(default_factory=dict)
    session_speaks: dict[str,int] | None = None


class SyncContinueInput(BaseModel):
    root: int = Field(ge=1)
    round: int = Field(ge=1)


class ReadingMessages(BaseModel):
    ids: list[int] = Field(min_length=1,max_length=200)


class ControlInput(BaseModel):
    action: str


class HeartbeatInput(BaseModel):
    sessions: list[dict] = Field(default_factory=list,max_length=101)
    leases: list[dict] = Field(default_factory=list,max_length=101)
    session_changes_supported: bool = False
    workspace: str = Field(default='',max_length=2000)
    project_name: str = Field(default='',max_length=200)
    workspace_supported: bool = False


class WorkspaceInput(BaseModel):
    modes: dict[str,str]
    revision: int = Field(ge=0)
    manager_sessions: bool = True


class LifecycleInput(BaseModel):
    action: str = Field(pattern='^(leave|rejoin|cancel-leave)$')
    nonce: str = Field(min_length=1,max_length=120)
    immediate: bool = False


class LifecycleAck(BaseModel):
    credential: str = Field(default='',max_length=128)


class ManagerSessionsInput(BaseModel):
    action: str = Field(pattern='^(create|leave|rejoin|cancel-leave)$')
    cause: int = Field(ge=1)
    nonce: str = Field(min_length=1,max_length=80)
    roles: list[dict] = Field(default_factory=list,max_length=100)
    members: list[str] = Field(default_factory=list,max_length=100)
    immediate: bool = False
    expected: str | None = Field(default=None,max_length=80)


class FinishInput(BaseModel):
    lease: str
    body: str | None = None
    error: str | None = None
    native_id: str = ''
    runtime: dict | None = None
    error_kind: str = Field(default='',max_length=80)


def create_app(data_dir=None):
    registration_open=os.environ.get('AGENTCHAT_REGISTRATION_OPEN','1')=='1'
    public_api_url=os.environ.get('AGENTCHAT_PUBLIC_API_URL','').strip().rstrip('/')
    if public_api_url:
        parsed=urlsplit(public_api_url)
        if parsed.scheme!='https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('AGENTCHAT_PUBLIC_API_URL 必须是没有凭证、查询或 fragment 的 HTTPS 地址')
    data_dir = Path(data_dir or os.environ.get('AGENTCHAT_DATA','data')).resolve()
    data_dir.mkdir(parents=True,exist_ok=True)
    store = Store(data_dir/'agentchat.sqlite3')
    code_file = data_dir/'bootstrap-code'
    with store.tx() as db:
        initialized = bool(db.execute('SELECT 1 FROM users').fetchone())
    if not initialized and not code_file.exists():
        code_file.write_text(token(),encoding='utf-8')
        code_file.chmod(0o600)
    app = FastAPI(title='AgentChat',docs_url=None,redoc_url=None,openapi_url=None)
    app.state.store = store
    app.add_middleware(BodyLimit)
    auth_attempts, rate_lock = {},threading.Lock()
    # Fingerprint only the explicit, credential-free client source allowlist.
    root=Path(__file__).resolve().parents[1]
    client_files=[root/'agentchat.py',root/'requirements-deepseek.txt',root/'deploy/Install-BridgeTask.ps1',root/'deploy/agentchat-bridge.service.example']
    client_files+=sorted((root/'bridge').glob('*.py'))+sorted((root/'bridge').glob('*.ps1'))+sorted((root/'skills').rglob('SKILL.md'))
    client_hash=hashlib.sha256((root/'bridge/CLIENT.md').read_bytes())
    for path in client_files:
        client_hash.update(path.relative_to(root).as_posix().encode()+b'\0'+path.read_bytes())
    client_version=client_hash.hexdigest()[:16]

    @app.exception_handler(Problem)
    async def problem_handler(request,exc):
        return JSONResponse({'error':exc.message},status_code=exc.status)

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict_handler(request,exc):
        return JSONResponse({'error':'名称或记录已存在'},status_code=409)

    @app.middleware('http')
    async def security(request, call_next):
        if request.method not in ('GET','HEAD','OPTIONS'):
            if request.headers.get('content-type','').split(';')[0]!='application/json':
                return JSONResponse({'error':'请求必须使用 application/json'},415)
            origin = request.headers.get('origin')
            if origin:
                allowed = {x.strip().rstrip('/') for x in os.environ.get('AGENTCHAT_ORIGINS','').split(',') if x.strip()}
                parsed = urlsplit(origin)
                same = parsed.netloc == request.headers.get('host') and parsed.scheme in ('http','https')
                if not same and origin.rstrip('/') not in allowed:
                    return JSONResponse({'error':'不允许的请求来源'},403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if request.url.path.startswith(('/api/','/downloads/','/setup/')):
            response.headers['Cache-Control']='no-store'
        elif request.url.path in ('/','/index.html','/app.js','/style.css'):
            response.headers['Cache-Control']='no-cache'
        return response

    def actor(request):
        auth = request.headers.get('authorization','')
        return store.identity(auth[7:] if auth.startswith('Bearer ') else None,request.cookies.get('agentchat_session'))

    def user(request):
        a = actor(request)
        if a['kind']!='user':
            raise Problem('需要管理员登录',403)
        return a

    def throttle(request,bucket='auth',account=''):
        # Do not trust arbitrary forwarded client addresses supplied by callers.
        peer=request.client.host if request.client else 'unknown'
        # Reverse proxies can share a peer address: one busy account must not
        # exhaust every user's login budget. Keep a bounded peer-wide ceiling.
        checks=[((peer,'total'),60),((peer,bucket),5 if bucket=='register' else 60 if bucket=='login' else 10)]
        if account:checks.append((('account',account),10))
        with rate_lock:
            now = time.time()
            for k in list(auth_attempts):
                auth_attempts[k]=[t for t in auth_attempts[k] if now-t<60]
                if not auth_attempts[k]:
                    del auth_attempts[k]
            if any(len(auth_attempts.get(key,[]))>=limit for key,limit in checks):
                raise Problem('尝试过于频繁，请一分钟后重试',429)
            for key,_ in checks:auth_attempts.setdefault(key,[]).append(now)

    def logged_in(uid,name):
        secret = token()
        with store.tx() as db:
            db.execute('DELETE FROM logins WHERE expires<?',(time.time(),))
            db.execute('INSERT INTO logins VALUES(?,?,?)',(digest(secret),uid,time.time()+86400*7))
        response = JSONResponse({'id':uid,'name':name,'kind':'user'})
        response.set_cookie('agentchat_session',secret,max_age=86400*7,httponly=True,samesite='strict',secure=os.environ.get('AGENTCHAT_SECURE_COOKIE')=='1')
        return response

    @app.get('/healthz')
    def health():
        return {'status':'ok','service':'AgentChat'}

    @app.get('/downloads/setup-skill.md')
    def setup_skill():
        root=Path(__file__).resolve().parents[1]
        return PlainTextResponse((root/'skills/agentchat-setup/SKILL.md').read_text(encoding='utf-8'))

    @app.get('/setup/{request_id}')
    def setup_guide(request_id:str,request:Request):
        service=public_api_url or str(request.base_url).rstrip('/')
        guide=(root/'skills/agentchat-setup/SKILL.md').read_text(encoding='utf-8')
        return PlainTextResponse(f'# AgentChat setup\n\n客户端下载：{service}/downloads/agentchat-client.zip?v={client_version}\n\n'+guide)

    @app.get('/downloads/agentchat-client.zip')
    def client_bundle():
        # Explicit source allowlist: never package data, local contexts, env files,
        # credentials, logs, or the arbitrary contents of a workspace.
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('README.md',(root/'bridge/CLIENT.md').read_text(encoding='utf-8'))
            for path in client_files:
                archive.write(path,path.relative_to(root).as_posix())
        return Response(output.getvalue(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="agentchat-client.zip"'})

    @app.get('/api/status')
    def status():
        with store.tx() as db:
            initialized=bool(db.execute('SELECT 1 FROM users').fetchone())
            return {'initialized':initialized,'registration_open':initialized and registration_open,
                    'public_api_url':public_api_url,'client_version':client_version}

    @app.post('/api/bootstrap')
    def bootstrap(data:Login,request:Request):
        throttle(request,'bootstrap')
        name=valid_name(data.username)
        with store.tx() as db:
            if db.execute('SELECT 1 FROM users').fetchone():
                raise Problem('管理员已创建',409)
            if not code_file.exists() or not secrets.compare_digest(data.setup_code,code_file.read_text().strip()):
                raise Problem('初始化码错误',403)
            uid=secrets.token_hex(12)
            db.execute('INSERT INTO users VALUES(?,?,?,?)',(uid,name,password_hash(data.password),time.time()))
        code_file.unlink(missing_ok=True)
        return logged_in(uid,name)

    @app.post('/api/register')
    def register(data:Registration,request:Request):
        throttle(request,'register')
        if not registration_open:raise Problem('当前暂未开放注册',403)
        name=valid_name(data.username)
        with store.tx() as db:
            if not db.execute('SELECT 1 FROM users').fetchone():raise Problem('请等待站点完成初始化',409)
            if db.execute('SELECT 1 FROM users WHERE name=?',(name,)).fetchone():raise Problem('用户名已被使用，请换一个名称',409)
        hashed=password_hash(data.password)
        uid=secrets.token_hex(12)
        with store.tx() as db:
            db.execute('INSERT INTO users(id,name,password,created) VALUES(?,?,?,?)',(uid,name,hashed,time.time()))
        response=logged_in(uid,name);response.status_code=201
        return response

    @app.post('/api/login')
    def login(data:Login,request:Request):
        throttle(request,'login',data.username.lower().strip())
        with store.tx() as db:
            row=db.execute('SELECT * FROM users WHERE name=?',(data.username.lower().strip(),)).fetchone()
        if not row or not password_ok(data.password,row['password']):
            raise Problem('用户名或密码错误',401)
        return logged_in(row['id'],row['name'])

    @app.post('/api/logout')
    def logout(request:Request):
        with store.tx() as db:
            db.execute('DELETE FROM logins WHERE hash=?',(digest(request.cookies.get('agentchat_session','')),))
        response=JSONResponse({'ok':True})
        response.delete_cookie('agentchat_session')
        return response

    @app.get('/api/me')
    def me(request:Request):
        a=actor(request)
        return {k:a[k] for k in ('id','name','kind')}

    @app.get('/api/role-templates')
    def list_templates(request:Request):
        return store.templates(user(request))

    @app.post('/api/role-templates')
    def create_template(data:TemplateInput,request:Request):
        return store.save_template(user(request),data.name,data.roles)

    @app.put('/api/role-templates/{template_id}')
    def update_template(template_id:str,data:TemplateInput,request:Request):
        return store.save_template(user(request),data.name,data.roles,template_id,data.revision)

    @app.delete('/api/role-templates/{template_id}')
    def delete_template(template_id:str,revision:int,request:Request):
        return store.delete_template(user(request),template_id,revision)

    @app.get('/api/rooms')
    def rooms(request:Request):
        a=user(request)
        with store.tx() as db:
            return [dict(x) for x in db.execute('''SELECT r.id,r.name,r.paused,r.created,
                CASE WHEN r.owner=? THEN 'owner' ELSE m.human_role END AS role FROM rooms r
                LEFT JOIN members m ON m.id=?||':'||r.id AND m.kind='user' AND m.revoked=0
                WHERE r.owner=? OR m.human_role IN ('observer','participant') ORDER BY r.created DESC''',(a['id'],a['id'],a['id']))]

    @app.get('/api/rooms/{room_id}/guest-invitation')
    def guest_invitation(room_id:str,request:Request):
        return guests.invitation(store,room_id,user(request))

    @app.post('/api/rooms/{room_id}/guest-invitation')
    def save_guest_invitation(room_id:str,data:GuestInvitationInput,request:Request):
        return guests.invitation(store,room_id,user(request),**data.model_dump())

    @app.post('/api/guest-invitations/preview')
    def preview_guest_invitation(data:GuestJoinInput,request:Request):
        a=user(request);throttle(request,'guest-invite',a['id'])
        return guests.inspect_invitation(store,a,data.token)

    @app.post('/api/guest-invitations/join')
    def join_guest_invitation(data:GuestJoinInput,request:Request):
        a=user(request);throttle(request,'guest-invite',a['id'])
        return guests.inspect_invitation(store,a,data.token,join=True,name=data.name)

    @app.patch('/api/rooms/{room_id}/guests/{member_id}')
    def edit_guest(room_id:str,member_id:str,data:GuestMemberInput,request:Request):
        return guests.edit_member(store,room_id,user(request),member_id,**data.model_dump())

    @app.post('/api/rooms')
    def create_room(data:RoomInput,request:Request):
        return store.create_room(user(request),data.name)

    @app.get('/api/rooms/{room_id}/setup-profile')
    def get_setup_profile(room_id:str,request:Request):
        return store.setup_profile(room_id,user(request))

    @app.get('/api/rooms/{room_id}/session-changes')
    def session_inventory(room_id:str,request:Request):
        return session_changes.inventory(store,room_id,user(request))

    @app.get('/api/rooms/{room_id}/workspaces')
    def workspace_list(room_id:str,request:Request):
        return workspaces.inventory(store,room_id,actor(request))

    @app.post('/api/rooms/{room_id}/members/{member_id}/lifecycle')
    def session_transition(room_id:str,member_id:str,data:LifecycleInput,request:Request):
        return lifecycle.request(store,room_id,member_id,actor(request),**data.model_dump())

    @app.post('/api/bridge/session-transitions/{operation}/ack')
    def session_transition_ack(operation:int,data:LifecycleAck,request:Request):
        return lifecycle.acknowledge(store,actor(request),operation,**data.model_dump())

    @app.get('/api/manager/sessions')
    def manager_sessions(request:Request):
        return manager.inventory(store,actor(request))

    @app.post('/api/manager/sessions')
    def manager_sessions_action(data:ManagerSessionsInput,request:Request):
        return manager.operate(store,actor(request),**data.model_dump())

    @app.post('/api/rooms/{room_id}/workspaces/{bridge_id}')
    def workspace_save(room_id:str,bridge_id:str,data:WorkspaceInput,request:Request):
        return workspaces.save(store,room_id,actor(request),bridge_id,**data.model_dump())

    @app.post('/api/rooms/{room_id}/session-changes')
    def submit_session_change(room_id:str,data:SessionChangeInput,request:Request):
        return session_changes.submit(store,room_id,user(request),**data.model_dump())

    @app.post('/api/rooms/{room_id}/session-changes/{change_id}/control')
    def control_session_change(room_id:str,change_id:str,data:ControlInput,request:Request):
        return session_changes.control(store,room_id,user(request),change_id,data.action)

    @app.get('/api/session-changes/{change_id}')
    def get_session_change(change_id:str,request:Request):
        return session_changes.manager_action(store,actor(request),change_id)

    @app.post('/api/session-changes/{change_id}/apply')
    def apply_session_change(change_id:str,request:Request):
        return session_changes.manager_action(store,actor(request),change_id,apply=True)

    @app.get('/api/bridge/session-changes/{change_id}')
    def bridge_session_change(change_id:str,request:Request):
        a=actor(request)
        with store.tx() as db:return session_changes.view(db,session_changes.access(db,a,change_id,bridge=True))

    @app.post('/api/bridge/session-changes/{change_id}/remove')
    def remove_session_change(change_id:str,data:SessionChangeRemoval,request:Request):
        return session_changes.remove(store,actor(request),change_id,data.member)

    @app.post('/api/bridge/session-changes/{change_id}/report')
    def report_session_change(change_id:str,data:SessionChangeReport,request:Request):
        return session_changes.report(store,actor(request),change_id,data.error)

    @app.put('/api/rooms/{room_id}/setup-profile')
    def save_setup_profile(room_id:str,data:SetupProfileInput,request:Request):
        return store.save_setup(room_id,user(request),**data.model_dump())

    @app.post('/api/rooms/{room_id}/setup-invitations')
    def create_setup_invitation(room_id:str,data:SetupInvitationInput,request:Request):
        return store.save_setup(room_id,user(request),**data.model_dump())

    def setup_bearer(request):
        auth=request.headers.get('authorization','')
        if not auth.startswith('Bearer '):raise Problem('请提供接入链接中的 token',401)
        return auth[7:]

    @app.get('/api/setup/requests/{request_id}')
    def setup_config(request_id:str,request:Request):
        return {**store.setup_request_config(request_id,setup_bearer(request)),'client_version':client_version}

    @app.post('/api/setup/requests/{request_id}/claim')
    def claim_setup(request_id:str,data:SetupClaimInput,request:Request):
        return store.claim_setup(request_id,setup_bearer(request),**data.model_dump())

    @app.post('/api/bridge/setup/{request_id}/complete')
    def complete_setup(request_id:str,request:Request):
        return store.complete_setup(request_id,actor(request))

    @app.post('/api/bridge/setup/{request_id}/report')
    def report_setup(request_id:str,data:SetupReportInput,request:Request):
        return store.report_setup(request_id,actor(request),data.detail)

    @app.get('/api/rooms/{room_id}')
    def snapshot(room_id:str,request:Request):
        return store.snapshot(room_id,actor(request))

    @app.get('/api/rooms/{room_id}/messages')
    def history(room_id:str,request:Request,after:int=0,before:int|None=None,limit:int=100,channel:str|None=None,through:int|None=None):
        return store.history(room_id,actor(request),after,before,max(1,min(limit,200)),channel,through)

    @app.get('/api/rooms/{room_id}/timeline')
    def timeline(room_id:str,request:Request,revision:str=''):
        return reading.timeline(store,room_id,actor(request),revision)

    @app.post('/api/rooms/{room_id}/reading/messages')
    def reading_messages(room_id:str,data:ReadingMessages,request:Request):
        return reading.messages(store,room_id,actor(request),data.ids)

    @app.get('/api/rooms/{room_id}/search')
    def search_messages(room_id:str,request:Request,q:str,before:int=0,scope:str='all',sender:str=''):
        return reading.search(store,room_id,actor(request),q,before,scope,sender)

    @app.post('/api/rooms/{room_id}/summaries')
    def summarize_round(room_id:str,data:SyncContinueInput,request:Request):
        return reading.request_summary(store,room_id,actor(request),data.root,data.round)

    @app.post('/api/rooms/{room_id}/messages')
    def send(room_id:str,data:MessageInput,request:Request):
        return store.post(room_id,actor(request),**data.model_dump())

    @app.post('/api/rooms/{room_id}/control')
    def room_control(room_id:str,data:ControlInput,request:Request):
        a=user(request)
        if data.action=='stop-all':return store.stop_all(room_id,a)
        with store.tx() as db:
            store.access(db,room_id,a)
            if data.action=='rotate-token':
                secret=token()
                db.execute('UPDATE rooms SET join_hash=? WHERE id=?',(digest(secret),room_id))
                db.execute("UPDATE setup_requests SET status='superseded' WHERE room=? AND status='pending'",(room_id,))
                return {'join_token':secret}
            if data.action not in ('pause','resume'):
                raise Problem('未知操作')
            db.execute('UPDATE rooms SET paused=? WHERE id=?',(int(data.action=='pause'),room_id))
            if data.action=='pause':
                db.execute("UPDATE deliveries SET status='cancelled',error='群管理员暂停了群聊' WHERE room=? AND status IN ('pending','running')",(room_id,))
                synchronous.stop(db,room_id)
        return {'ok':True}

    @app.put('/api/rooms/{room_id}/round-limit')
    def room_round_limit(room_id:str,data:RoundInput,request:Request):
        return store.set_round_limit(room_id,user(request),**data.model_dump())

    @app.post('/api/rooms/{room_id}/synchronous/continue')
    def continue_sync(room_id:str,data:SyncContinueInput,request:Request):
        return synchronous.continue_failed(store,actor(request),room_id,data.root,data.round)

    @app.post('/api/rooms/{room_id}/members/{member_id}/control')
    def member_control(room_id:str,member_id:str,data:ControlInput,request:Request):
        return store.control_member(room_id,member_id,actor(request),data.action)

    @app.post('/api/rooms/{room_id}/members/{member_id}/models/refresh')
    def refresh_models(room_id:str,member_id:str,request:Request):
        a=actor(request)
        with store.tx() as db:
            store.access(db,room_id,a,manage=True);m=store.member(db,member_id)
            if m['room']!=room_id or not manages_member(a,m):raise Problem('权限不足',403)
            db.execute('UPDATE agent_settings SET catalog_refresh=? WHERE member=?',(time.time(),member_id))
        return {'ok':True}

    @app.post('/api/rooms/{room_id}/members/{member_id}/model')
    def member_model(room_id:str,member_id:str,data:ModelInput,request:Request):
        return store.set_model(room_id,member_id,actor(request),**data.model_dump())

    @app.post('/api/rooms/{room_id}/members/{member_id}/permissions')
    def member_permissions(room_id:str,member_id:str,data:PermissionInput,request:Request):
        return permissions.save(store,actor(request),room_id,member_id,**data.model_dump())

    @app.post('/api/rooms/{room_id}/members/{member_id}/appearance')
    def member_appearance(room_id:str,member_id:str,data:AppearanceInput,request:Request):
        a=user(request)
        with store.tx() as db:
            store.access(db,room_id,a,manage=False)
            member=store.member(db,member_id)
            if member['room']!=room_id or member['kind'] not in ('agent','user'):raise Problem('成员不存在',404)
            if not guests.can_edit_appearance(a,room_id,member):raise Problem('只能修改自己的群聊资料',403)
            color=data.bubble_color.lower()
            db.execute('UPDATE members SET bubble_color=? WHERE id=?',(color,member_id))
        return {'id':member_id,'bubble_color':color}

    @app.post('/api/rooms/{room_id}/members/{member_id}/avatar')
    def member_avatar(room_id:str,member_id:str,data:AvatarInput,request:Request):
        a=user(request)
        avatar=valid_avatar(data.avatar)
        with store.tx() as db:
            store.access(db,room_id,a,manage=False)
            member=store.member(db,member_id)
            if member['room']!=room_id or member['kind'] not in ('agent','user'):raise Problem('成员不存在',404)
            if not guests.can_edit_appearance(a,room_id,member):raise Problem('只能修改自己的群聊资料',403)
            db.execute('UPDATE members SET avatar=? WHERE id=?',(avatar,member_id))
        return {'id':member_id,'avatar':avatar}

    @app.post('/api/rooms/{room_id}/messages/{message_id}/recall')
    def recall_message(room_id:str,message_id:int,request:Request):
        from .recall import withdraw
        return withdraw(store,user(request),room_id,message_id)

    @app.post('/api/rooms/{room_id}/deliveries/{delivery_id}/retry')
    def retry(room_id:str,delivery_id:str,request:Request):
        a=user(request)
        with store.tx() as db:
            room=store.access(db,room_id,a)
            synchronous.settle(db,room_id)
            d=db.execute('SELECT * FROM deliveries WHERE id=? AND room=?',(delivery_id,room_id)).fetchone()
            if not d or d['status']!='failed':
                raise Problem('只能重试失败的任务',409)
            m=store.member(db,d['member'])
            if not m['enabled'] or m['paused'] or room['paused']:
                raise Problem('请先恢复此 session 和群聊',409)
            message=db.execute('SELECT id,root,recalled_at,message_kind FROM messages WHERE id=?',(d['message'],)).fetchone()
            if message['message_kind']=='session_change_request':raise Problem('请在增减 session 页面重试这项变更',409)
            if message['recalled_at']:raise Problem('消息已撤回，不能重试',409)
            if db.execute('SELECT stopped FROM messages WHERE id=?',(message['root'] or message['id'],)).fetchone()['stopped']:
                raise Problem('这一轮已停止，请发送新任务',409)
            if d['sync_root']:synchronous.retry(db,d)
            else:
                from . import recovery
                recovery.cancel(db,delivery_id)
                db.execute("UPDATE deliveries SET status='pending',lease=NULL,error='' WHERE id=?",(delivery_id,))
        return {'ok':True}

    @app.post('/api/rooms/{room_id}/deliveries/{delivery_id}/steer')
    def convert_to_steer(room_id:str,delivery_id:str,data:SteerConvertInput,request:Request):
        from .steering import convert
        return convert(store,actor(request),room_id,delivery_id,data.task_ref)

    @app.post('/api/agent/join')
    def join(data:JoinInput,request:Request):
        throttle(request,'join')
        auth=request.headers.get('authorization','')
        if not auth.startswith('Bearer '):
            raise Problem('缺少群聊 token',401)
        return store.join(auth[7:],data.name,data.machine)

    @app.post('/api/bridge/sessions')
    def add_session(data:SessionInput,request:Request):
        return store.add_session(actor(request),**data.model_dump())

    @app.post('/api/bridge/heartbeat')
    def heartbeat(data:HeartbeatInput,request:Request):
        a=actor(request)
        if a['kind']!='bridge':
            raise Problem('需要 bridge token',403)
        valid_leases=[];invalid_leases=[]
        with store.tx() as db:
            db.execute("UPDATE members SET last_seen=?,status='online' WHERE id=?",(time.time(),a['id']))
            db.execute("INSERT INTO bridge_projects(bridge,supported,workspace,project) VALUES(?,?,?,?) ON CONFLICT(bridge) DO UPDATE SET supported=excluded.supported,workspace=COALESCE(NULLIF(excluded.workspace,''),bridge_projects.workspace),project=COALESCE(NULLIF(excluded.project,''),bridge_projects.project)",
                       (a['id'],int(data.session_changes_supported),data.workspace,data.project_name))
            db.execute('UPDATE bridge_projects SET workspace_supported=? WHERE bridge=?',(int(data.workspace_supported),a['id']))
            if data.workspace_supported:workspaces.ensure(db,a['id'])
            for item in data.sessions:
                mid=str(item.get('id',''))
                member=db.execute('SELECT id,harness,status FROM members WHERE id=? AND bridge=? AND revoked=0',(mid,a['id'])).fetchone()
                if not member:continue
                db.execute('UPDATE members SET lifecycle_supported=? WHERE id=?',(int(item.get('lifecycle_supported') is True),mid))
                db.execute('UPDATE members SET summary_supported=? WHERE id=?',(int(item.get('summary_supported') is True),mid))
                permissions.heartbeat(db,dict(member),item)
                db.execute('UPDATE members SET sync_supported=? WHERE id=?',(int(item.get('sync_supported') is True),mid))
                db.execute('UPDATE members SET steer_supported=? WHERE id=?',(int(item.get('steer_supported') is True and member['harness']=='codex'),mid))
                if 'runtime' in item:
                    db.execute('UPDATE members SET runtime=? WHERE id=?',(json.dumps(valid_runtime(item['runtime'],member['harness'])),mid))
                db.execute('UPDATE members SET last_seen=?,native_id=?,detail=? WHERE id=? AND bridge=? AND revoked=0',
                           (time.time(),str(item.get('native_id',''))[:200],'' if member['status']=='running' else str(item.get('detail',''))[:1000],mid,a['id']))
                if 'model_control' in item:
                    model=valid_model(str(item.get('model','')))
                    db.execute('INSERT OR IGNORE INTO agent_settings(member,model) VALUES(?,?)',(mid,model))
                    settings=db.execute('SELECT * FROM agent_settings WHERE member=?',(mid,)).fetchone()
                    applied=item.get('config_revision',0)
                    # Only acknowledge the exact configuration loaded locally.
                    if type(applied)!=int or applied<0 or applied>settings['revision']:applied=0
                    if applied==settings['revision'] and model!=settings['model']:applied=0
                    db.execute('UPDATE agent_settings SET supported=?,applied_revision=?,applied_model=? WHERE member=?',
                               (int(item['model_control'] is True),applied,model,mid))
                    catalog=item.get('model_catalog')
                    if isinstance(catalog,dict) and isinstance(catalog.get('models'),list) and len(catalog['models'])<=100:
                        choices=[]
                        for choice in catalog['models']:
                            if not isinstance(choice,dict) or not isinstance(choice.get('id'),str):continue
                            try:model_id=valid_model(choice['id'])
                            except Problem:continue
                            if model_id:choices.append({'id':model_id,'label':str(choice.get('label',model_id))[:200]})
                        updated=catalog.get('updated',0)
                        if type(updated) not in (float,int) or not math.isfinite(updated) or updated<0:updated=0
                        db.execute('UPDATE agent_settings SET catalog=?,catalog_source=?,catalog_updated=? WHERE member=?',(json.dumps(choices,ensure_ascii=False),str(catalog.get('source',''))[:200],updated,mid))
                else:
                    db.execute('UPDATE agent_settings SET supported=0 WHERE member=?',(mid,))
            for item in data.leases:
                try:
                    d=store.delivery(db,a,item.get('id'),str(item.get('lease','')))
                except Problem:
                    invalid_leases.append({'id':item.get('id'),'kind':'lease_invalid','reason':'任务凭证已失效或不属于此 bridge'})
                    continue
                if d['status']=='running' and d['lease_until']>=time.time():
                    db.execute('UPDATE deliveries SET lease_until=? WHERE id=?',(time.time()+LEASE_SECONDS,d['id']))
                    valid_leases.append(d['id'])
                else:
                    kind='lease_expired' if d['status']=='running' else 'task_cancelled' if d['status']=='cancelled' else 'task_finished'
                    invalid_leases.append({'id':d['id'],'kind':kind,'reason':d['error'] or ('任务租约已到期' if kind=='lease_expired' else '服务器上的任务已结束')})
            configs=[dict(row) for row in db.execute('SELECT s.member AS id,s.model,s.revision,s.catalog_refresh,s.reasoning_effort,s.timeout_seconds FROM agent_settings s JOIN members m ON m.id=s.member WHERE m.bridge=? AND m.revoked=0 AND s.supported=1',(a['id'],))]
            permission_configs=[{'id':row['id'],**setting} for row in db.execute('SELECT id FROM members WHERE bridge=? AND revoked=0',(a['id'],)) if (setting:=permissions.desired(db,row['id']))]
            session_states=[{'id':row['id'],'revoked':bool(row['revoked'])} for row in db.execute("SELECT id,revoked FROM members WHERE bridge=? AND kind='agent'",(a['id'],))]
            synchronous.settle(db,a['room'])
            session_changes.settle(db,a['room'])
            changes=session_changes.pending(db,a['id'])
            transitions=lifecycle.states(db,a['id'])
        return {'ok':True,'valid_leases':valid_leases,'invalid_leases':invalid_leases,'lease_seconds':LEASE_SECONDS,
                'model_settings':configs,'permission_settings':permission_configs,'session_states':session_states,'session_changes':changes,'session_transitions':transitions}

    @app.post('/api/bridge/sessions/{member_id}/claim')
    def claim(member_id:str,request:Request):
        return {'delivery':store.claim(actor(request),member_id)}

    @app.post('/api/bridge/deliveries/{delivery_id}/finish')
    def finish(delivery_id:str,data:FinishInput,request:Request):
        return store.finish(actor(request),delivery_id,**data.model_dump())

    @app.post('/api/bridge/deliveries/{delivery_id}/steers/claim')
    def claim_steers(delivery_id:str,data:SteerClaimInput,request:Request):
        from .steering import claim
        return claim(store,actor(request),delivery_id,**data.model_dump())

    @app.post('/api/bridge/deliveries/{delivery_id}/steers/ack')
    def ack_steers(delivery_id:str,data:SteerAckInput,request:Request):
        from .steering import acknowledge
        return acknowledge(store,actor(request),delivery_id,**data.model_dump())

    app.mount('/',StaticFiles(directory=Path(__file__).parent/'static',html=True),name='ui')
    return app
