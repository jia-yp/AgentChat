"""Resolve the creator's workspace and owning project, never a recent chat title."""
import json
import os
import sqlite3
from pathlib import Path


def workspace_path(value):
    if not isinstance(value,str) or not value.strip() or any(c in value for c in '\0\r\n'):
        raise ValueError('无法读取 manager 的工作目录；请由 setup agent 提供 --manager-workspace <当前工作目录的绝对路径>')
    value=value.strip()
    # Codex Desktop stores Windows paths with the extended-length prefix.
    if os.name=='nt' and value.startswith('\\\\?\\'):
        value='\\\\'+value[8:] if value.startswith('\\\\?\\UNC\\') else value[4:]
    path=Path(value).expanduser()
    if not path.is_absolute():raise ValueError('manager 的 workspace 必须是绝对路径，不能依赖 connect 命令的执行目录')
    return path.resolve()


def codex_metadata(native,env):
    home=Path(env.get('CODEX_HOME') or Path.home()/'.codex').expanduser().resolve()
    for db_path in sorted(home.glob('state_*.sqlite'),reverse=True):
        try:
            with sqlite3.connect(db_path.as_uri()+'?mode=ro',uri=True,timeout=2) as db:
                db.row_factory=sqlite3.Row
                available={r['name'] for r in db.execute('PRAGMA table_info(threads)')}
                fields=[k for k in ('model','reasoning_effort','cwd','project_id') if k in available]
                if not fields:continue
                row=db.execute('SELECT '+','.join(fields)+' FROM threads WHERE id=?',(native,)).fetchone()
                if not row:continue
                data={k:row[k] for k in ('model','reasoning_effort') if k in fields and row[k]}
                if 'cwd' in fields and row['cwd']:
                    try:data['workspace']=str(workspace_path(row['cwd']))
                    except (ValueError,OSError):pass
                try:
                    project=db.execute('SELECT name FROM projects WHERE id=?',(row['project_id'],)).fetchone() if 'project_id' in fields and row['project_id'] else None
                    if project and project['name']:data['project_name']=project['name']
                    elif data.get('workspace'):
                        # Desktop threads can belong to a project through a root
                        # even when threads.project_id is null. Prefer the nearest root.
                        cwd=workspace_path(data['workspace']);matches=[]
                        for root in db.execute('SELECT p.name,r.path FROM projects p JOIN project_roots r ON r.project_id=p.id'):
                            try:path=workspace_path(root['path'])
                            except (ValueError,OSError):continue
                            if cwd.is_relative_to(path) and root['name']:matches.append((len(path.parts),root['name']))
                        if matches:
                            depth=max(d for d,_ in matches);names={name for d,name in matches if d==depth}
                            if len(names)==1:data['project_name']=names.pop()
                            else:data['project_name_required']=True
                except sqlite3.Error:pass  # CLI-only databases do not have projects.
                return data
        except (sqlite3.Error,OSError):continue
    return {}


def claude_metadata(native,env):
    home=Path(env.get('CLAUDE_CONFIG_DIR') or Path.home()/'.claude').expanduser()
    paths=list((home/'projects').glob('*/'+native+'.jsonl'))
    if len(paths)!=1:return {}
    # Only inspect this exact session's metadata, with a bounded read.
    try:
        with paths[0].open(encoding='utf-8') as file:
            for line in file.read(1024*1024).splitlines():
                try:row=json.loads(line)
                except ValueError:continue
                if row.get('sessionId')==native and row.get('cwd'):
                    return {'workspace':str(workspace_path(row['cwd']))}
    except OSError:pass
    return {}


def native_metadata(identity,env=None):
    env=os.environ if env is None else env
    if identity['harness']=='codex':return codex_metadata(identity['native_id'],env)
    if identity['harness']=='claude':return claude_metadata(identity['native_id'],env)
    return {}


def identity_location(identity):
    workspace=workspace_path(identity.get('workspace'))
    if identity.get('project_name_required') and not identity.get('project_name'):
        raise ValueError('这个工作目录属于多个 project；请由 setup agent 提供 --manager-project-name <自己所属的项目名称>')
    name=identity.get('project_name') or workspace.name
    if not isinstance(name,str) or not name.strip() or len(name.strip())>200 or any(ord(c)<32 for c in name):
        raise ValueError('请由 setup agent 提供有效的 --manager-project-name <自己所属的项目名称>')
    return workspace,name.strip()


def setup_location(profile,args,config=None):
    from .setup_manager import creator_identity, existing_manager
    if profile.get('resolved_workspace'):
        return workspace_path(profile['resolved_workspace']),profile['resolved_project_name'],None
    if not profile.get('inherit_manager'):
        # Keep relative paths from legacy/manual setup compatible.
        return Path(profile['workspace']).expanduser().resolve(),profile.get('project_name') or profile['room']['name'],None
    manager=existing_manager(config)
    if manager:
        workspace=workspace_path(config['workspace'])
        name=config.get('project_name')
        if not name:
            metadata=native_metadata({**manager['definition'],'native_id':manager['native_id']})
            if metadata.get('workspace') and workspace_path(metadata['workspace'])==workspace:
                return (*identity_location({**metadata,'workspace':str(workspace)}),None)
            name=workspace.name
        return (*identity_location({'workspace':str(workspace),'project_name':name}),None)
    identity=creator_identity(args)
    return (*identity_location(identity),identity)
