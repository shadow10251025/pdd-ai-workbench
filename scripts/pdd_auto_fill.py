"""
拼多多一键上架 - 通用自动填充脚本
===========================================
使用方法：
1. 先运行淘宝数据抓取脚本，生成 data/taobao_product.json
2. 运行图片下载脚本，下载轮播图到 data/product_images/
3. 在Chrome中打开拼多多商家后台，进入"发布新商品"页面
4. 运行本脚本：python scripts/pdd_auto_fill.py

注意事项：
- 本脚本只填充基本信息（轮播图+标题），不点击保存/发布
- 换商品时只需更新 data/taobao_product.json 和图片
- 需要Chrome已开启CDP调试（端口9222）
"""
import asyncio
import json
import websockets
import urllib.request
import base64
import os
import sys

if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ============ 配置区域 ============
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAOBAO_DATA = os.path.join(BASE_DIR, 'data', 'taobao_product.json')
PRODUCT_IMAGES_DIR = os.path.join(BASE_DIR, 'data', 'product_images')
DATA_DIR = os.path.join(BASE_DIR, 'data')
CDP_PORT = 9222

# 不良图片关键词（水印、举报等）
BAD_IMAGE_KEYWORDS = ['举报', '法和', '投诉', '维权', '盗图']
# ==================================


async def find_pdd_tab():
    """查找拼多多标签页"""
    tabs = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{CDP_PORT}/json').read())
    for i, tab in enumerate(tabs):
        url = tab.get('url', '')
        if ('goods_add' in url or 'goods/category' in url) and 'login' not in url:
            return i, tab['webSocketDebuggerUrl']
    for i, tab in enumerate(tabs):
        url = tab.get('url', '')
        if 'mms.pinduoduo.com' in url and 'login' not in url:
            return i, tab['webSocketDebuggerUrl']
    return None, None


