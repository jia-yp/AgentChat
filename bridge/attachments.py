"""Validate an exact existing local conversation; never guess the latest one."""
import os
import uuid
from pathlib import Path

from .setup_project import native_metadata, workspace_path


def inspect(role, workspace, config):
    native=role.get('attach_native_id')
    if not native:return {}
    try:uuid.UUID(native)
    except (ValueError,TypeError,AttributeError):raise ValueError('attach_native_id 必须是准确的原生会话 UUID') from None
    harness=role['harness']
    if harness not in ('codex','claude'):raise ValueError('此 harness 暂不支持接续外部会话；已托管过的会话请重新加入')
    environments=[dict(os.environ)]
    if harness=='codex':
        if not config.get('codex_home'):raise ValueError('接续已有 Codex 会话前，请先为群聊独立目录完成 codex-login')
        environments.insert(0,dict(os.environ,CODEX_HOME=config['codex_home']))
    for env in environments:
        info=native_metadata({'harness':harness,'native_id':native},env)
        if not info.get('workspace'):continue
        if workspace_path(info['workspace'])!=Path(workspace).resolve():raise ValueError('来源会话不属于此 workspace；请交给对应 workspace 的 manager')
        return {'attach_native_id':native,**({'native_id':native,'codex_source_home':str(Path(env.get('CODEX_HOME') or Path.home()/'.codex').resolve())} if harness=='codex' else {})}
    raise ValueError('本机找不到该原生 session，或无法确认它的 workspace；未创建替代会话')
