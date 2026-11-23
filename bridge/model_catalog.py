"""Query local harness model menus without creating a conversation or running inference."""
import json
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

from .adapters import discover, stop_tree
from .harness_environment import harness_environment
from .model_settings import configured_model
from .processes import background_options


class JsonProcess:
    def __init__(self,command,workspace,env=None):
        options=background_options()
        self.process=subprocess.Popen(command,cwd=workspace,env=env if env is not None else harness_environment(),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',**options)
        self.messages=queue.Queue()
        def read():
            for line in self.process.stdout:
                try:self.messages.put(json.loads(line))
                except ValueError:pass
            self.messages.put({'closed':True})
        threading.Thread(target=read,daemon=True).start()

    def send(self,body):
        self.process.stdin.write(json.dumps(body)+'\n');self.process.stdin.flush()

    def receive(self,predicate):
        deadline=time.monotonic()+18
        while time.monotonic()<deadline:
            item=self.messages.get(timeout=max(.01,deadline-time.monotonic()))
            if item.get('closed'):raise RuntimeError('模型列表连接已关闭')
            if predicate(item):return item
        raise TimeoutError('模型列表读取超时')

    def close(self):
        stop_tree(self.process)
        for stream in (self.process.stdin,self.process.stdout):
            try:stream.close()
            except OSError:pass


def discover_models(session):
    role=session['definition'];harness=role['harness'];workspace=session['workspace']
    executable=role.get('executable') or discover().get(harness,{}).get('executable')
    models=[];source='本地配置'
    if harness in ('codex','claude') and executable:
        command=[executable,'app-server','--stdio'] if harness=='codex' else [executable,'-p','--input-format','stream-json','--output-format','stream-json','--verbose']
        from .codex_isolation import environment
        process=JsonProcess(command,workspace,environment(session) if harness=='codex' else None)
        try:
            if harness=='codex':
                process.send({'id':1,'method':'initialize','params':{'clientInfo':{'name':'agentchat-models','version':'1.0'}}})
                process.receive(lambda x:x.get('id')==1);process.send({'method':'initialized'})
                cursor=None
                for page in range(5):
                    process.send({'id':page+2,'method':'model/list','params':{'limit':100,**({'cursor':cursor} if cursor else {})}})
                    response=process.receive(lambda x:x.get('id')==page+2)
                    if 'error' in response:raise RuntimeError('Codex 模型列表暂不可用')
                    result=response.get('result',{})
                    models += [{'id':m.get('model',m.get('id','')),'label':m.get('displayName') or m.get('model') or m.get('id','')} for m in result.get('data',[]) if not m.get('hidden')]
                    cursor=result.get('nextCursor')
                    if not cursor:break
            else:
                process.send({'type':'control_request','request_id':'models','request':{'subtype':'initialize'}})
                response=process.receive(lambda x:x.get('type')=='control_response' and x.get('response',{}).get('request_id')=='models')
                models=[{'id':m['value'],'label':m.get('displayName',m['value'])} for m in response.get('response',{}).get('response',{}).get('models',[]) if m.get('value')]
            source=harness+' 本机账号模型列表'
        finally:process.close()
    elif harness=='antigravity' and executable:
        result=subprocess.run([executable,'models'],cwd=workspace,env=harness_environment(harness=harness),capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20,**background_options(new_session=False))
        if result.returncode:raise RuntimeError('Antigravity 模型列表暂不可用')
        models=[{'id':line.split('\t',1)[0].strip(),'label':line.split('\t',1)[1].strip()} for line in result.stdout.splitlines() if '\t' in line]
        source='Antigravity 本机账号模型列表'
    elif harness=='deepseek':
        cli=role.get('dsh_bin') or discover()['deepseek'].get('desktop_cli')
        if cli:
            env=harness_environment()
            if role.get('dsh_home'):env['DSH_HOME']=role['dsh_home']
            # The composed config may contain secrets: extract only model IDs in
            # memory. Never return, persist or log the raw output.
            result=subprocess.run([cli,'--profile',role.get('profile','sdk'),'--dump-config'],env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20,**background_options(new_session=False))
            if not result.returncode:
                models=[{'id':m,'label':m} for m in re.findall(r'^\s*model:\s*[\'\"]?([a-zA-Z0-9][a-zA-Z0-9._:/@+\-]*)',result.stdout,re.M)]
                source='DeepSeek 本机 profile 已配置模型；可用适配参数 model_options 扩充'
    for item in role.get('model_options',[]):
        if isinstance(item,str):models.append({'id':item,'label':item})
        elif isinstance(item,dict) and isinstance(item.get('id'),str):models.append({'id':item['id'],'label':item.get('label',item['id'])})
    current=configured_model(role)
    if current and current not in {m['id'] for m in models}:models.append({'id':current,'label':current+'（当前配置）'})
    return {'models':list({m['id']:m for m in models if m['id']}.values())[:100],'source':source,'updated':time.time()}


class ModelCatalogs:
    def __init__(self):
        self.cache={};self.inflight=set();self.lock=threading.Lock();self.requests={}

    def key(self,session):
        role=session['definition']
        return (role['harness'],role.get('executable',''),session['workspace'],session.get('codex_home',''),role.get('dsh_home',''),json.dumps(role.get('model_options',[]),sort_keys=True))

    def get(self,session,requested=0):
        key=self.key(session)
        with self.lock:
            cached=self.cache.get(key)
            due=not cached or time.time()-cached['updated']>900 or requested>self.requests.get(key,0)
            if due and key not in self.inflight:
                self.inflight.add(key);self.requests[key]=requested
                threading.Thread(target=self.load,args=(key,dict(session)),daemon=True).start()
            return cached

    def load(self,key,session):
        try:result=discover_models(session)
        except Exception:
            old=self.cache.get(key)
            result={**(old or {'models':[]}), 'source':'读取失败，可点刷新重试','updated':time.time()}
        with self.lock:self.cache[key]=result;self.inflight.discard(key)
