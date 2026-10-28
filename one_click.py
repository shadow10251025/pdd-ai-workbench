"""
拼多多一键上架 - 全自动脚本
==========================================
用法: python one_click.py <淘宝/1688链接>

完整流程:
1. 打开淘宝页面，爬取标题、价格、规格、轮播图、详情图
2. 自动下载所有图片到本地（过滤水印图）
3. 自动填写拼多多表单（通过drop event模拟上传）
4. 不保存草稿、不发布

前置条件:
- Chrome已开启CDP调试: chrome --remote-debugging-port=9222
- 已登录拼多多商家后台并进入发布页面
- pip install websockets aiohttp
"""
import asyncio
import json
import os
import sys
import base64
import urllib.request
import urllib.parse
from pathlib import Path

if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

try:
    import websockets
    import aiohttp
except ImportError:
    print("请先安装: pip install websockets aiohttp")
    sys.exit(1)

# ============ 配置 ============
CDP_PORT = 9222
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / 'data'
IMAGES_DIR = DATA_DIR / 'product_images'
DETAIL_DIR = DATA_DIR / 'detail_images'
MIN_IMAGE_SIZE = 5000

for d in [DATA_DIR, IMAGES_DIR, DETAIL_DIR]:
    d.mkdir(parents=True, exist_ok=True)


