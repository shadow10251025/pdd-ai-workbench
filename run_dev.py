"""Key-free, loopback-only image-workbench entry point for source handoff.

This entry does not enable licensing administration or shop/browser operations.
Production desktop and rental entry points keep their existing authorization.
"""
from starlette.responses import JSONResponse


class DevelopmentBoundary:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        path = scope['path']
        if path == '/api/license/status' and scope['method'] == 'GET':
            return await JSONResponse({
                'ok': True, 'state': 'development', 'development_mode': True,
                'admin_mode': False, 'pack_ready': True, 'upgrade_needed': False,
                'used': 0, 'quota': 0, 'days_left': None, 'minutes_left': None,
                'machine_id': 'development', 'code': '', 'expiry': None,
            })(scope, receive, send)
        allowed = (
            path in {'/', '/api/config', '/api/health', '/api/session',
                     '/api/listing/preflight'}
            or path.startswith(('/static/', '/data/', '/api/ai/'))
            or path == '/api/migrated-products'
            or path.startswith('/api/migrated-products/')
            or scope['method'] == 'GET' and path.startswith('/api/one-click/')
        )
        if not allowed:
            return await JSONResponse({
                'detail': '开发入口仅用于图片工作台和商品草稿；店铺操作与授权管理未启用',
                'code': 'development_operation_disabled',
            }, 403)(scope, receive, send)
        await self.app(scope, receive, send)


def create_app():
    from local_security import LocalSessionBoundary
    from web.app import app, require_license

    app.dependency_overrides[require_license] = lambda: None
    app.add_middleware(DevelopmentBoundary)
    # Keep one session boundary outside developer-only responses. Two separate
    # boundaries would issue incompatible CSRF tokens.
    app.user_middleware.sort(key=lambda item: 0 if item.cls is LocalSessionBoundary else 1)
    return app


if __name__ == '__main__':
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser(description='本地图片工作台开发入口，不需要生产私钥')
    parser.add_argument('--port', type=int, default=3000)
    args = parser.parse_args()
    uvicorn.run(create_app(), host='127.0.0.1', port=args.port)
