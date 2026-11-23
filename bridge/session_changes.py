"""Apply the owner's immutable session list without blocking harnesses or heartbeats."""
import threading
from types import SimpleNamespace
from urllib.error import URLError

from .common import APIError, read_json, write_json
from .execution import cancel_job


class SessionChanges:
    def __init__(self,bridge):
        self.bridge=bridge
        self.thread=None

    def poll(self,changes):
        if not changes or self.bridge.stop.is_set() or (self.thread and self.thread.is_alive()):return
        self.thread=threading.Thread(target=self.apply,args=(changes[0]['id'],),daemon=True,name='session-changes')
        self.thread.start()

    def apply(self,rid):
        bridge=self.bridge;client=bridge.client;base='/api/bridge/session-changes/'+rid
        try:
            change=client.call(base)
            if change['status']!='applying' or bridge.stop.is_set():return
            config=read_json(bridge.path)
            # Check every requested harness before changing the active roster.
            if change['additions']:
                from .cli import validate_roles
                validate_roles(change['additions'])
            waiting=False
            for member in change['removals']:
                if bridge.stop.is_set():return
                removal=client.call(base+'/remove',{'member':member['id']})
                if removal.get('operation'):
                    waiting=waiting or not removal['ok']
                    continue
                with bridge.lock:
                    session=bridge.sessions.get(member['id'])
                    if session is not None:
                        session['revoked']=True
                        if job:=bridge.running.get(member['id']):cancel_job(job,'session_deleted','执行已停止：管理员通过 manager 移除了此 session')
                        else:write_json(session['context'],session)
            if waiting:return
            if change['additions']:
                from .cli import setup
                args=SimpleNamespace(workspace=config['workspace'],server=config['url'],name=config['name'],
                    config=str(bridge.path),project_name=config.get('project_name') or bridge.config.get('project_name'),allow_unavailable=False,
                    start=False,token_stdin=False,change_id=rid)
                setup(args,role_values=change['additions'])
            # Registration and local config writes are durable before reporting.
            # Heartbeat processes revocation and the normal loop loads new entries.
            if not bridge.stop.is_set():client.call(base+'/report',{})
        except (URLError,TimeoutError,ConnectionError):
            # Re-read durable server state on the next heartbeat; don't replay a
            # native model call or change the owner's task budget.
            pass
        except APIError as exc:
            if exc.status>=500 or exc.status in (401,429):return
            if exc.status in (403,409):
                try:
                    current=client.call(base)
                    if current['status']!='applying' or not current.get('can_apply',True):return
                except Exception:return
            self.fail(base,exc)
        except Exception as exc:
            self.fail(base,exc)

    def fail(self,base,error):
        safe=self.bridge.redact(str(error))[:1000]
        try:self.bridge.client.call(base+'/report',{'error':safe})
        except Exception:pass
        print('Session 变更未完成：'+safe,flush=True)
