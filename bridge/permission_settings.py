"""Explicit native permissions; workspace defaults are resolved by the server."""
import json
from pathlib import Path
from urllib.parse import urlsplit

INHERIT = {'id':'inherit','label':'保留接入时权限','description':'沿用接入配置；不会自动扩大权限。'}
FULL = {'id':'full','label':'完全访问（含 SSH）','description':'允许命令、联网和工作区外文件访问，使用本机账号的权限与 SSH 配置。'}
CATALOG = {
    'codex': [INHERIT,
        {'id':'read-only','label':'只读沙箱','description':'可读取文件，禁止写入与联网。'},
        {'id':'workspace','label':'工作区沙箱 · 禁止联网','description':'允许工作区内写入和沙箱命令；禁止联网。'},
        {'id':'network','label':'工作区沙箱 · 允许联网','description':'开放沙箱网络；仍限制文件访问，SSH 密钥或客户端可能被沙箱阻止。'}, FULL],
    'claude': [INHERIT,
        {'id':'edits','label':'自动接受文件编辑','description':'自动接受文件编辑；需要额外授权的命令会被拒绝。'},
        {'id':'ssh','label':'文件编辑 + SSH / SCP / SFTP','description':'额外允许 SSH、SCP、SFTP 命令，包括其远程操作；其他工具仍遵循本机 Claude 权限规则。'}, FULL],
    'antigravity': [INHERIT,
        {'id':'collaborate','label':'后台协作 · 工作区沙箱','description':'自动接受编辑与沙箱内命令，允许访问群聊服务；其他联网或越界操作仍遵循本机规则。'},
        {'id':'sandbox','label':'沙箱 · 自动接受编辑','description':'开启原生沙箱与终端限制；需额外授权的命令仍可能被拒绝。'}, FULL],
}


def options(role):
    # Arbitrary flags/settings files could override a web choice. Do not claim
    # enforcement for a launch we cannot translate, including custom commands.
    return [] if role.get('args') else CATALOG.get(role['harness'],[])


def unavailable_reason(role):
    if role.get('args'):return '此 session 使用自定义 args；需先移除覆盖参数，才能由网页管理权限。'
    if role['harness']=='deepseek':return '当前 DeepSeek SDK 未提供逐 session 权限接口；请在本机 harness 配置权限。'
    return '自定义命令的权限由启动程序管理。'


def validate(role):
    mode=role.get('permissions','inherit')
    if not isinstance(mode,str) or (mode!='inherit' and mode not in {x['id'] for x in options(role)}):
        raise ValueError('此 harness 不支持权限选项：'+str(mode))
    return mode


def summary(role):
    mode=validate(role)
    if mode!='inherit':return next(x['label'] for x in options(role) if x['id']==mode)
    if role.get('args'):return '接入配置包含自定义 args'
    if role.get('harness')=='codex':return '接入配置：'+role.get('sandbox','workspace-write')+'；网络遵循本机配置'
    if role.get('harness')=='claude':return '接入配置：'+role.get('permission_mode','acceptEdits')+'；工具遵循本机规则'
    if role.get('harness')=='antigravity':return '接入配置：'+role.get('permission_mode','accept-edits')+' + sandbox'
    return '本机 harness 配置'


def report(session):
    role=session['definition']
    return {'mode':validate(role),'revision':session.get('permission_revision',0),
            'supported':bool(options(role)),'detail':summary(role) if options(role) else unavailable_reason(role)}


def codex_policy(role):
    mode=validate(role)
    sandbox={'read-only':'read-only','workspace':'workspace-write','network':'workspace-write','full':'danger-full-access'}.get(mode,role.get('sandbox','workspace-write'))
    config={}
    if mode in ('read-only','workspace','network'):
        config={'sandbox_workspace_write.network_access':mode=='network','sandbox_read_only.network_access':False}
    return sandbox,config


def claude_policy(role):
    mode=validate(role)
    native='bypassPermissions' if mode=='full' else 'acceptEdits' if mode in ('edits','ssh') else role.get('permission_mode','acceptEdits')
    allowed=[f'{tool}({command} *)' for tool in ('Bash','PowerShell') for command in ('ssh','scp','sftp','ssh.exe','scp.exe','sftp.exe')] if mode=='ssh' else []
    return native,allowed


def claude_settings(role):
    mode=validate(role)
    if mode=='full':return {'sandbox':{'enabled':False}}
    if mode=='ssh':return {'sandbox':{'excludedCommands':[c+' *' for c in ('ssh','scp','sftp','ssh.exe','scp.exe','sftp.exe')]}}
    return {}


def antigravity_policy(role,session=None):
    mode=validate(role)
    if mode=='full':return ['--mode','accept-edits','--dangerously-skip-permissions','--sandbox=false']
    if mode=='collaborate':
        settings_path=Path.home()/'.gemini/antigravity-cli/settings.json'
        configured=json.loads(settings_path.read_text(encoding='utf-8-sig')).get('permissions',{}) if settings_path.exists() else {}
        if not isinstance(configured,dict):raise ValueError('Antigravity 本机 permissions 配置无效')
        allow=list(configured.get('allow',[]))
        if not all(isinstance(x,str) for x in allow):raise ValueError('Antigravity 本机 allow 配置无效')
        root=Path(__file__).resolve().parents[1]
        allow.extend(['read_file('+str(root/'bridge')+')','read_file('+str(root/'agentchat.py')+')'])
        if session:
            if host:=urlsplit(session.get('url','')).hostname:allow.append('read_url('+host+')')
            if session.get('workspace'):allow.append('write_file('+session['workspace']+')')
        allow.extend('mcp(agentchat/'+tool+')' for tool in ('members','history','send'))
        if session and session.get('session_type')=='manager':allow.append('mcp(agentchat/sessions)')
        settings={'toolPermission':'proceed-in-sandbox','artifactReviewPolicy':'always-proceed','permissions':{**configured,'allow':list(dict.fromkeys(allow))}}
        return ['--mode','accept-edits','--sandbox','--settings',json.dumps(settings)]
    return ['--mode','accept-edits' if mode=='sandbox' else role.get('permission_mode','accept-edits'),'--sandbox']
