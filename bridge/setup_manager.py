"""Bind the setup agent itself, never guess from which CLIs are installed."""
import os
import re
from pathlib import Path

from .common import read_json
from .roles import HARNESSES
from .setup_project import native_metadata


MANAGER_ROLE='作为负责本群接入的 setup agent，响应管理员的私聊，管理本项目的 harness 设置、session 配置与文件，保护协作 session 的未提交成果。'
IDENTITY_FIELDS={'harness','native_id','model','reasoning_effort','executable','command','dsh_home','dsh_bin','provider','profile','project','workspace','project_name'}


def existing_manager(config):
    if not config:return None
    managers=[read_json(s['context']) for s in config['sessions'] if Path(s['context']).is_file()]
    managers=[s for s in managers if s.get('session_type')=='manager' and not s.get('revoked')]
    if len(managers)>1:raise ValueError('本地配置存在多个 manager，请先核对原 bridge 配置')
    return managers[0] if managers else None


def creator_identity(args,environment=None):
    env=os.environ if environment is None else environment
    path=getattr(args,'manager_identity',None)
    data=read_json(path) if path else {}
    if not isinstance(data,dict) or set(data)-IDENTITY_FIELDS:
        raise ValueError('setup 身份文件仅接受 harness、native_id、workspace、project_name 及模型/适配器信息')
    data=dict(data)
    for key in ('harness','native_id','workspace','project_name'):
        explicit=getattr(args,'manager_'+key,None)
        if explicit:data[key]=explicit
    harness=data.get('harness')
    if not harness:
        candidates=[]
        if env.get('CODEX_THREAD_ID'):candidates.append('codex')
        if env.get('CLAUDECODE') or env.get('CLAUDE_SESSION_ID'):candidates.append('claude')
        if len(candidates)!=1:
            raise ValueError('无法确定当前 setup agent 的 harness。由 setup agent 使用 --manager-harness 和 --manager-native-id 提供自己的身份，无需用户在角色列表另配 manager。')
        harness=candidates[0];data['harness']=harness
    if harness not in HARNESSES:raise ValueError('setup agent 的 harness 无效')
    if not data.get('native_id'):
        key={'codex':'CODEX_THREAD_ID','claude':'CLAUDE_SESSION_ID'}.get(harness)
        if key:data['native_id']=env.get(key,'')
    native=data.get('native_id')
    if not isinstance(native,str) or not native or len(native)>200 or re.search(r'[\s\x00-\x1f]',native):
        raise ValueError('尚未获得 setup agent 当前原生 session ID；请由 setup agent 用 --manager-native-id 或 --manager-identity 补充。不会猜测“最近会话”或另建替代 manager。')
    if harness in ('codex','claude','antigravity') and not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}',native):
        raise ValueError('当前 harness 的原生 session ID 应为 UUID')
    for key,value in native_metadata(data,env).items():data.setdefault(key,value)
    if harness=='codex':data['codex_source_home']=str(Path(env.get('CODEX_HOME') or Path.home()/'.codex').resolve())
    return data


def manager_plan(args,config,workers,pending=None,environment=None):
    current=existing_manager(config)
    if current:
        if any(r['name']==current['name'] for r in workers):raise ValueError(current['name']+' 已是 manager，请为协作角色换一个名称')
        return None
    identity=(pending or {}).get('manager_identity') or creator_identity(args,environment)
    if config:
        for entry in config['sessions']:
            session=read_json(entry['context'])
            if session.get('native_id')==identity['native_id'] and session['definition']['harness']==identity['harness']:
                raise ValueError('这个原生 session 已作为协作角色接入，不能同时用作私有 manager')
    return identity


def manager_definition(identity,workers,members,bridge_id=None):
    occupied={r['name'] for r in workers}|{m['name'] for m in members}
    base='manager' if not bridge_id or not any(m.get('kind')=='bridge' and m['id']!=bridge_id for m in members) else 'manager-'+bridge_id[:6]
    name=base;number=2
    while name in occupied:name=f'{base}{number}';number+=1
    return {**{k:v for k,v in identity.items() if k not in ('native_id','workspace','project_name','project_name_required','codex_source_home')},'name':name,'session_type':'manager','role':MANAGER_ROLE}
