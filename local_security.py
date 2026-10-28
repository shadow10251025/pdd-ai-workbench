"""Same-origin boundary for the loopback desktop service."""
import secrets
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.exceptions import HTTPException


class LocalSessionBoundary:
    def __init__(self, app):
        self.app = app
        self.token = secrets.token_urlsafe(32)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope['headers']}
        host = headers.get('host', '')
        parsed = urlsplit('http://' + host)
        if parsed.hostname not in {'127.0.0.1', 'localhost', '::1', 'testserver'}:
            return await JSONResponse({'detail': '本地管理端仅接受回环地址'}, 403)(scope, receive, send)
        origin = headers.get('origin', '')
        if origin and urlsplit(origin).netloc != host:
            return await JSONResponse({'detail': '请从本地管理端页面操作'}, 403)(scope, receive, send)
        if headers.get('sec-fetch-site') == 'cross-site':
            return await JSONResponse({'detail': '不接受其他网站的请求'}, 403)(scope, receive, send)
        if scope['path'] == '/api/session' and scope['method'] == 'GET':
            return await JSONResponse({'session_token': self.token}, headers={'Cache-Control': 'no-store'})(scope, receive, send)
        if scope['method'] not in {'GET', 'HEAD', 'OPTIONS'} and not secrets.compare_digest(headers.get('x-pdd-session', ''), self.token):
            return await JSONResponse({'detail': '管理端会话已更新，请刷新页面'}, 403)(scope, receive, send)
        try:
            declared = int(headers.get('content-length', '0'))
        except ValueError:
            declared = 32 * 1024 * 1024 + 1
        if declared < 0 or declared > 32 * 1024 * 1024:
            return await JSONResponse({'detail': '上传请求超过32MB限制'}, 413)(scope, receive, send)
        total = 0
        async def bounded_receive():
            nonlocal total
            event = await receive()
            total += len(event.get('body', b''))
            if total > 32 * 1024 * 1024:
                raise HTTPException(413, '上传请求超过32MB限制')
            return event
        await self.app(scope, bounded_receive, send)
