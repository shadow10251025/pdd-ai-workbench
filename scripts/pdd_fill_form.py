"""Generic PDD product listing script - fill form from Taobao data"""
import asyncio
import json
import websockets
import urllib.request
import base64
import os
import sys

# ============ CONFIG ============
# Change this URL to switch products
TAOBAO_DATA = 'c:/Users/周泰斗/OneDrive/Desktop/一键上架功能/data/taobao_product.json'
PRODUCT_IMAGES = 'c:/Users/周泰斗/OneDrive/Desktop/一键上架功能/data/product_images'
DETAIL_IMAGES_DIR = 'c:/Users/周泰斗/OneDrive/Desktop/一键上架功能/data/detail_images'
DATA_DIR = 'c:/Users/周泰斗/OneDrive/Desktop/一键上架功能/data'
# ================================

async def get_pdd_tab():
    """Find the PDD goods_add or goods category tab"""
    tabs = json.loads(urllib.request.urlopen('http://127.0.0.1:9222/json').read())
    for i, tab in enumerate(tabs):
        url = tab['url']
        if 'goods_add' in url or 'goods/category' in url:
            return i, tab['webSocketDebuggerUrl']
    # fallback to any mms tab
    for i, tab in enumerate(tabs):
        if 'mms.pinduoduo.com' in tab['url'] and 'login' not in tab['url']:
            return i, tab['webSocketDebuggerUrl']
    return None, None

async def main():
    tab_idx, ws_url = await get_pdd_tab()
    if not ws_url:
        print('ERROR: No PDD tab found!')
        return
    print(f'Using tab {tab_idx}: {ws_url[:60]}')

    # Load product data
    with open(TAOBAO_DATA, 'r', encoding='utf-8') as f:
        product = json.load(f)
    print(f'Product: {product["title"][:50]}')

    async with websockets.connect(ws_url, max_size=50*1024*1024) as ws:
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

        asyncio.create_task(recv_loop())
        await asyncio.sleep(0.3)

        async def send_cmd(method, params=None):
            nonlocal msg_id
            msg_id += 1
            mid = msg_id
            m = {'id': mid, 'method': method}
            if params:
                m['params'] = params
            await ws.send(json.dumps(m))
            for _ in range(100):
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

        async def click(x, y):
            await send_cmd('Input.dispatchMouseEvent', {
                'type': 'mousePressed', 'x': x, 'y': y,
                'button': 'left', 'clickCount': 1
            })
            await asyncio.sleep(0.05)
            await send_cmd('Input.dispatchMouseEvent', {
                'type': 'mouseReleased', 'x': x, 'y': y,
                'button': 'left', 'clickCount': 1
            })

        async def screenshot(name):
            r = await send_cmd('Page.captureScreenshot', {'format': 'jpeg', 'quality': 60})
            if r and 'result' in r:
                with open(f'{DATA_DIR}/{name}.jpg', 'wb') as f:
                    f.write(base64.b64decode(r['result']['data']))
                print(f'  Screenshot: {name}.jpg')

        # ============ STEP 1: Check current page ============
        url = await js('window.location.href')
        print(f'Current URL: {url[:100] if url else "N/A"}')

        body = await js('document.body.innerText.substring(0, 2000)')
        print(f'Page text preview: {(body or "")[:200]}')

        # ============ STEP 2: Upload main images (轮播图) ============
        print('\n=== Step 2: Upload carousel images ===')

        # Get file input
        r = await send_cmd('DOM.getDocument', {})
        root_id = r['result']['root']['nodeId']
        r2 = await send_cmd('DOM.querySelectorAll', {
            'nodeId': root_id,
            'selector': 'input[type=file]'
        })
        file_inputs = r2['result']['nodeIds']
        print(f'Found {len(file_inputs)} file input(s)')

        if file_inputs:
            # Upload good carousel images
            good_images = []
            for name in ['carousel_1.jpg', 'carousel_2.jpg', 'carousel_3.jpg',
                         'carousel_4.jpg', 'carousel_5.jpg', 'carousel_6.jpg', 'carousel_9.png']:
                path = os.path.join(PRODUCT_IMAGES, name).replace('\\', '/')
                if os.path.exists(path.replace('/', '\\')):
                    good_images.append(path)

            print(f'Uploading {len(good_images)} images...')
            r3 = await send_cmd('DOM.setFileInputFiles', {
                'nodeId': file_inputs[0],
                'files': good_images
            })
            print(f'Upload result: {str(r3)[:100]}')
            await asyncio.sleep(5)
            await screenshot('step2_images')

        # ============ STEP 3: Fill title ============
        print('\n=== Step 3: Fill title ===')
        title = product.get('title', '')[:30]  # Max 30 Chinese chars
        print(f'Title: {title}')

        # Find title input
        title_result = await js(f'''
        (function() {{
            var inputs = document.querySelectorAll("input, textarea");
            for (var i = 0; i < inputs.length; i++) {{
                var ph = inputs[i].placeholder || "";
                if (ph.includes("商品标题") || ph.includes("标题") || ph.includes("规格")) {{
                    var r = inputs[i].getBoundingClientRect();
                    return JSON.stringify({{idx: i, x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)}});
                }}
            }}
            return "NOT_FOUND";
        }})()
        ''')
        print(f'Title input: {title_result}')

        if title_result and title_result != 'NOT_FOUND':
            tinfo = json.loads(title_result)
            # Click to focus
            await click(tinfo['x'], tinfo['y'])
            await asyncio.sleep(0.3)
            # Type title using insertText
            await send_cmd('Input.insertText', {'text': title})
            await asyncio.sleep(0.5)
            print('Title filled!')

        await screenshot('step3_title')

        # ============ DONE ============
        print('\n=== Form filling complete (test mode - NOT saving) ===')
        print('Review the form manually. DO NOT click publish or save draft.')

asyncio.run(main())
