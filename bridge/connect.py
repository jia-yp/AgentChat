"""Redeem a short setup link, then acknowledge readiness without calling a model."""
import argparse
import getpass
import json
import re
import secrets
import socket
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .common import APIError, Client, ProcessLock, read_json, write_json
from .setup_manager import manager_plan, manager_definition
from .setup_project import setup_location


def parse_setup_link(link):
    parsed=urlsplit(link.strip())
    match=re.fullmatch(r'(.*)/setup/([a-f0-9]{24})',parsed.path)
    fields=parse_qs(parsed.fragment,keep_blank_values=True)
    if (parsed.scheme not in ('http','https') or not parsed.netloc or parsed.username or parsed.password or parsed.query
            or not match or set(fields)!={'token'} or len(fields['token'])!=1
            or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',fields['token'][0])):
        raise ValueError('请使用网页复制的完整 setup 链接（包含 #token= 部分）')
    return urlunsplit((parsed.scheme,parsed.netloc,match[1],'','')),match[2],fields['token'][0]


def finish_setup(config_path, timeout=45):
    path=Path(config_path).resolve();config=read_json(path)
    request=config.get('setup_request')
    if not request:raise ValueError('这个 bridge 配置没有关联的 setup 链接')
    client=Client(config['url'],config['token']);deadline=time.monotonic()+timeout
    while True:
        try:
            receipt=client.call('/api/bridge/setup/'+request['id']+'/complete',{})
            break
        except APIError as exc:
            if exc.status!=425:raise
            if time.monotonic()>=deadline:
                raise RuntimeError('尚未确认全部心跳；保持 bridge 运行，稍后用 finish-setup --config 重试') from None
            time.sleep(.5)
    config=read_json(path)
    config['setup_request'].update(status='complete',completed=receipt['completed'])
    write_json(path,config)
    manager=next((s for entry in config['sessions'] if (s:=read_json(entry['context'])).get('session_type')=='manager'),None)
    print(json.dumps({'room':config['room'],'config':str(path),'workspace':config['workspace'],
                      'project_name':config.get('project_name',config['room']['name']),'setup':'complete',
                      'manager':{'name':manager['name'],'harness':manager['definition']['harness'],'context':manager['context']} if manager else None,
                      'invitation_invalidated':True,'token_rotated':receipt['token_rotated']},ensure_ascii=False,indent=2))
    return receipt


def connect(args):
    from .cli import ROOT, install_skills, setup, start_bridge, validate_roles
    link=sys.stdin.readline().strip() if args.link_stdin else getpass.getpass('完整 setup 链接（不回显）: ')
    url,request_id,join_token=parse_setup_link(link)
    explicit=Path(args.config).expanduser().resolve() if args.config else None
    saved=read_json(explicit) if explicit and explicit.is_file() else None
    client=Client(url,join_token)
    try:
        profile=client.call('/api/setup/requests/'+request_id)
    except APIError as exc:
        if exc.status!=401 or not saved or saved.get('url')!=url or saved.get('setup_request',{}).get('id')!=request_id:raise
        client=Client(url,saved['token'])
        profile=client.call('/api/setup/requests/'+request_id)
    if saved and (saved.get('url')!=url or saved.get('room_id')!=profile['room']['id']):
        raise ValueError('原 bridge 配置与这个链接的服务或群聊不一致')
    workspace,project_name,creator=setup_location(profile,args,saved)
    workspace.mkdir(parents=True,exist_ok=True)
    name=args.name or 'setup-'+re.sub('[^a-z0-9-]','-',socket.gethostname().lower())[:25]
    # Reuse a uniquely matching bridge, including a custom name from legacy setup.
    if not explicit:
        candidates=[]
        paths=list((workspace/'.agentchat').glob('*.json'))+list((workspace/'.agentchat'/'rooms'/profile['room']['id']/'bridges').glob('*.json'))
        for path in paths:
            try:previous=read_json(path)
            except (OSError,ValueError):continue
            if (previous.get('kind')=='bridge' and previous.get('url')==url and previous.get('room_id')==profile['room']['id']
                    and previous.get('workspace')==str(workspace) and (not args.name or previous.get('name')==name)):
                candidates.append(path)
        if len(candidates)>1:raise ValueError('本地有多个匹配的 bridge，请用 --config 指定要恢复的配置')
        if candidates:explicit=candidates[0]
    config_path=explicit or workspace/'.agentchat'/'rooms'/profile['room']['id']/'bridges'/f'{name}.json'
    config_path.parent.mkdir(parents=True,exist_ok=True)
    with ProcessLock(config_path.with_suffix('.setup.lock')):
        config=read_json(config_path) if config_path.is_file() else None
        if config:
            if config['url']!=url or config['workspace']!=str(workspace) or config['room_id']!=profile['room']['id']:
                raise ValueError('原 bridge 配置与这个链接的服务、群聊或 workspace 不一致')
            if args.name and args.name!=config['name']:raise ValueError('原 bridge 的名称与 --name 不一致')
            name=config['name']
            if profile.get('inherit_manager'):
                workspace,project_name,creator=setup_location(profile,args,config)
        if profile['status']=='complete' and config:
            start_bridge(config_path)
            return finish_setup(config_path)
        roles=validate_roles(profile['roles'])
        install_skills(workspace)
        pending_path=workspace/'.agentchat'/'setup-requests'/f'{request_id}.json'
        if pending_path.is_file():
            pending=read_json(pending_path)
            if pending['url']!=url or pending['config']!=str(config_path) or pending['name']!=name:
                raise ValueError('这次接入已有本地恢复记录，请使用原 config 和名称继续')
            if config and pending['bridge_token']!=config['token']:raise ValueError('本地恢复记录与 bridge 身份不一致')
            if pending.get('workspace',str(workspace))!=str(workspace):raise ValueError('本地恢复记录的 workspace 不一致')
            if profile.get('inherit_manager') and pending.get('project_name'):
                project_name=pending['project_name']
        else:
            pending={'id':request_id,'url':url,'config':str(config_path),'name':name,'claim_id':uuid.uuid4().hex,
                     'bridge_token':config['token'] if config else secrets.token_urlsafe(32)}
        if creator and 'manager_identity' not in pending:pending['manager_identity']=creator
        identity=manager_plan(args,config,roles,pending) if profile.get('manager_policy')=='setup-agent' else None
        if identity:
            # Validate the native adapter before consuming any invitation state.
            validate_roles([manager_definition(identity,roles,[])])
            pending['manager_identity']=identity
        pending.update(workspace=str(workspace),project_name=project_name)
        write_json(pending_path,pending)
        # Persist the candidate credential before sending it, so a lost reply is retryable.
        claimed=client.call('/api/setup/requests/'+request_id+'/claim',{'name':name,
            'machine':(socket.gethostname()+' | '+str(workspace))[:500],'claim_id':pending['claim_id'],'bridge_token':pending['bridge_token'],
            'workspace':str(workspace),'project_name':project_name})
        if config and config['id']!=claimed['member_id']:raise ValueError('服务器返回的 bridge 身份与本地不一致')
        if not config:
            config={'url':url,'token':pending['bridge_token'],'id':claimed['member_id'],'name':claimed['name'],
                    'kind':'bridge','room':claimed['room'],'room_id':claimed['room']['id'],'workspace':str(workspace),
                    'sessions':[],'cli':str(ROOT/'agentchat.py')}
        config['project_name']=project_name
        config['setup_request']={'id':request_id,'claim_id':pending['claim_id'],'status':'running','profile_revision':profile['profile_revision']}
        write_json(config_path,config)
        print('接入配置：'+str(config_path),flush=True)
        try:
            if identity:
                members=Client(url,config['token']).call('/api/rooms/'+config['room']['id'])['members']
                if any(m.get('session_type')=='manager' and m.get('bridge')==config['id'] and not m['revoked'] for m in members) and 'manager_definition' not in pending:
                    raise ValueError('此 bridge 已有 manager，但本地身份记录缺失；请恢复原配置后重试，不会创建重复 manager')
                if 'manager_definition' not in pending:
                    pending['manager_definition']=manager_definition(identity,roles,members,config['id'])
                    write_json(pending_path,pending)
                roles=[pending['manager_definition'],*roles]
            setup(argparse.Namespace(workspace=str(workspace),server=url,roles=None,name=name,config=str(config_path),
                                     project_name=project_name,allow_unavailable=False,token_stdin=False,start=True,manager_identity=identity),role_values=roles)
            return finish_setup(config_path)
        except Exception as exc:
            try:Client(url,config['token']).call('/api/bridge/setup/'+request_id+'/report',{'detail':str(exc)[:1000]})
            except Exception:pass
            raise
