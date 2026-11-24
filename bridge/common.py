import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


class APIError(Exception):
    def __init__(self,status,message):
        self.status=status
        super().__init__(message)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        # Never forward bearer credentials to a redirected host.
        raise APIError(code,'服务地址返回重定向，请使用最终 API 地址')


class Client:
    def __init__(self,url,token):
        self.url=url.rstrip('/')
        self.token=token
        self.opener=urllib.request.build_opener(NoRedirect())

    def _open(self,path,data=None,timeout=20):
        req=urllib.request.Request(self.url+path,data=json.dumps(data,ensure_ascii=False).encode() if data is not None else None,
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json','User-Agent':'AgentChat-Bridge/1'})
        try:
            return self.opener.open(req,timeout=timeout)
        except urllib.error.HTTPError as exc:
            try:
                payload=json.load(exc)
                message=payload.get('error',str(payload.get('detail','API error')))
            except (ValueError,TypeError):
                message='HTTP '+str(exc.code)
            raise APIError(exc.code,message) from None

    def call(self,path,data=None,timeout=20):
        with self._open(path,data,timeout) as response:
            return json.load(response)

    def download(self,path,size,timeout=20):
        with self._open(path,timeout=timeout) as response:
            return response.read(size+1)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path,data):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(data,stream,ensure_ascii=False,indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        # On Windows a reader (status command, indexer, antivirus) can briefly
        # deny replacement. Preserve atomic writes and retry the same temp file.
        for attempt in range(8):
            try:
                os.replace(tmp,path)
                break
            except PermissionError:
                if attempt==7:raise
                time.sleep(.01*2**attempt)
        path.chmod(0o600)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class ProcessLock:
    def __init__(self,path):
        self.path=Path(path)

    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.file=open(self.path,'a+b')
        if self.file.tell()==0:
            self.file.write(b'0');self.file.flush()
        self.file.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('这个配置已有 bridge 正在运行') from None
        return self

    def __exit__(self,*args):
        self.file.close()
