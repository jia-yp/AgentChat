from fastapi.responses import JSONResponse

from .uploads import is_upload_request


class BodyLimit:
    """Bound bodies even for chunked requests sent directly to the API port."""
    def __init__(self,app,limit=131072):
        self.app,self.limit=app,limit

    async def __call__(self,scope,receive,send):
        if (scope['type']!='http' or scope['method'] in ('GET','HEAD','OPTIONS')
                or is_upload_request(scope['method'], scope['path'])):
            return await self.app(scope,receive,send)
        chunks=[];size=0
        while True:
            event=await receive()
            if event['type']=='http.disconnect':return
            chunk=event.get('body',b'');size+=len(chunk)
            if size>self.limit:
                return await JSONResponse({'error':'请求过大'},status_code=413)(scope,receive,send)
            chunks.append(chunk)
            if not event.get('more_body',False):break
        delivered=False
        async def buffered_receive():
            nonlocal delivered
            if delivered:return await receive()
            delivered=True
            return {'type':'http.request','body':b''.join(chunks),'more_body':False}
        await self.app(scope,buffered_receive,send)