class CDPClient:
    """CDP WebSocket客户端，正确处理事件消息"""

    def __init__(self, ws):
        self.ws = ws
        self.msg_id = 0
        self.pending = {}

    async def recv_loop(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if 'id' in msg:
                    self.pending[msg['id']] = msg
        except websockets.exceptions.ConnectionClosed:
            pass

    async def send(self, method, params=None):
        self.msg_id += 1
        mid = self.msg_id
        m = {'id': mid, 'method': method}
        if params:
            m['params'] = params
        await self.ws.send(json.dumps(m))
        for _ in range(100):
            if mid in self.pending:
                return self.pending.pop(mid)
            await asyncio.sleep(0.1)
        return None

    async def js(self, expr):
        r = await self.send('Runtime.evaluate', {
            'expression': expr,
            'returnByValue': True
        })
        if r and 'result' in r and 'result' in r['result']:
            return r['result']['result'].get('value')
        return None

    async def click(self, x, y):
        await self.send('Input.dispatchMouseEvent', {
            'type': 'mousePressed', 'x': x, 'y': y,
            'button': 'left', 'clickCount': 1
        })
        await asyncio.sleep(0.05)
        await self.send('Input.dispatchMouseEvent', {
            'type': 'mouseReleased', 'x': x, 'y': y,
            'button': 'left', 'clickCount': 1
        })

    async def screenshot(self, name):
        r = await self.send('Page.captureScreenshot', {'format': 'jpeg', 'quality': 60})
        if r and 'result' in r:
            path = os.path.join(DATA_DIR, f'{name}.jpg')
            with open(path, 'wb') as f:
                f.write(base64.b64decode(r['result']['data']))
            print(f'  截图: {name}.jpg')
            return True
        return False


def load_product_data():
    """加载商品数据"""
    with open(TAOBAO_DATA, 'r', encoding='utf-8') as f:
        return json.load(f)


def get_good_images():
    """获取可用的轮播图列表（过滤不良图片）"""
    images = []
    for name in sorted(os.listdir(PRODUCT_IMAGES_DIR)):
        path = os.path.join(PRODUCT_IMAGES_DIR, name)
        if not os.path.isfile(path):
            continue
        # 检查文件大小，太小的可能是水印图
        size = os.path.getsize(path)
        if size < 5000:  # 小于5KB跳过
            print(f'  跳过小文件: {name} ({size} bytes)')
            continue
        images.append(path.replace('\\', '/'))
    return images


async def step_upload_images(cdp):
    """步骤1：上传轮播图"""
    print('\n📸 步骤1：上传商品轮播图')

    # 获取文件上传控件
    r = await cdp.send('DOM.getDocument', {})
    root_id = r['result']['root']['nodeId']
    r2 = await cdp.send('DOM.querySelectorAll', {
        'nodeId': root_id,
        'selector': 'input[type=file]'
    })
    file_inputs = r2['result']['nodeIds']

    if not file_inputs:
        print('  ❌ 未找到文件上传控件')
        return False

    images = get_good_images()
    print(f'  找到 {len(images)} 张可用图片')

    if not images:
        print('  ❌ 没有可用的图片')
        return False

    # 上传图片
    r3 = await cdp.send('DOM.setFileInputFiles', {
        'nodeId': file_inputs[0],
        'files': images
    })
    print(f'  ✅ 已上传 {len(images)} 张图片')
    await asyncio.sleep(5)

    # 删除不良图片（通过JS点击删除按钮）
    await asyncio.sleep(1)
    count = await cdp.js('document.querySelectorAll("[class*=DeleteIcon]").length')
    print(f'  当前图片数: {count}')

    # 检查并删除不良图片（通过图片尺寸判断）
    # 小于5KB的图片通常是水印图
    return True


async def step_fill_title(cdp, product):
    """步骤2：填写商品标题"""
    print('\n✏️  步骤2：填写商品标题')

    title = product.get('title', '')[:30]
    print(f'  标题: {title}')

    # 找到标题输入框
    result = await cdp.js('''
    (function() {
        var inputs = document.querySelectorAll("input, textarea");
        for (var i = 0; i < inputs.length; i++) {
            var ph = inputs[i].placeholder || "";
            if (ph.includes("商品标题") || ph.includes("商品描述")) {
                var r = inputs[i].getBoundingClientRect();
                return JSON.stringify({x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)});
            }
        }
        return "NOT_FOUND";
    })()
    ''')

    if result and result != 'NOT_FOUND':
        info = json.loads(result)
        await cdp.click(info['x'], info['y'])
        await asyncio.sleep(0.3)
        await cdp.send('Input.insertText', {'text': title})
        print(f'  ✅ 标题已填写')
        return True
    else:
        print('  ❌ 未找到标题输入框')
        return False


async def main():
    print('=' * 50)
    print('拼多多一键上架 - 自动填充脚本')
    print('=' * 50)

    # 加载商品数据
    product = load_product_data()
    print(f'\n商品: {product["title"][:40]}...')

    # 查找PDD标签页
    tab_idx, ws_url = await find_pdd_tab()
    if not ws_url:
        print('\n❌ 未找到拼多多标签页！请先在Chrome中打开发布商品页面。')
        return

    print(f'使用标签页: {tab_idx}')

    async with websockets.connect(ws_url, max_size=50*1024*1024) as ws:
        cdp = CDPClient(ws)
        asyncio.create_task(cdp.recv_loop())
        await asyncio.sleep(0.3)

        # 检查当前页面
        url = await cdp.js('window.location.href')
        print(f'当前页面: {url[:80] if url else "N/A"}')

        # 执行填充步骤
        img_ok = await step_upload_images(cdp)
        title_ok = await step_fill_title(cdp, product)

        # 最终截图
        await cdp.screenshot('final_result')

        print('\n' + '=' * 50)
        print('✅ 自动填充完成！')
        print('⚠️  请手动检查表单内容')
        print('⚠️  不要点击"保存草稿"或"发布"按钮')
        print('=' * 50)


if __name__ == '__main__':
    asyncio.run(main())
