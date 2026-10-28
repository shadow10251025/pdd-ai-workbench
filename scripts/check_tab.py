"""Check and screenshot a specific tab"""
import asyncio
import json
import websockets
import urllib.request
import base64
import sys

TAB_IDX = int(sys.argv[1]) if len(sys.argv) > 1 else 11
DATA_DIR = 'c:/Users/周泰斗/OneDrive/Desktop/一键上架功能/data'

async def main():
    tabs = json.loads(urllib.request.urlopen('http://127.0.0.1:9222/json').read())
    if TAB_IDX >= len(tabs):
        print(f'ERROR: Tab {TAB_IDX} does not exist')
        return

    ws_url = tabs[TAB_IDX]['webSocketDebuggerUrl']
    print(f'Tab {TAB_IDX}: {tabs[TAB_IDX]["url"][:80]}')

    try:
        async with websockets.connect(ws_url, max_size=50*1024*1024, close_timeout=5) as ws:
            msg_id = 0
            pending = {}

            async def recv_loop():
                try:
                    async for raw in ws:
                        msg = json.loads(raw)
                        if 'id' in msg:
                            pending[msg['id']] = msg
                except websockets.exceptions.ConnectionClosed:
                    pass

            recv_task = asyncio.create_task(recv_loop())
            await asyncio.sleep(0.3)

            async def send_cmd(method, params=None):
                nonlocal msg_id
                msg_id += 1
                mid = msg_id
                m = {'id': mid, 'method': method}
                if params:
                    m['params'] = params
                await ws.send(json.dumps(m))
                for _ in range(50):
                    if mid in pending:
                        return pending.pop(mid)
                    await asyncio.sleep(0.1)
                return None

            async def js(expr):
                r = await send_cmd('Runtime.evaluate', {
                    'expression': expr,
                    'returnByValue': True
                })
                if r and 'result' in r and 'result' in r['result']:
                    return r['result']['result'].get('value')
                return None

            # Get page info
            title = await js('document.title')
            url = await js('window.location.href')
            body_len = await js('document.body.innerText.length')
            print(f'Title: {title}')
            print(f'URL: {url[:100] if url else "N/A"}')
            print(f'Body text length: {body_len}')

            # Scroll to table area
            await js('window.scrollTo(0, 2200)')
            await asyncio.sleep(1)

            # Screenshot
            r = await send_cmd('Page.captureScreenshot', {
                'format': 'jpeg',
                'quality': 50
            })
            if r and 'result' in r:
                img = base64.b64decode(r['result']['data'])
                fname = f'{DATA_DIR}/tab{TAB_IDX}.jpg'
                with open(fname, 'wb') as f:
                    f.write(img)
                print(f'Screenshot saved to tab{TAB_IDX}.jpg ({len(img)} bytes)')
            else:
                print('Screenshot failed')

            # Check for key content
            has_specs = await js('''
            (function() {
                var text = document.body.innerText;
                return JSON.stringify({
                    has_31_40: text.includes('31*40'),
                    has_1398: text.includes('1398'),
                    has_sakura: text.includes('桜') || text.includes('樱'),
                    has_specs: text.includes('规格'),
                    first200: text.substring(0, 200)
                });
            })()
            ''')
            if has_specs:
                print(f'Content check: {has_specs}')

            recv_task.cancel()

    except Exception as e:
        print(f'Error: {e}')

asyncio.run(main())
