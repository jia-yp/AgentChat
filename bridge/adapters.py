"""Native CLI adapters. No shared 'last session' flags and no shell interpolation."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from .model_settings import model_arguments
from .harness_errors import HarnessFailure
from .permission_settings import codex_policy, claude_policy, claude_settings, antigravity_policy


def discover():
    home=Path.home()
    candidates={
        'codex':[shutil.which('codex')],
        'claude':[shutil.which('claude'),str(home/'.local/bin/claude.exe'),str(home/'.local/bin/claude')],
        'antigravity':[shutil.which('agy'),str(home/'.gemini/bin/agy.exe'),str(home/'.gemini/bin/agy')],
    }
    if os.name=='nt':
        windows_apps=Path(os.environ.get('ProgramFiles','C:/Program Files'))/'WindowsApps'
        try:
            candidates['codex']+= [str(p) for p in windows_apps.glob('OpenAI.Codex_*/app/resources/codex.exe')]
            candidates['claude']+= [str(p) for p in windows_apps.glob('Claude*/app/resources/claude.exe')]
        except OSError:
            pass
    result={name:{'available':bool(found:=[p for p in paths if p and Path(p).is_file()]),'executable':found[0] if found else None} for name,paths in candidates.items()}
    sdk=importlib.util.find_spec('deepseek_harness') is not None
    runtime=importlib.util.find_spec('deepseek_harness_runtime') is not None
    desktop_cli=None
    if os.name=='nt':
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER,winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive,r'Software\Microsoft\Windows\CurrentVersion\Uninstall') as uninstall:
                    for i in range(winreg.QueryInfoKey(uninstall)[0]):
                        try:
                            with winreg.OpenKey(uninstall,winreg.EnumKey(uninstall,i)) as entry:
                                if 'DeepSeek Harness' not in str(winreg.QueryValueEx(entry,'DisplayName')[0]):continue
                                location=Path(winreg.QueryValueEx(entry,'InstallLocation')[0])
                                candidate=location/'resources/runtime/cli/bin/dsh.cmd'
                                if candidate.exists():desktop_cli=str(candidate)
                        except OSError:continue
            except OSError:continue
    result['deepseek']={'available':sdk and (runtime or bool(desktop_cli)),'sdk':sdk,'runtime':runtime,'desktop_cli':desktop_cli,'executable':sys.executable if sdk and (runtime or desktop_cli) else None,
                        'hint':'需要当前 Python 中安装 deepseek-harness-sdk，并提供 dsh_home 和可用的模型认证'}
    return result


def build_command(session,folder,workspace):
    role=session['definition']; harness=role['harness']; native=session.get('native_id','')
    extras=role.get('args',[])
    if not isinstance(extras,list) or not all(isinstance(x,str) for x in extras):
        raise ValueError('args 必须是字符串数组')
    if session.get('config_revision',0)>0:
        extras=model_arguments(role)[0]
    if harness=='command':
        command=role.get('command')
        if not command or not isinstance(command,list) or not all(isinstance(x,str) for x in command):
            raise ValueError('自定义 harness 必须提供 command 字符串数组')
        values={'session_id':native or session['id'],'workspace':str(workspace),'name':session['name'],'model':role.get('model','')}
        return [x.format_map(values) for x in command]+extras
    if harness=='deepseek':
        return [sys.executable,str(Path(__file__).with_name('deepseek_runner.py')),str(folder/'session.json')]
    executable=role.get('executable') or discover().get(harness,{}).get('executable')
    if not executable:
        raise RuntimeError(f'未找到 {harness} CLI；在 roles 配置指定 executable，或先安装并登录')
    if harness=='codex':
        sandbox,config=codex_policy(role)
        cmd=[executable,'-a','never','-s',sandbox]
        for key,value in config.items():cmd+=['-c',key+'='+json.dumps(value)]
        cmd+=['exec']
        if native:
            cmd+=['resume',native]
        cmd+=['--skip-git-repo-check','--json','--color','never'] if not native else ['--skip-git-repo-check','--json']
        cmd+=['-o',str(folder/'last-response.txt')]
        if role.get('model'):cmd+=['-m',role['model']]
        if role.get('reasoning_effort'):cmd+=['-c','model_reasoning_effort='+json.dumps(role['reasoning_effort'])]
        return cmd+extras+['-']
    if harness=='claude':
        permission_mode,allowed=claude_policy(role)
        cmd=[executable,'-p','--output-format','json','--permission-mode',permission_mode]
        if settings:=claude_settings(role):cmd+=['--settings',json.dumps(settings)]
        if session.get('context'):
            # Only grant the chat operations already authorized for this agent.
            config={'mcpServers':{'agentchat':{'type':'stdio','command':sys.executable,
                'args':[str(Path(__file__).resolve().parents[1]/'agentchat.py'),'chat-mcp'],
                'env':{'AGENTCHAT_CONTEXT':session['context']}}}}
            cmd+=['--mcp-config',json.dumps(config)]
            allowed+=['mcp__agentchat__members','mcp__agentchat__history','mcp__agentchat__send']
        if allowed:cmd+=['--allowedTools',*allowed]
        if native:cmd+=['--resume',native]
        else:cmd+=['--session-id',session['requested_id'],'--name',session['name']]
        if role.get('model'):cmd+=['--model',role['model']]
        if role.get('reasoning_effort'):cmd+=['--effort',role['reasoning_effort']]
        return cmd+extras
    if harness=='antigravity':
        cmd=[executable,'--input-format','stream-json','--output-format','stream-json']+antigravity_policy(role)
        if native:cmd+=['--conversation',native]
        elif role.get('project') or session.get('project_id'):cmd+=['--project',role.get('project') or session['project_id']]
        else:cmd+=['--new-project']
        if role.get('model'):cmd+=['--model',role['model']]
        if role.get('reasoning_effort'):cmd+=['--effort',role['reasoning_effort']]
        return cmd+extras
    raise ValueError('不支持的 harness：'+harness)


def parse_result(session,folder,stdout):
    session.pop('native_result_warning',None)
    harness=session['definition']['harness']
    native=session.get('native_id','')
    objects=[]
    try:
        objects=[json.loads(stdout)]
    except ValueError:
        for line in stdout.splitlines():
            try:objects.append(json.loads(line))
            except ValueError:pass
    result=None;errors=[];terminal_error=None
    for item in objects:
        if not isinstance(item,dict):continue
        if item.get('type')=='thread.started':native=item.get('thread_id',native)
        if item.get('event')=='init':native=item.get('conversation_id',native)
        if item.get('session_id'):native=item['session_id']
        if item.get('conversation_id'):native=item['conversation_id']
        session['native_id']=native
        if item.get('type') in ('error','turn.failed'):
            errors.append(str(item.get('message') or item.get('error') or item))
        if item.get('type')=='turn.failed':terminal_error=errors[-1]
        if harness=='codex' and item.get('type')=='item.completed' and item.get('item',{}).get('type')=='agent_message':result=item['item'].get('text')
        if harness=='claude' and item.get('type')=='result':
            if item.get('is_error'):raise HarnessFailure(str(item.get('result') or item.get('errors','Claude execution failed')))
            result=item.get('result')
        if harness=='antigravity' and item.get('event')=='result':
            envelope=item['result'];native=envelope.get('conversation_id',native)
            if envelope.get('status') not in ('SUCCESS','success'):
                from .antigravity_result import completed_despite_stale_error
                if not completed_despite_stale_error(objects,envelope):
                    raise HarnessFailure(str(envelope.get('error') or envelope.get('status')))
                session['native_result_warning']='Antigravity 返回了旧错误状态；本次所有步骤已完成且没有错误步骤，最终回复与完成事件完全一致，已保留回复。'
            result=envelope.get('response')
        if harness in ('deepseek','command') and 'response' in item:
            if item.get('error'):raise HarnessFailure(item['error'])
            result=item['response']
    if harness=='codex' and (folder/'last-response.txt').exists():
        result=(folder/'last-response.txt').read_text(encoding='utf-8')
    if harness=='command' and result is None:result=stdout
    session['native_id']=native
    from .runtime_metadata import result_runtime
    session['runtime']=result_runtime(session,objects)
    if terminal_error:raise HarnessFailure(terminal_error)
    if errors and not result:raise HarnessFailure('\n'.join(errors)[-4000:])
    if not result or not result.strip():raise HarnessFailure('harness 没有返回最终消息，详情见本地日志')
    return result.strip()


def stop_tree(process):
    if process.poll() is not None:return
    if os.name=='nt':
        subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        import signal
        try:os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:return
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL)
