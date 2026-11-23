"""Use only the manager's scoped credential and current private task."""
import os


def sessions(context, client, action='list', roles=None, members=None, nonce='', immediate=False, expected=None):
    if context.get('session_type')!='manager':raise ValueError('只有 manager 可管理本 workspace 的成员')
    if action=='list':return client.call('/api/manager/sessions')
    cause=str(context.get('_task_cause') or os.environ.get('AGENTCHAT_CAUSE') or '')
    if not cause or not cause.isdigit():raise ValueError('此操作需在管理员的当前私有任务中执行')
    if not nonce:raise ValueError('请提供稳定的 nonce，网络重试沿用同一编号')
    return client.call('/api/manager/sessions',{'action':action,'cause':int(cause),'nonce':nonce,
        'roles':roles or [],'members':members or [],'immediate':immediate,'expected':expected})