# ============ CDP客户端 ============
class CDP:
    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self._pending = {}

    async def _recv_loop(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if 'id' in msg:
                    self._pending[msg['id']] = msg
        except:
            pass

    async def call(self, method, params=None):
        self._id += 1
        mid = self._id
        m = {'id': mid, 'method': method}
        if params:
            m['params'] = params
        await self.ws.send(json.dumps(m))
        for _ in range(200):
            if mid in self._pending:
                return self._pending.pop(mid)
            await asyncio.sleep(0.05)
        return None

    async def js(self, expr):
        r = await self.call('Runtime.evaluate', {
            'expression': expr, 'returnByValue': True, 'awaitPromise': True
        })
        if r and 'result' in r and 'result' in r['result']:
            v = r['result']['result']
            return v.get('value') if v.get('type') != 'undefined' else None
        return None

    async def screenshot(self, name):
        r = await self.call('Page.captureScreenshot', {'format': 'jpeg', 'quality': 70})
        if r and 'result' in r:
            with open(DATA_DIR / f'{name}.jpg', 'wb') as f:
                f.write(base64.b64decode(r['result']['data']))

    async def click(self, x, y):
        await self.call('Input.dispatchMouseEvent', {
            'type': 'mousePressed', 'x': x, 'y': y, 'button': 'left', 'clickCount': 1
        })
        await asyncio.sleep(0.05)
        await self.call('Input.dispatchMouseEvent', {
            'type': 'mouseReleased', 'x': x, 'y': y, 'button': 'left', 'clickCount': 1
        })


# ============ 标签页管理 ============
async def find_pdd_tab():
    tabs = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{CDP_PORT}/json').read())
    for t in tabs:
        url = t.get('url', '')
        if 'goods_add' in url or 'goods/category' in url:
            return t
    for t in tabs:
        if 'mms.pinduoduo.com' in t.get('url', '') and 'login' not in t.get('url', ''):
            return t
    return None


# ============ 第一步：爬取商品 ============
async def scrape_taobao(url):
    print(f'\n{"="*60}')
    print(f'  Scrape: {url}')
    print(f'{"="*60}')

    # 用CDP browser websocket创建新标签
    version = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{CDP_PORT}/json/version').read())
    bws = await websockets.connect(version['webSocketDebuggerUrl'], max_size=50*1024*1024)
    bcdp = CDP(bws)
    asyncio.create_task(bcdp._recv_loop())
    create_result = await bcdp.call('Target.createTarget', {'url': url})
    target_id = create_result['result']['targetId']
    await bws.close()
    await asyncio.sleep(2)

    # 找到新标签并连接
    tabs = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{CDP_PORT}/json').read())
    ws_url = None
    for t in tabs:
        if t.get('id') == target_id:
            ws_url = t['webSocketDebuggerUrl']
            break
    if not ws_url:
        for t in tabs:
            if 'taobao' in t.get('url', '') or '1688' in t.get('url', ''):
                ws_url = t['webSocketDebuggerUrl']
                break

    ws = await websockets.connect(ws_url, max_size=50*1024*1024)
    cdp = CDP(ws)
    asyncio.create_task(cdp._recv_loop())
    await asyncio.sleep(5)

    product = {
        'source_url': url,
        'platform': 'taobao' if 'taobao' in url or 'tmall' in url else '1688',
        'title': '', 'price': 0, 'images': [], 'detail_images': [],
        'colors': [], 'sizes': [], 'attributes': {}
    }

    # 标题
    product['title'] = await cdp.js('''
    (() => {
        let el = document.querySelector('[class*="ItemHeader--mainTitle"] span,
            [class*="mainTitle"] span, h3[class*="main"], h1');
        return el ? el.innerText.trim() : '';
    })()
    ''') or ''
    print(f'  Title: {product["title"][:50]}')

    # 价格
    price_info = await cdp.js('''
    (() => {
        let prices = [];
        document.querySelectorAll('[class*="Price"] span, [class*="price"] em').forEach(el => {
            let m = el.innerText.match(/(\\d+\\.?\\d*)/);
            if (m) prices.push(parseFloat(m[1]));
        });
        prices = prices.filter(p => p > 0).sort((a,b) => a - b);
        return JSON.stringify({min: prices[0] || 0, max: prices[prices.length-1] || 0});
    })()
    ''')
    if price_info:
        pi = json.loads(price_info)
        product['price'] = pi['min']
    print(f'  Price: {product["price"]}')

    # 轮播图 - 过滤小图（Logo/图标），只保留商品图
    images = await cdp.js('''
    (() => {
        let imgs = [];
        document.querySelectorAll('img').forEach(img => {
            let src = img.src || img.getAttribute('data-src') || '';
            if (src && src.startsWith('//')) src = 'https:' + src;
            if (!src || !src.includes('alicdn.com')) return;
            if (src.includes('favicon') || src.includes('logo') || src.includes('.svg')) return;
            // 过滤小图：宽度或高度小于200px的不是商品图
            let w = img.naturalWidth || img.width || 0;
            let h = img.naturalHeight || img.height || 0;
            if (w > 0 && h > 0 && (w < 200 || h < 200)) return;
            // 过滤太小的文件（通过URL特征判断）
            if (src.includes('_40x40') || src.includes('_50x50') || src.includes('_60x60')
                || src.includes('logo') || src.includes('icon')) return;
            imgs.push(src);
        });
        return JSON.stringify([...new Set(imgs)].slice(0, 15));
    })()
    ''')
    product['images'] = json.loads(images) if images else []
    print(f'  Images: {len(product["images"])}')

    # 规格
    specs = await cdp.js('''
    (() => {
        let colors = [], sizes = [];
        document.querySelectorAll('[class*="sku"] [class*="content"] [class*="item"]').forEach(el => {
            let text = el.innerText.trim();
            if (text && text.length < 20) {
                if (/^[\\u4e00-\\u9fa5\\w\\s]+$/.test(text) && !text.match(/\\d+[cmx]/i)) colors.push(text);
                else sizes.push(text);
            }
        });
        return JSON.stringify({colors: [...new Set(colors)], sizes: [...new Set(sizes)]});
    })()
    ''')
    if specs:
        sd = json.loads(specs)
        product['colors'] = sd.get('colors', [])
        product['sizes'] = sd.get('sizes', [])
    print(f'  Colors: {product["colors"][:5]}')
    print(f'  Sizes: {product["sizes"]}')

    # 详情图
    await cdp.js('window.scrollTo(0, document.body.scrollHeight)')
    await asyncio.sleep(2)
    detail_imgs = await cdp.js('''
    (() => {
        let imgs = [];
        document.querySelectorAll('[class*="detail"] img, [class*="Desc"] img').forEach(img => {
            let src = img.src || img.getAttribute('data-src') || '';
            if (src && src.includes('http') && !src.includes('favicon')) imgs.push(src.replace(/_\\d+x\\d+\\.jpg/, '.jpg'));
        });
        return JSON.stringify([...new Set(imgs)]);
    })()
    ''')
    product['detail_images'] = json.loads(detail_imgs) if detail_imgs else []
    print(f'  Detail images: {len(product["detail_images"])}')

    try:
        await cdp.call('Page.close')
    except:
        pass

    return product


# ============ 图片下载（带Referer防防盗链） ============
def _download_image(url, save_path):
    """下载单张图片，带 Referer 头绕过淘宝防盗链"""
    if url.startswith('//'):
        url = 'https:' + url
    # 清理URL：去掉 .jpg 后面的 _q50.jpg_.webp 等后缀
    jpg_pos = url.find('.jpg')
    if jpg_pos > 0:
        url = url[:jpg_pos + 4]
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://item.taobao.com/'
        })
        data = urllib.request.urlopen(req, timeout=15).read()
        if len(data) < MIN_IMAGE_SIZE:
            return False
        with open(save_path, 'wb') as f:
            f.write(data)
        return True
    except:
        return False


