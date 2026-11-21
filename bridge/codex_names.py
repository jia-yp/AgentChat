"""Name only the native Codex thread that this bridge created, via its public RPC."""
import json
import queue
import subprocess
import threading

from .adapters import discover,stop_tree
from .codex_isolation import environment
from .processes import background_options


def set_native_name(session):
    executable=session['definition'].get('executable') or discover()['codex']['executable']
    options=background_options()
    process=subprocess.Popen([executable,'app-server','--stdio'],cwd=session['workspace'],env=environment(session),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',**options)
    received=queue.Queue()
    def read():
        for line in process.stdout:
            try:received.put(json.loads(line))
            except ValueError:pass
        received.put({'error':'app-server closed'})
    threading.Thread(target=read,daemon=True).start()
    def send(payload):
        process.stdin.write(json.dumps(payload,ensure_ascii=False)+'\n');process.stdin.flush()
    def response(request_id):
        import time
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            message=received.get(timeout=max(.01,deadline-time.monotonic()))
            if message.get('id')==request_id:
                if 'error' in message:raise RuntimeError(str(message['error']))
                return message.get('result')
        raise TimeoutError('Codex thread rename timed out')
    try:
        send({'id':1,'method':'initialize','params':{'clientInfo':{'name':'agentchat','version':'1.0'}}})
        response(1)
        send({'method':'initialized'})
        send({'id':2,'method':'thread/name/set','params':{'threadId':session['native_id'],'name':session['name']}})
        response(2)
        session['native_named']=True
    finally:
        if process.stdin:
            try:process.stdin.close()
            except OSError:pass
        try:process.wait(timeout=3)
        except subprocess.TimeoutExpired:stop_tree(process)
