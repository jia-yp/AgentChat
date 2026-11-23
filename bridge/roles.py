"""Portable validation for role files and saved templates."""
from .names import normalize_name
from .permission_settings import validate as validate_permissions
from .execution import timeout_seconds
from .context_window import prompt_limit


HARNESSES = {'codex', 'claude', 'antigravity', 'deepseek', 'command'}


def role_definitions(roles):
    if not isinstance(roles, list) or not 1 <= len(roles) <= 101:
        raise ValueError('角色清单最多包含 100 个协作 session 和 1 个 manager')
    result, names = [], set()
    for item in roles:
        if not isinstance(item, dict):
            raise ValueError('每个角色必须是 JSON 对象')
        name = normalize_name(item.get('name', ''))
        if name in names:
            raise ValueError('session 名称重复：' + name)
        names.add(name)
        if not isinstance(item.get('harness'), str) or item['harness'] not in HARNESSES:
            raise ValueError(name + ' 的 harness 无效')
        role = item.get('role')
        if not isinstance(role, str) or not role.strip() or len(role) > 4000:
            raise ValueError(name + ' 需填写 1–4000 字的分工')
        if item.get('session_type','worker') not in ('worker','manager'):
            raise ValueError(name + ' 的 session_type 必须是 worker 或 manager')
        if item.get('permission_source','session') not in ('session','workspace'):
            raise ValueError(name+' 的 permission_source 必须是 session 或 workspace')
        if item.get('permission_source')=='workspace' and item.get('args'):
            raise ValueError(name+' 使用自定义 args，不能同时跟随 workspace 权限')
        validate_permissions(item)
        timeout_seconds(item)
        prompt_limit(item)
        result.append({**item, 'name': name, 'role': role.strip()})
    if sum(r.get('session_type')=='manager' for r in result)>1:
        raise ValueError('每个接入配置最多一个 manager')
    if sum(r.get('session_type')!='manager' for r in result)>100:
        raise ValueError('最多配置 100 个协作 session')
    return result


def worker_roles(roles):
    """Legacy templates may contain managers; setup owns that identity now."""
    result=[]
    for role in role_definitions(roles):
        if role.get('session_type')=='manager':continue
        role=dict(role);role.pop('session_type',None);result.append(role)
    if not result:raise ValueError('请至少添加一个协作角色；manager 由 setup agent 自动接入')
    return result