async def download_images(product):
    print(f'\n{"="*60}')
    print(f'  Download images')
    print(f'{"="*60}')

    for d in [IMAGES_DIR, DETAIL_DIR]:
        for f in d.iterdir():
            if f.is_file():
                f.unlink()

    # 轮播图
    good_images = []
    for i, url in enumerate(product['images']):
        filename = f'carousel_{len(good_images)+1:02d}.jpg'
        filepath = IMAGES_DIR / filename
        if _download_image(url, filepath):
            good_images.append(filepath)
            print(f'  [{len(good_images)}] OK: {filename} ({filepath.stat().st_size} bytes)')
        else:
            print(f'  [{i+1}] FAIL: {url[:60]}')
    product['local_images'] = [str(p) for p in good_images]
    print(f'  Carousel: {len(good_images)} images')

    # 详情图
    detail_local = []
    for i, url in enumerate(product['detail_images']):
        filename = f'detail_{len(detail_local)+1:02d}.jpg'
        filepath = DETAIL_DIR / filename
        if _download_image(url, filepath):
            detail_local.append(filepath)
    product['local_detail_images'] = [str(p) for p in detail_local]
    print(f'  Detail: {len(detail_local)} images')

    return product


# ============ 第三步：填写PDD表单 ============
async def fill_pdd(product):
    print(f'\n{"="*60}')
    print(f'  Fill PDD form')
    print(f'{"="*60}')

    tab = await find_pdd_tab()
    if not tab:
        print('  No PDD tab found!')
        return False

    print(f'  Tab: {tab["url"][:60]}')

    ws = await websockets.connect(tab['webSocketDebuggerUrl'], max_size=50*1024*1024)
    cdp = CDP(ws)
    asyncio.create_task(cdp._recv_loop())
    await asyncio.sleep(0.5)

    # === 1. 上传轮播图（先上传到PDD服务器，再写入组件状态）===
    print('\n  --- Upload carousel ---')
    good_images = sorted([p for p in IMAGES_DIR.iterdir() if p.is_file() and p.stat().st_size >= 5000])
    print(f'  Found {len(good_images)} images to upload')

    # 准备所有图片的base64
    imgs_b64 = {}
    for img_path in good_images:
        img_data = img_path.read_bytes()
        imgs_b64[img_path.name] = base64.b64encode(img_data).decode()

    # 新方法：逐个上传到PDD服务器，获取URL后通过onUploadBegin+onUploadEnd写入状态
    files_json = json.dumps(imgs_b64)
    upload_result = await cdp.js(f'''(async () => {{
        const imgs = {files_json};
        const imgEntries = Object.entries(imgs);
        const results = [];

        // 获取PDD上传签名
        const sigResp = await fetch("/galerie/business/get_signature", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{ bucket_tag: "beast" }})
        }});
        const sigData = await sigResp.json();
        const signature = sigData.result && sigData.result.signature;
        if (!signature) return "NO_SIG";

        // 找到onUploadBegin/onUploadEnd组件
        const inp = document.querySelector("input[type=file]");
        if (!inp) return "NO_INPUT";
        let fiberKey = Object.keys(inp).find(k => k.startsWith("__reactInternalInstance"));
        if (!fiberKey) return "NO_FIBER";
        let fiber = inp[fiberKey];

        let onBegin = null, onEnd = null;
        let current = fiber;
        for (let i = 0; i < 30 && current; i++) {{
            if (current.memoizedProps && current.memoizedProps.onUploadBegin) {{
                onBegin = current.memoizedProps.onUploadBegin;
                onEnd = current.memoizedProps.onUploadEnd;
                break;
            }}
            current = current.return;
        }}
        if (!onBegin) return "NO_HANDLER";

        // 逐个上传图片
        let idx = 0;
        for (const [name, b64] of imgEntries) {{
            try {{
                // base64 -> Blob -> File
                const byteChars = atob(b64);
                const byteArray = new Uint8Array(byteChars.length);
                for (let i = 0; i < byteChars.length; i++) {{
                    byteArray[i] = byteChars.charCodeAt(i);
                }}
                const blob = new Blob([byteArray], {{ type: "image/jpeg" }});
                const file = new File([blob], name, {{ type: "image/jpeg" }});

                // 上传到PDD
                const formData = new FormData();
                formData.append("upload_sign", signature);
                formData.append("image", file, name);
                formData.append("forbid_override", "false");

                const resp = await fetch("https://file.pinduoduo.com/v3/store_image", {{
                    method: "POST",
                    body: formData
                }});
                const data = await resp.json();
                if (!data || !data.url) {{
                    results.push(name + ":UPLOAD_ERR");
                    continue;
                }}

                const uploadId = "upload_" + idx + "_" + Date.now();
                // onUploadBegin: 添加到组件state（显示loading）
                onBegin([{{ id: uploadId, base64: b64.substring(0, 100), name: name }}]);
                await new Promise(r => setTimeout(r, 200));
                // onUploadEnd: 更新为真实URL（完成上传）
                onEnd([{{
                    id: uploadId,
                    file_id: data.url.split("/").pop().split("?")[0],
                    url: data.url,
                    name: name,
                    base64: ""
                }}]);

                results.push(name + ":OK");
                idx++;
                await new Promise(r => setTimeout(r, 300));
            }} catch(e) {{
                results.push(name + ":ERR_" + e.message);
            }}
        }}

        return results.join("|");
    }})()''')
    print(f'  Upload result: {upload_result}')

    # 等待AI分类识别完成
    print('  Waiting for AI category recognition...')
    for i in range(20):
        await asyncio.sleep(2)
        status = await cdp.js('''(() => {
            const text = document.body.innerText;
            if (text.includes("为您推荐分类") || text.includes("已为您识别")) return "CATEGORY_FOUND";
            if (text.includes("识别中")) return "RECOGNIZING";
            return "WAITING";
        })()''')
        print(f'  [{(i+1)*2}s] {status}')
        if status == "CATEGORY_FOUND":
            break

    await cdp.screenshot('pdd_images')

    # === 2. AI智能推荐标题 ===
    print('\n  --- AI title recommendation ---')
    # 先等分类识别完成
    await asyncio.sleep(3)

    # 点击"AI智能推荐标题"然后"立即使用"
    ai_title = await cdp.js('''(() => {
        let btns = document.querySelectorAll("button, a, span, div");
        for (let b of btns) {
            let text = (b.innerText || "").trim();
            if (text.includes("AI") && text.includes("标题")) {
                b.click();
                return 'CLICKED_AI_TITLE';
            }
        }
        return 'NOT_FOUND';
    })()''')
    print(f'  AI title button: {ai_title}')
    await asyncio.sleep(3)

    # 点击"立即使用"
    use_result = await cdp.js('''(() => {
        let btns = document.querySelectorAll("button, a, span, div");
        for (let b of btns) {
            let text = (b.innerText || "").trim();
            if (text === "立即使用" || text === "使用") {
                b.click();
                return 'CLICKED_USE';
            }
        }
        return 'NOT_FOUND';
    })()''')
    print(f'  Use title: {use_result}')
    await asyncio.sleep(2)

    # === 3. 点击"一键复用"填充属性 ===
    print('\n  --- Auto fill attributes ---')
    reuse = await cdp.js('''(() => {
        let btns = document.querySelectorAll("button, a, span, div");
        for (let b of btns) {
            let text = (b.innerText || "").trim();
            if (text.includes("一键复用") || text === "复用") {
                b.click();
                return 'CLICKED_REUSE';
            }
        }
        return 'NOT_FOUND';
    })()''')
    print(f'  Reuse attrs: {reuse}')
    await asyncio.sleep(3)

    # === 4. 填写品牌 ===
    brand = product.get('brand', '')
    if brand:
        print(f'\n  --- Fill brand: {brand} ---')
        await cdp.js(f'''(() => {{
            let inputs = document.querySelectorAll("input");
            for (let inp of inputs) {{
                let ph = (inp.placeholder || "").toLowerCase();
                if (ph.includes("品牌") || ph.includes("brand")) {{
                    let setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                    setter.call(inp, "{brand}");
                    inp.dispatchEvent(new Event("input", {{bubbles: true}}));
                    inp.dispatchEvent(new Event("change", {{bubbles: true}}));
                    return true;
                }}
            }}
            return false;
        }})()''')
        await asyncio.sleep(1)

    # === 5. 点击下一步 ===
    print('\n  --- Next step ---')
    await asyncio.sleep(1)
    await cdp.js('''
    (() => {
        let btns = document.querySelectorAll("button");
        for (let b of btns) {
            if (b.innerText.includes("下一步")) { b.click(); return true; }
        }
    })()
    ''')
    await asyncio.sleep(5)

    url = await cdp.js('window.location.href')
    print(f'  URL: {url[:80]}')

    # === 6. 在表单页面填写标题（如果还没填）===
    title = product.get('title', '')[:30]
    if title:
        await cdp.js(f'''(() => {{
            let inputs = document.querySelectorAll("input, textarea");
            for (let inp of inputs) {{
                let ph = (inp.placeholder || "");
                if (ph.includes("商品标题") || ph.includes("标题")) {{
                    let setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                    setter.call(inp, "{title}");
                    inp.dispatchEvent(new Event("input", {{bubbles: true}}));
                    inp.dispatchEvent(new Event("change", {{bubbles: true}}));
                    return true;
                }}
            }}
            return false;
        }})()''')
        print(f'  Title filled: {title[:30]}')

    await cdp.screenshot('pdd_form')

    # === 7. 填写价格和库存 ===
    print('\n  --- Fill price ---')
    price = product.get('price', 0)
    if price > 0:
        price_filled = await cdp.js(f'''
        (() => {{
            let inputs = document.querySelectorAll("input");
            let count = 0;
            for (let inp of inputs) {{
                let ph = (inp.placeholder || "").toLowerCase();
                if (ph.includes("价格") || ph.includes("¥")) {{
                    let setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                    setter.call(inp, "{price:.2f}");
                    inp.dispatchEvent(new Event("input", {{bubbles: true}}));
                    inp.dispatchEvent(new Event("change", {{bubbles: true}}));
                    count++;
                }}
            }}
            return count;
        }})()
        ''')
        print(f'  Price inputs: {price_filled}')

    # === 8. 上传详情图 ===
    print('\n  --- Upload detail images ---')
    detail_images = sorted([p for p in DETAIL_DIR.iterdir() if p.is_file() and p.stat().st_size >= 5000])
    if detail_images:
        # 滚动到详情图区域
        await cdp.js('''
        (() => {
            let els = document.querySelectorAll("*");
            for (let e of els) {
                if ((e.innerText || "").includes("商品详情") && e.offsetHeight > 0) {
                    e.scrollIntoView({block: "start"});
                    return true;
                }
            }
            window.scrollBy(0, 2000);
        })()
        ''')
        await asyncio.sleep(1)

        detail_b64 = {}
        for dp in detail_images[:20]:
            detail_b64[dp.name] = base64.b64encode(dp.read_bytes()).decode()

        detail_json = json.dumps(detail_b64)
        detail_result = await cdp.js(f'''(async () => {{
            const imgs = {detail_json};
            const fileInputs = document.querySelectorAll("input[type=file]");
            // 找详情图区域的file input（通常在页面下方）
            let targetInput = null;
            for (let inp of fileInputs) {{
                let parent = inp.closest('[class*=detail], [class*=Detail], [class*=desc], [class*=Desc]');
                if (parent) {{ targetInput = inp; break; }}
            }}
            if (!targetInput && fileInputs.length > 1) {{
                targetInput = fileInputs[fileInputs.length - 1];
            }}
            if (!targetInput) return 'NO_INPUT';

            let fiberKey = Object.keys(targetInput).find(k => k.startsWith('__reactInternalInstance'));
            if (!fiberKey) return 'NO_FIBER';
            let fiber = targetInput[fiberKey];

            let current = fiber;
            for (let i = 0; i < 20 && current; i++) {{
                if (current.memoizedProps && current.memoizedProps.onUploadBegin) {{
                    const onBegin = current.memoizedProps.onUploadBegin;
                    const files = [];
                    let idx = 0;
                    for (const [name, b64] of Object.entries(imgs)) {{
                        files.push({{ id: 'detail_' + idx + '_' + Date.now(), base64: b64, name: name }});
                        idx++;
                    }}
                    onBegin(files);
                    return 'OK_' + files.length;
                }}
                current = current.return;
            }}
            return 'NO_UPLOAD_HANDLER';
        }})()''')
        print(f'  Detail upload: {detail_result}')
        await asyncio.sleep(10)

    # 截图
    await cdp.screenshot('pdd_final')

    print(f'\n{"="*60}')
    print('  DONE! No draft saved, no product published')
    print(f'{"="*60}')

    await ws.close()
    return True


# ============ 主流程 ============
async def main():
    if len(sys.argv) < 2:
        print('Usage: python one_click.py <taobao/1688 URL>')
        sys.exit(1)

    url = sys.argv[1].strip()
    if 'taobao.com' not in url and '1688.com' not in url and 'tmall.com' not in url:
        print('Please provide taobao, tmall or 1688 URL')
        sys.exit(1)

    print(f'URL: {url}')

    try:
        urllib.request.urlopen(f'http://127.0.0.1:{CDP_PORT}/json/version', timeout=3)
    except:
        print('Chrome CDP not running!')
        print('Start: chrome.exe --remote-debugging-port=9222')
        sys.exit(1)

    # 1. Scrape
    product = await scrape_taobao(url)

    # Save data
    with open(DATA_DIR / 'taobao_product.json', 'w', encoding='utf-8') as f:
        json.dump(product, f, ensure_ascii=False, indent=2)

    # 2. Download
    product = await download_images(product)

    # Update data
    with open(DATA_DIR / 'taobao_product.json', 'w', encoding='utf-8') as f:
        json.dump(product, f, ensure_ascii=False, indent=2)

    # 3. Fill PDD
    await fill_pdd(product)


if __name__ == '__main__':
    asyncio.run(main())
