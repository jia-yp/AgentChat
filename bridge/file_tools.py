"""Scoped chat tools for harnesses that can edit files but cannot run an MCP/CLI.

Only an active delivery is serviced. A durable immutable request and its nonce
survive HTTP uncertainty; another task/attempt never replays these files.
"""
import hashlib
import json
import re
import threading
from pathlib import Path
from urllib.error import URLError

from .chat_mcp import call_tool
from .common import APIError,Client,read_json,write_json


class FileTools:
    @staticmethod
    def folder(session,delivery):
        attempt=hashlib.sha256(delivery['lease'].encode()).hexdigest()[:16]
        return Path(session['context']).parent/'chat-tools'/delivery['id']/attempt

    @classmethod
    def instructions(cls,session,delivery):
        if not delivery.get('lease') or not delivery.get('id'):return ''
        folder=cls.folder(session,delivery)
        tools='members/history/send'+('/sessions（仅管理员明确要求增减时）' if session.get('session_type')=='manager' else '')
        return f'''若没有可用的群聊 MCP/CLI，可以直接用文件编辑工具提交 {tools}，无需 shell 或额外权限。
请求目录：{folder/'requests'}；结果目录：{folder/'receipts'}。
每个操作写一个新的英文文件名 .json，例如 note-1.json，内容为 {{"tool":"send","arguments":{{"body":"@目标 内容","nonce":"本次操作的稳定编号"}}}}。members 的 arguments 是空对象；history 可传 before/limit。manager 的 sessions 参数与专用工具相同。
发送附件时，在 send 的 arguments 中加 "files":["reports/成果.pdf"]。相对路径以共享项目根目录为基准。
gateway 只以你自己的身份执行本次任务中的请求；同名文件不可修改或复用。读取结果目录的同名 JSON，ok=true 才表示成功；需要结果时稍后再读，不循环等待或重复发送。最终回复照常直接输出，不要同时用文件发送同一条最终回复。'''

    def __init__(self,session,delivery,cancel,client=None):
        self.context={**session,'_task_cause':delivery['message']['id']}
        self.client=client or Client(session['url'],session['token'])
        self.root=self.folder(session,delivery)
        for name in ('requests','receipts','accepted'):(self.root/name).mkdir(parents=True,exist_ok=True)
        self.cancel=cancel;self.stop=threading.Event();self.lock=threading.RLock();self.thread=None

    def start(self):
        self.thread=threading.Thread(target=self.run,daemon=True,name='session-chat-tools');self.thread.start()

    def run(self):
        while not self.stop.is_set() and not self.cancel.is_set():
            try:self.drain()
            except OSError:pass
            self.stop.wait(.4)

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=1)

    def drain(self,final=False):
        complete=True
        with self.lock:
            for path in sorted((self.root/'requests').glob('*.json')):
                if self.cancel.is_set() or self.stop.is_set():return False
                if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}\.json',path.name):continue
                if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):continue
                receipt=self.root/'receipts'/path.name;accepted=self.root/'accepted'/path.name
                if receipt.exists():continue
                try:
                    if accepted.exists():saved=read_json(accepted)
                    else:
                        if path.stat().st_size>1024*1024:raise ValueError('请求文件超过 1 MiB；请缩小到单个群聊操作')
                        raw=path.read_bytes()
                        try:request=json.loads(raw.decode('utf-8-sig'))
                        except (json.JSONDecodeError,UnicodeDecodeError):
                            # File editors can expose a partial write. Only the
                            # final drain knows the native writer has finished.
                            if not final:complete=False;continue
                            raise ValueError('请求不是完整的 UTF-8 JSON') from None
                        saved={'request_sha256':hashlib.sha256(raw).hexdigest(),'request':request}
                        write_json(accepted,saved)
                    request=saved['request']
                    if not isinstance(request,dict) or set(request)!={'tool','arguments'} or not isinstance(request['arguments'],dict):
                        raise ValueError('请求需为 tool 和 arguments 对象')
                    if request['tool'] not in ('members','history','send','sessions'):raise ValueError('未知群聊工具')
                    result=call_tool(self.context,self.client,request['tool'],request['arguments'])
                    write_json(receipt,{'ok':True,'request_sha256':saved['request_sha256'],'result':result})
                except (URLError,TimeoutError,ConnectionError):complete=False
                except APIError as exc:
                    if exc.status>=500 or exc.status==429:complete=False
                    else:self.error(receipt,exc)
                except (ValueError,TypeError,KeyError) as exc:self.error(receipt,exc)
        return complete

    def error(self,path,error):
        message=str(error)
        if self.context.get('token'):message=message.replace(self.context['token'],'[redacted]')
        write_json(path,{'ok':False,'error':message[:2000]})
