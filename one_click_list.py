"""
拼多多一键上架 - 核心流程模块
可复用的浏览器自动化流程，通过CDP控制Chrome完成商品上架。

流程:
1. 填充规格（新版「选择或输入主色/自定义尺码」，或手动「添加规格类型」逐个填值）
2. 价格库存批量填写 (导出Excel → 填充价格库存 → 上传导入)
3. 商品详情图片上传
4. 规格预览图上传

关键约束:
- 不使用官方API，全程浏览器自动化
- 不点击发布/提交/保存草稿
"""

import asyncio
import contextvars
import json
import os
import re
import sys
import time
import io
import html
import base64
import glob
import zipfile
import urllib.request
from urllib.parse import quote
from xml.sax import saxutils
import websockets

# 授权校验：核心功能入口都会调 license.guard()，让「改了 API 层的门禁」也不能白用
# （为什么校验要下沉到这些函数里，见 license.py 里 guard() 的说明）
import license

from asset_store import load as _A


def _js_string(value) -> str:
    """将用户/模型输入安全地嵌入 Runtime.evaluate 的 JavaScript 字符串。"""
    return json.dumps(str(value or ''), ensure_ascii=False)

# Windows编码修复
if sys.platform == 'win32':
    # 包一层只为把编码定成 UTF-8（中文系统控制台默认 GBK，中文日志会变乱码）。
    # line_buffering=True 不能省：新包装默认是**块缓冲**，print 要攒满 8KB 才落盘，
    # 于是「跑的时候实时看日志」全成了瞎猜（挂掉被强杀时，缓冲里的还直接丢）。
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace',
                                  line_buffering=True)


# ==================== CDP工具函数 ====================

def open_new_tab(port, url, timeout=10):
    """让 Chrome 新开一个标签页，返回该标签页信息。

    /json/new 在新版 Chrome 上必须用 PUT（用 GET 会返回 405 Method Not Allowed，
    表现就是「打不开新标签页」），所以先 PUT，老版本不认再退回 GET。
    同时加上超时：Chrome 没起来时原来会一直挂在这里。
    """
    target = f'http://127.0.0.1:{port}/json/new?{quote(url)}'
    last = None
    for method in ('PUT', 'GET'):
        try:
            req = urllib.request.Request(target, method=method)
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except Exception as e:
            last = e
    raise Exception(f'打开新标签页失败（{url}）：{last}')


class CDPClient:
    """Chrome DevTools Protocol 客户端"""

    def __init__(self, ws):
        self.ws = ws
        self.pending = {}
        self.mid = 0
        self._recv_task = None
        self.closed_error = None   # 接收循环挂掉的原因（连接断了/页面关了），供 call() 快速失败
        self.events = []  # 收集所有带 method 的 CDP 事件（如网络请求），供上层统一读取

    async def start(self):
        """启动接收循环"""
        async def recv():
            try:
                async for raw in self.ws:
                    msg = json.loads(raw)
                    if 'id' in msg:
                        self.pending[msg['id']] = msg
                    elif 'method' in msg:
                        self.events.append(msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 原来是裸的 except: pass —— 连接一断谁也不知道，后面每个 call() 都白等 10 秒
                # 然后返回 None，上层只看到「什么都没发生」，根本查不出是连接断了。
                self.closed_error = f'{type(e).__name__}: {e}'
                print(f'[CDP] 接收循环中断：{self.closed_error}')
        self._recv_task = asyncio.create_task(recv())

    async def call(self, method, params=None, wait=10):
        """发送CDP命令并等待响应。

        wait 是等多少秒（原来写死 10 秒）。虚拟滚动的规格表要在一段 JS 里滚完几百行，
        本来就得花更久 —— 写死 10 秒会把「还在干活」报成「页面卡住了」。
        """
        # 用局部变量配对：self.mid 会被并发调用改掉（原来拿它当 key，两个 call 并发时
        # 谁先改谁就拿到对方的响应）
        mid = self.mid = self.mid + 1
        # 连接已经断了的时候 send 会直接抛异常（ConnectionClosedError）。这个类的约定是
        # 「断了就返回 None，上层自己判断」，可这一句原来裸着 —— 于是连接一断，异常从这里
        # 一路冒到顶（2026-09-27 详情图那轮就是这样把整轮上架打死的），上层连「断了」都看不见。
        try:
            await self.ws.send(json.dumps({
                'id': mid,
                'method': method,
                'params': params or {}
            }))
        except Exception as e:
            self.closed_error = self.closed_error or f'{type(e).__name__}: {e}'
            print(f'[CDP] 连接已断开，{method} 发不出去（{self.closed_error}）')
            return None
        for _ in range(max(1, int(wait / 0.05))):
            if mid in self.pending:
                return self.pending.pop(mid)
            if self.closed_error:
                print(f'[CDP] 连接已断开，{method} 直接失败（{self.closed_error}）')
                return None
            await asyncio.sleep(0.05)
        print(f'[CDP] {method} 等了 {wait:g} 秒没有响应（页面卡住了？）')
        return None

    async def send(self, method, params=None):
        """发送CDP命令不等响应（用于Input.dispatchMouseEvent等）"""
        self.mid += 1
        try:
            await self.ws.send(json.dumps({
                'id': self.mid,
                'method': method,
                'params': params or {}
            }))
        except Exception as e:
            # 同上：断连了就安静地记一笔，别让调用方在一个已经没了的连接上反复抛异常
            self.closed_error = self.closed_error or f'{type(e).__name__}: {e}'
            print(f'[CDP] 连接已断开，{method} 发不出去（{self.closed_error}）')

    async def js(self, expr, wait=10):
        """执行JavaScript并返回结果（出错时打日志，别静默返回 None）。

        wait：等多少秒。默认 10 秒够一般的读写；自己会在 JS 里滚表格、填几百行的那种
        要显式给大一点（那段时间页面在干活，不是卡住了）。
        """
        r = await self.call('Runtime.evaluate', {
            'expression': expr,
            'returnByValue': True,
            'awaitPromise': True
        }, wait=wait)
        if not r:
            return None
        if r.get('error'):
            print(f'[CDP] JS 执行报错: {str(r["error"])[:200]}')
            return None
        out = r.get('result') or {}
        det = out.get('exceptionDetails')
        if det:      # JS 里抛异常了：以前这里和「正常返回 undefined」一样都是 None，查不出来
            print(f'[CDP] JS 抛异常: {str(det.get("exception") or det.get("text") or det)[:200]}')
            return None
        v = out.get('result') or {}
        if v.get('type') != 'undefined':
            return v.get('value')
        return None

    async def screenshot(self, path):
        """截图"""
        import os
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        r = await self.call('Page.captureScreenshot', {
            'format': 'jpeg',
            'quality': 70
        })
        if r and 'result' in r:
            with open(path, 'wb') as f:
                f.write(base64.b64decode(r['result']['data']))

    async def click(self, x, y):
        """CDP鼠标点击"""
        await self.call('Input.dispatchMouseEvent', {
            'type': 'mousePressed',
            'x': x, 'y': y,
            'button': 'left', 'clickCount': 1
        })
        await asyncio.sleep(0.05)
        await self.call('Input.dispatchMouseEvent', {
            'type': 'mouseReleased',
            'x': x, 'y': y,
            'button': 'left', 'clickCount': 1
        })


# ==================== 「这个标签页到底是哪个页面」 ====================
# 光看 URL 挑页面会挑错（见 _pick_pdd_tab 的注释），所以挑之前先连上去问页面自己一句。

def _JS_PAGE_MARKERS():
    """原第 197 行的模块级长常量（476 字符）：已移到 assets_src/，

    加密后随包分发。做成函数是为了**延迟解密** —— import 期还没激活，没钥匙。
    """
    return _A('_JS_PAGE_MARKERS')



_JS_NEXT_STATE = '''
(() => {
    let body = document.body.innerText;
    if (body.includes("AI添加规格") || body.includes("规格与库存")) return "IN_SPEC_PAGE";
    if (body.includes("商品分类") && body.includes("推荐使用")) return "CATEGORY_SELECTION";
    return "STILL_FIRST";
})()
'''

_JS_NEXT_STATE_FIRST = '''
(() => {
    let body = document.body.innerText;
    if (body.includes("AI添加规格") || body.includes("规格与库存")) return "IN_SPEC_PAGE";
    if (body.includes("商品标题") || body.includes("基本信息")) return "IN_FORM";
    return "STILL_LOADING";
})()
'''


# ==================== 「最近使用的分类」 ====================
# 点「发布新商品」会落到 goods/category?msfrom=mms_sidenav。这个页面实测是**两步**：
#   1) 点一下「最近使用的分类」里的某一条（选中，该条 class 变 choose-category checked）
#   2) 再点「确认发布该类商品」这个按钮 —— **这一步才跳转**
# 只做第 1 步不会跳转（2026-09-28 实测：点完页面原地不动，分数一直是 1）。
#
# 选择器**必须**用 .always-use-v2 .always-use-list .choose-category 限定到底：
# 「清空」按钮在隔壁的 .always-use-title 里，点错一下就把用户的最近分类全清了。
# 分类 id 读不出来（在 React fiber 的闭包里，页面 HTML 里一次都搜不到），
# 只能靠点完之后页面自己跳到 goods_add/index?...&id=<cat_id>。

_JS_RECENT_CATS = '''
(() => {
    try {
        const box = document.querySelector('.always-use-v2 .always-use-list');
        if (!box) return JSON.stringify({ok: false, why: 'NO_BOX'});
        const vis = e => { const r = e.getBoundingClientRect();
                           return r.width > 0 && r.height > 0; };
        const items = [...box.querySelectorAll('.choose-category')].filter(vis);
        return JSON.stringify({
            ok: true,
            cats: items.map(e => (e.innerText || '').trim()),
            checked: items.findIndex(e => e.classList.contains('checked'))
        });
    } catch (e) { return JSON.stringify({ok: false, why: 'ERR:' + e.message}); }
})()
'''


_JS_PICK_RECENT_CAT = '''
(() => {
    try {
        const box = document.querySelector('.always-use-v2 .always-use-list');
        if (!box) return JSON.stringify({ok: false, why: 'NO_BOX'});
        const vis = e => { const r = e.getBoundingClientRect();
                           return r.width > 0 && r.height > 0; };
        const items = [...box.querySelectorAll('.choose-category')].filter(vis);
        if (!items.length) return JSON.stringify({ok: false, why: 'EMPTY'});
        const i = __I__;
        if (i < 0 || i >= items.length) {
            return JSON.stringify({ok: false, why: 'OUT_OF_RANGE', n: items.length});
        }
        const el = items[i];
        // 已经选中就别再点：再点一次有可能把选中取消掉（这个风险不值得冒）。
        if (!el.classList.contains('checked')) el.click();
        return JSON.stringify({ok: true, name: (el.innerText || '').trim(), n: items.length});
    } catch (e) { return JSON.stringify({ok: false, why: 'ERR:' + e.message}); }
})()
'''


_JS_CONFIRM_CATEGORY = '''
(() => {
    try {
        const vis = e => { const r = e.getBoundingClientRect();
                           return r.width > 0 && r.height > 0; };
        const b = [...document.querySelectorAll('button,[role=button],[class*=btn],[class*=Btn]')]
            .filter(vis).find(e => (e.innerText || '').trim() === '确认发布该类商品');
        if (!b) return JSON.stringify({ok: false, why: 'NO_CONFIRM'});
        b.click();
        return JSON.stringify({ok: true});
    } catch (e) { return JSON.stringify({ok: false, why: 'ERR:' + e.message}); }
})()
'''


async def _wait_for(cdp, js, ready, timeout=10.0, interval=0.4, label=None):
    """盯着页面问「好了没」，好了马上往下走 —— 别再用「睡 5 秒再看一眼」那种死等。

    流程里到处是「点一下 → asyncio.sleep(5) → 检查页面状态」：页面上其实 0.3 秒就好了，
    那 5 秒纯粹躺在那里等（2026-09-27 用户说「还是有点慢」）。改成每隔 0.4 秒问一句，
    通常几百毫秒就返回。**timeout 一律取原来那个 sleep 的值** —— 只能比原来快，不会比原来等得短。

    ready: 字符串 → js 返回它就算好了；callable → 拿返回值自己判断（比如数量够了没）。
    返回 (是否等到, 最后一次读到的值)。等不到也照样返回、不抛异常：老代码睡够了也是往下走。
    """
    def ok(v):
        if callable(ready):
            try:
                return bool(ready(v))
            except Exception:
                return False
        return v == ready

    t0 = time.monotonic()
    val = None
    while True:
        try:
            val = await cdp.js(js)
        except Exception:
            val = None
        if ok(val):
            return True, val
        if time.monotonic() - t0 >= timeout:
            return False, val
        await asyncio.sleep(interval)


async def _wait_tab_ready(url_part, port, timeout=10.0):
    """等新开的标签页既出现、又渲染出内容（原来是无条件 sleep(5)）。返回 tab 或 None。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            tabs = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{port}/json', timeout=3).read())
        except Exception:
            tabs = []
        for t in tabs:
            if url_part in (t.get('url') or ''):
                m = await _probe_tab_markers(t, timeout=3.0)
                if m and m['len'] > 0:
                    return t
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.4)


# ==================== 店铺身份：这个窗口登的是哪家店 ====================
# 店铺窗口的标签页标题是通用的（「首页」「发布新商品」），光看 /json 的 title 分不出是哪家店。
# 好在 mms 后台顶栏写着店名。实测（2026-09-28，9223 窗口的 /home 页）：
#     span.user-name-text  → "默筱雪服饰"                   ← 干净，首选
#     div.user-name        → "默筱雪服饰 默筱雪四号客服"      ← 店名 + 当前客服账号
#     __NEXT_DATA__.props.pageProps.coreData.extra.mallId → 180015080
# mallId 才是稳定标识（店名会改，mallId 不会），带上它排查时很有用。
#
# 这段**不放进加密资产**（assets_src/）：它只是读一个标签的文字，不是核心逻辑；而多加
# 一份资产意味着**所有已发出的租户资产包都要重发**，为一个显示用的标签不值。
# 读不到就返回空，由调用方回退成「店铺 N（端口 922x）」—— 绝不编一个店名出来。

_JS_SHOP_NAME = r'''
(() => {
    const t = s => (s || '').replace(/[\s\u200b]+/g, ' ').trim();
    const txt = sel => { const e = document.querySelector(sel); return e ? t(e.innerText) : ''; };
    const both = txt('.user-name');
    let name = txt('.user-name-text') || txt('.user-name-name');
    let account = '';
    if (!name) {
        name = both;                    // 专门那个节点读不到，退而用整块文字
    } else if (both && both.indexOf(name) === 0) {
        account = t(both.slice(name.length));
    }
    if (name.length > 30) name = '';    // 长成这样一定不是店名，是选中了别的元素
    let mallId = null;
    try { mallId = window.__NEXT_DATA__.props.pageProps.coreData.extra.mallId || null; } catch (e) {}
    return JSON.stringify({name: name, account: account, mallId: mallId});
})()
'''

_EMPTY_IDENTITY = {'name': '', 'account': '', 'mallId': None}


async def probe_shop_identity(cdp):
    """问一个已经连上的 mms 页面「你是哪家店」。读不到返回空（不猜）。"""
    try:
        raw = await cdp.js(_JS_SHOP_NAME, wait=10)
        d = json.loads(raw) if raw else None
    except Exception:
        d = None
    if not isinstance(d, dict):
        return dict(_EMPTY_IDENTITY)
    return {'name': (d.get('name') or '').strip(),
            'account': (d.get('account') or '').strip(),
            'mallId': d.get('mallId')}


async def _port_shop_identity(port, tabs):
    """在这个端口的 mms 页里问出店名。按「发布页 → 首页 → 其它 mms 页」的顺序试。

    同一个窗口里所有标签页共用一份登录，随便哪个 mms 页问出来的都是同一家店；多试几个
    只是为了避开「这一页刚好没渲染出顶栏」。
    """
    def rank(t):
        u = t.get('url') or ''
        if 'goods_add' in u or 'goods/category' in u:
            return 0
        return 1 if '/home' in u else 2

    pages = [t for t in tabs if 'mms.pinduoduo.com' in (t.get('url') or '')]
    pages.sort(key=rank)
    for t in pages[:3]:
        try:
            ws = await asyncio.wait_for(
                websockets.connect(t['webSocketDebuggerUrl'], max_size=10 * 1024 * 1024),
                timeout=5)
        except Exception:
            continue
        try:
            cdp = CDPClient(ws)
            await cdp.start()
            ident = await probe_shop_identity(cdp)
            if ident['name']:
                return ident
        except Exception:
            pass
        finally:
            try:
                await ws.close()
            except Exception:
                pass
    return dict(_EMPTY_IDENTITY)


async def _port_publish_page(port, tabs):
    """这个端口里有没有「能填的发布页」。返回 (就绪?, 那个标签页)。

    只看 URL 不够：`goods/category?msfrom=mms_sidenav` 是侧边栏「发布新商品」的空壳页，
    上面只有「选择类目」（_score_markers 给 1 分），不能算就绪 —— 2026-09-27 就是它
    让整轮上架全填失败。所以必须连上去问页面自己。
    """
    best, best_tab = await _port_best_cand(tabs)
    return best >= 3, (best_tab if best >= 3 else None)


async def _port_best_cand(tabs):
    """这个端口里「最像发布页」的那个标签页，连同它的打分。返回 (分数, 标签页)。

    和 _port_publish_page 只差一件事：**就绪不了的时候也有分数可看**。
    2 分是「页面没答上来」（多半还在加载，值得再等），1 分是「答了，只有类目选择」，
    0 分是「答了，是别的页」。要分这两种情况的地方就得用它 —— 光看 _port_publish_page
    的 ready=False 会把它们混成一种，然后白等一整个超时。
    """
    cands = [t for t in (tabs or [])
             if 'goods/category' in (t.get('url') or '') or 'goods_add' in (t.get('url') or '')]
    best, best_tab = 0, None
    for t in cands:
        try:
            s = _score_markers(await _probe_tab_markers(t))
        except Exception:
            s = 2               # 问不出来（多半还在加载）—— 当「可能行」，但它拿不到 ready
        if s > best:
            best, best_tab = s, t
        if s >= 3:
            break
    return best, best_tab


# ==================== 停在「分类选择」页时，替人把分类选了 ====================

class NeedManualCategory(Exception):
    """这家店没有「最近使用的分类」，只能人在它自己的窗口里手选一个。

    单独一个类型是为了让上层把它当成「这一步需要人」，而不是「这家店坏了」——
    用户看到的话术、和「窗口没开」「页面上找不到规格表」都不一样。
    """


def _tabs_at(port):
    """问一个端口要它的标签页列表。问不到返回 []（不抛）。"""
    try:
        return json.loads(urllib.request.urlopen(
            f'http://127.0.0.1:{port}/json', timeout=3).read())
    except Exception:
        return []


def _why(r):
    """把 JS 回的 {ok:false, why:...} 变成一句能读的话。"""
    if isinstance(r, dict):
        return r.get('why') or '没给原因'
    return '页面没答上来'


async def _run_js_on(tab, expr, timeout=8.0):
    """连上这个标签页跑一句 JS，跑完断开。返回值；跑不了返回 None（不猜）。

    跟 _probe_tab_markers 一个路子，只是能跑任意表达式。timeout 是必须的：
    卡住的页面 Runtime.evaluate 永远不返回。
    """
    try:
        ws = await asyncio.wait_for(
            websockets.connect(tab['webSocketDebuggerUrl'], max_size=10 * 1024 * 1024),
            timeout=timeout)
    except Exception:
        return None
    try:
        await ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate',
                                  'params': {'expression': expr, 'returnByValue': True}}))
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                return None
            msg = json.loads(await asyncio.wait_for(ws.recv(), left))
            if msg.get('id') == 1:
                v = msg.get('result', {}).get('result', {}).get('value')
                try:
                    return json.loads(v) if isinstance(v, str) else v
                except Exception:
                    return None
    except Exception:
        return None
    finally:
        try:
            await ws.close()
        except Exception:
            pass


async def _port_picker_tab(tabs):
    """在给的标签页里找「只有类目选择」的那一个（打分 1）。找不到返回 None。

    分类页的 URL 一般是 goods/category，但**不靠 URL 认**（URL 会变），
    还是连上去问页面自己 —— 跟 _port_publish_page 一个原则。
    """
    pages = [t for t in (tabs or [])
             if t.get('type') == 'page' and 'mms.pinduoduo.com' in (t.get('url') or '')]
    # 最像的先问，省几轮往返
    pages.sort(key=lambda t: 0 if 'goods/category' in (t.get('url') or '') else 1)
    for t in pages:
        try:
            m = await _probe_tab_markers(t)
        except Exception:
            continue
        if m and m.get('picker') and not m.get('usable'):
            return t
    return None


async def read_recent_cats(tab):
    """只读：这个标签页的「最近使用的分类」。不是分类页 / 读不到 → []。

    给前端下拉用，**不点任何东西**。读不到和「真的没有」都返回 []（下拉上没区别，
    都是不给选项）；真正要动手的那条路用 _run_js_on 自己分辨（见 pick_category_and_wait）。
    """
    r = await _run_js_on(tab, _JS_RECENT_CATS)
    if not isinstance(r, dict) or not r.get('ok'):
        return []
    return [c for c in (r.get('cats') or []) if c]


def _target_tabs(tabs):
    """这一轮快照里，地址像「分类选择页 / 发布页」的标签页。

    只用来判断**值不值得再等**，不用来认页面（认页面一律问 DOM，见 _score_markers）。
    地址栏在导航那一刻就变了，DOM 要晚一两秒才跟上 —— 正好用这个时间差把
    「还在渲染」和「这压根是别的页」分开，见 pick_category_and_wait 里的注释。
    """
    return [t for t in (tabs or [])
            if 'goods/category' in (t.get('url') or '')
            or 'goods_add' in (t.get('url') or '')]


async def pick_category_and_wait(port, tabs, cat_index=0, on_wait=None,
                                 manual_timeout=300.0, ready_timeout=25.0,
                                 load_timeout=30.0, poll=1.0):
    """这家店停在「只有类目选择」的页面时，替它把分类选好，等它变成能填的发布页。

    返回**就绪的标签页**；调用方拿到它再去开 websocket（所以这里全程不碰 cdp/ws，
    也就不存在「点完跳转了要重连」的问题 —— 实测是**原标签页**跳转，不是新开页）。

      · 本来就 score>=3      → 原样返回，一步都不动（老流程完全不变）
      · 分类页 + 有最近分类  → 选第 cat_index 个 → 点「确认发布该类商品」→ 等它变成发布页
      · 分类页 + 没有最近分类 → on_wait(消息) 报进度，轮询等**人自己手选**，最多 manual_timeout 秒
      · 压根不是分类页        → 返回 None，交回调用方原来的逻辑

    等不到就抛错（有分类→Exception，没分类→NeedManualCategory），
    **绝不带着一张填不动的页面往下走** —— 往下只会把主图、规格、价格全填失败。
    """
    ready, tab = await _port_publish_page(port, tabs)
    if ready:
        return tab

    # 等页面把话说清楚。这里必须分清两件事：
    #   「答了，这压根是别的页」   → 立刻走人，一秒都不等（用户开着首页/订单页是常事）
    #   「答了，但还没渲染完」     → 接着等
    # 旧逻辑只把「一个字都没有」（2 分）当成「还没渲染完」，一看到 0 分就判「是别的页」。
    # **可刚导航到 goods/category 的那一两秒正好是 0 分**：拼多多先把商家后台的外壳
    # （功能/订单/服务/课程/规则…）画出来，「选择类目」那一块要晚一步才出来。地址栏在导航
    # 那一刻就已经是 goods/category 了，而 DOM 还没认出来 —— 所以「地址像目标页 + DOM 没结论」
    # 只可能是还在渲染（_target_tabs）。
    # 2026-09-28 用户报的「都到选分类那一栏了还说报错，我再点一遍上架就能选分类了，为什么要
    # 点两遍」就是踩在这儿：旧逻辑当场放弃 → 又开一个分类页 → 还是当场放弃 → 最后报
    # 「不是商品编辑页」。第二遍能成，只是因为那会儿页面正好渲染完了。
    score, picker = await _port_best_cand(tabs)
    deadline = time.monotonic() + load_timeout
    while True:
        if score >= 3:
            return picker
        if score == 1:
            break
        # 2 分 = 有目标页但答不上来（正在加载），0 分且**没有**目标页 = 真不关这事。
        if score != 2 and not _target_tabs(tabs):
            return None
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(poll)
        tabs = _tabs_at(port)
        ready, tab = await _port_publish_page(port, tabs)
        if ready:
            return tab
        score, picker = await _port_best_cand(tabs)

    if score != 1 or picker is None:
        if _target_tabs(tabs):
            # 地址一直是分类页/发布页，可 DOM 死活没渲染出来（连「选择类目」都没有）。
            # 这时候别再说「不是商品编辑页」—— 那会把用户指去翻商品页；真实原因是这一页
            # 只是没渲染出来，刷新一下就好。
            raise Exception(
                f'端口 {port} 的窗口停在「选择类目」页上，但这一页一直没渲染出来'
                f'（等了 {int(load_timeout)} 秒，页面上只有拼多多后台的外壳，'
                f'既没有「选择类目」也没有商品表单）。\n'
                f'  请在那个店铺窗口里把这个页面刷新一下（F5），再执行上架。'
            )
        return None

    r = await _run_js_on(picker, _JS_RECENT_CATS)
    if not isinstance(r, dict) or not r.get('ok'):
        # 读不出来（页面正在变 / 连不上）—— 不猜，交回调用方按老路子报错
        print(f'  [店铺] 端口 {port} 像是分类页，但读不到「最近使用的分类」（{_why(r)}）')
        return None
    cats = [c for c in (r.get('cats') or []) if c]

    if cats:
        idx = cat_index if 0 <= cat_index < len(cats) else 0
        print(f'  [店铺] 端口 {port} 停在分类页，自动选「{cats[idx]}」'
              f'（共 {len(cats)} 个最近分类）')
        pr = await _run_js_on(picker, _JS_PICK_RECENT_CAT.replace('__I__', str(idx)))
        if not isinstance(pr, dict) or not pr.get('ok'):
            raise Exception(
                f'端口 {port} 点不动「最近使用的分类」（{_why(pr)}）。'
                f'请在那个店铺窗口里手选一个分类，再执行上架')
        await asyncio.sleep(0.3)
        cr = await _run_js_on(picker, _JS_CONFIRM_CATEGORY)
        if not isinstance(cr, dict) or not cr.get('ok'):
            raise Exception(
                f'端口 {port} 分类选好了，但点不到「确认发布该类商品」（{_why(cr)}）。'
                f'拼多多这个页面可能改版了 —— 请手动点一下确认，再执行上架')
        budget = ready_timeout
    else:
        msg = ('这家店没有「最近使用的分类」，请在它自己的窗口里手选一个分类，'
               f'选完自动继续（最多等 {int(manual_timeout / 60)} 分钟）')
        print(f'  [店铺] 端口 {port}：{msg}')
        if on_wait:
            try:
                on_wait(msg)
            except Exception:
                pass
        budget = manual_timeout

    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        await asyncio.sleep(poll)
        ready, tab = await _port_publish_page(port, _tabs_at(port))
        if ready:
            return tab

    if cats:
        raise Exception(
            f'端口 {port} 自动选完分类后没进到发布页（等了 {int(ready_timeout)} 秒）。'
            f'请在那个店铺窗口里看一眼，手动把分类选了再执行上架')
    raise NeedManualCategory(
        f'端口 {port} 这家店没有「最近使用的分类」，等了 {int(manual_timeout / 60)} 分钟'
        f'也没等到手选。请在那个店铺窗口里把分类选好，再执行上架')


async def list_shops():
    """枚举「可以上架到哪几家店」，给前端的勾选弹窗用。

    每条：{port, slot, label, name, account, mallId, alive, ready, reason, page, cats, on_cat}
    - on_cat：这家店是不是正停在「分类选择」页。**单独给一个字段**，因为光看 cats 是
      分不清「停在分类页但最近分类是空的」和「压根没停在分类页」的 —— 而这两种情况
      上架时的行为完全不同（前者会停下来等人手选，后者会自动开一个发布页），
      前端的话术必须分开说，不能说错方向。
    - cats：这家店正停在「分类选择」页时的「最近使用的分类」名字列表（前端下拉用）。
      **只有停在那个页面上才有值**；停在发布页时那个区块在 DOM 里根本不存在，给 []。
    - alive=False（**窗口根本没开**）→ ready 也是 False，reason='窗口没开'。
      前端靠它把这一家**直接禁用**：连都连不上，勾了也一定失败（而且后端不会回退到别的店）。
    - alive=True 但 ready=False（窗口开着、没停在能填的发布页）→ reason='没停在发布商品页'。
      这一种**可以勾**：上架时会自动在**它自己的窗口**里开一个发布页。

    alive 和 ready 是两个维度，别合并（2026-09-28 修的 bug）：前端弹勾选框时用 alive 判
    「能不能勾」、用 ready 判「要不要自动开页」。少返回 alive 这个字段，前端的 `!s.alive`
    就恒为 true —— 每一家都被当成窗口没开，**整列勾选框全是灰的、一个都勾不动**。
    """
    import launcher
    slots = launcher.shop_slots()

    async def one(s):
        port = s['port']
        row = {'port': port, 'slot': s.get('slot'),
               'label': (f"店铺 {s['slot']}" if s.get('slot') else '主浏览器'),
               'name': '', 'account': '', 'mallId': None,
               'alive': bool(s['alive']),
               'ready': False, 'reason': '', 'page': None, 'cats': [], 'on_cat': False}
        if not s['alive']:
            row['reason'] = '窗口没开'
            return row
        try:
            tabs = json.loads(urllib.request.urlopen(
                f'http://127.0.0.1:{port}/json', timeout=3).read())
        except Exception:
            # 登记表里说它活着，实际问不上话（刚崩、或者还在启动中）—— 对前端就是「没开」。
            row['alive'] = False
            row['reason'] = '窗口没开'
            return row
        row.update(await _port_shop_identity(port, tabs))
        ready, tab = await _port_publish_page(port, tabs)
        row['ready'] = ready
        if ready:
            row['page'] = (tab.get('url') or '')[:120]
        else:
            row['reason'] = '没停在发布商品页'
            # 停在分类页的话，把「最近使用的分类」读出来给前端下拉（只读，不动页面）
            picker = await _port_picker_tab(tabs)
            if picker is not None:
                row['on_cat'] = True
                row['cats'] = await read_recent_cats(picker)
        return row

    # 各端口并行问，别让弹窗等成串行的几秒
    return list(await asyncio.gather(*[one(s) for s in slots]))


def _markers_from(raw):
    """把页面自报的标记变成字典。解析不了（没答上来、答的是别的）就返回 None。

    None 的含义是「不知道」，**不是**「不能填」——调用方要区别对待。
    """
    try:
        m = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None
    if not isinstance(m, dict):
        return None
    return {
        'usable': bool(m.get('form') or m.get('first') or m.get('category')),
        'picker': bool(m.get('picker')),
        'url': m.get('url') or '',
        'head': m.get('head') or '',
        'len': m.get('len') or 0,
    }


def _score_markers(m):
    """3=能填的页（商品表单 / 第一步 / 以图发品），2=没答上来（可能在加载），1=只有类目选择，0=别的页"""
    if m is None:
        return 2
    if m['usable']:
        return 3
    if m['len'] == 0:
        return 2          # 一个字都没有：多半还在加载，别急着判死刑
    if m['picker']:
        return 1
    return 0


async def _probe_tab_markers(tab, timeout=5.0):
    """连上这个标签页问一句「你是什么页面」，问完就断开。问不出来返回 None（不猜）。

    timeout 是必须的：卡住的页面 Runtime.evaluate 永远不返回（拼多多的页面卡过好几次），
    没有它这里会把整轮上架挂住。
    """
    try:
        ws = await asyncio.wait_for(
            websockets.connect(tab['webSocketDebuggerUrl'], max_size=10 * 1024 * 1024),
            timeout=timeout)
    except Exception:
        return None
    try:
        await ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate',
                                  'params': {'expression': _JS_PAGE_MARKERS(), 'returnByValue': True}}))
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                return None
            msg = json.loads(await asyncio.wait_for(ws.recv(), left))
            if msg.get('id') == 1:
                return _markers_from(msg.get('result', {}).get('result', {}).get('value'))
    except Exception:
        return None
    finally:
        try:
            await ws.close()
        except Exception:
            pass


async def _pick_pdd_tab(tabs):
    """从一堆标签页里挑出「要上架的那个商品页」。挑不到返回 None。

    以前是纯看 URL：优先不带 goods_id 的 goods/category，其次带 goods_id 的，再次 goods_add。
    2026-09-27 用户报「现在不能执行呢」，主浏览器上同时开着三个 PDD 页：
      goods/category?msfrom=mms_sidenav          ← 侧边栏「发布新商品」，页面上只有「选择类目」
      goods/category?...&goods_id=...&type=add   ← 停在「选择类目」那一步的草稿，也只有「选择类目」
      goods_add/index?...&goods_id=...           ← 真正的商品表单（商品标题/规格与库存/保存草稿）
    老逻辑正好挑中了第一个空壳页，然后一路「已在商品编辑页面」把主图、规格、预览图、价格全填失败，
    最后抛的是「页面上找不到规格表（页面上只有 0 行表格）」—— 完全指不到真正的原因。
    带 goods_id 也不代表就是表单（第二个就是反例），所以只能问页面自己。

    先在「自报能填」的里面按原来的 URL 优先级取第一个；都自报不出来（可能在加载）就取第一个候选；
    剩下的（只有类目选择的空壳页）也照样返回 —— 由 _ensure_on_spec_page 当场拒绝并说清原因。
    """
    cands = [t for t in tabs
             if 'goods/category' in t.get('url', '') or 'goods_add' in t.get('url', '')]
    if not cands:
        return None
    # URL 优先级保持不变，只是不再无条件相信它
    cands.sort(key=lambda t: (
        0 if ('goods/category' in t.get('url', '') and 'goods_id' not in t.get('url', '')) else
        1 if 'goods/category' in t.get('url', '') else 2))
    maybe = None
    for t in cands:
        try:
            m = await _probe_tab_markers(t)
        except Exception as e:
            # 问一句只是「尽量别挑错」，问不出来不该把整轮上架带走
            print(f'  [店铺] 问不到这个页面（{e}）：{(t.get("url") or "")[:80]}')
            m = None
        score = _score_markers(m)
        if score >= 3:
            return t
        if score == 2 and maybe is None:
            maybe = t
    return maybe if maybe is not None else cands[0]


async def connect_pdd_tab(title=None, main_images=None, ports=None,
                          cat_index=0, on_wait=None, manual_timeout=300.0):
    """连接到PDD商品编辑页的CDP WebSocket。如果没有打开则自动创建。

    ports: 要搜索的调试端口列表（None 时只搜主浏览器 9222）。多店铺场景下传入全部端口，
           按顺序找第一个「停在发布商品页」的浏览器。返回 (cdp, ws, 选中的端口)。

    挑中的页要是停在「分类选择」那一步，就地替人把分类选好再连（见
    pick_category_and_wait）。cat_index 选第几个最近分类，没有最近分类时调 on_wait
    报进度、停下等人手选（最多 manual_timeout 秒）。
    """
    if ports is None:
        ports = [9222]

    pdd_tab = None
    chosen_port = None

    for port in ports:
        try:
            tabs = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{port}/json', timeout=3).read())
        except Exception:
            continue
        t = await _pick_pdd_tab(tabs)
        if t:
            pdd_tab = t
            chosen_port = port
            break

    if not pdd_tab:
        if len(ports) > 1:
            # 多店铺场景：不自动开，要求用户自己把目标店停在「发布商品」页，避免误填到别的店
            raise Exception(
                '未找到停在「发布商品」页的店铺窗口。'
                '请在你要上架的那家店窗口里打开「发布商品」页，再点一次「执行上架」。'
            )
        # 单店铺：自动在主浏览器 9222 打开PDD商品发布页（用goods/category，不要用goods_add，后者会卡在加载中）
        print('  未找到PDD商品页，正在自动打开...')
        pdd_url = 'https://mms.pinduoduo.com/goods/category'
        # 通过CDP创建新tab（新版 Chrome 必须用 PUT）
        open_new_tab(9222, pdd_url)
        chosen_port = 9222
        print('  已打开PDD商品页，请在Chrome中确认登录状态')
        # 等这个标签页出现并渲染出内容（原来固定睡 5 秒）
        await _wait_tab_ready('goods/category', 9222, timeout=10.0)
        # 重新获取tabs
        tabs = json.loads(urllib.request.urlopen('http://127.0.0.1:9222/json', timeout=10).read())
        pdd_tab = await _pick_pdd_tab(tabs)

    if not pdd_tab:
        raise Exception('PDD商品编辑页打开失败，请检查Chrome是否已启动并登录拼多多')

    # 挑中的页要停在「分类选择」那一步，就地替人把分类选了再连 —— 单店老路径同样受益：
    # 用户点「发布新商品」落到分类页，以前必须自己手点分类（那 15 秒轮询就是在等他）。
    # chosen_port 已经是「要上到哪家店」，在这家店上替它选分类不会串到别人家；
    # 而且这一步**只点已经开着的页面，绝不新开标签页**（多店那条「不自动开页」的规矩还在）。
    if chosen_port is not None:
        picked = await pick_category_and_wait(
            chosen_port, _tabs_at(chosen_port), cat_index=cat_index,
            on_wait=on_wait, manual_timeout=manual_timeout)
        if picked is not None:
            pdd_tab = picked

    print(f'  [店铺] 目标浏览器调试端口: {chosen_port}（{"主浏览器" if chosen_port == 9222 else f"店铺窗口 {chosen_port - 9221}"}）')
    print(f'  [店铺] 选中的页面: {(pdd_tab.get("url") or "")[:110]}')

    ws = await websockets.connect(
        pdd_tab['webSocketDebuggerUrl'],
        max_size=50 * 1024 * 1024
    )
    cdp = CDPClient(ws)
    await cdp.start()

    # 检查是否停在第一步，自动填写标题+上传主图+点击下一步
    await _ensure_on_spec_page(cdp, title=title, main_images=main_images)

    return cdp, ws, chosen_port


async def connect_pdd_tab_at_port(port, title=None, main_images=None, auto_open=True,
                                  cat_index=0, on_wait=None, manual_timeout=300.0, expected_identity=None):
    """连到**指定端口**那家店的发布页（多店铺上架用）。返回 (cdp, ws, port)。

    和 connect_pdd_tab 的关键区别：那个是「给我找一家能上的店」，会在多个端口间挑第一个
    匹配的；这个是「就上这一家」。**绝不能把指定的店换成别的店** —— A 店的商品上到 B 店
    是最严重的事故，所以这里从头到尾只碰 port 这一个窗口，找不到就报错，不换一家。

    这家店没停在能填的发布页时（auto_open）：在**它自己的窗口**里开一个发布页。新标签页
    开在谁家窗口里就是谁家的店（登录态按窗口隔离），所以不会串到别的店。

    停的要是「分类选择」页，就替人把分类选好再往下走（`pick_category_and_wait`）：
    cat_index 选第几个最近分类（前端下拉给的，默认第一个）；没有最近分类时调 on_wait
    报进度、停在原地等人手选（最多 manual_timeout 秒）。
    """
    def _tabs():
        try:
            return json.loads(urllib.request.urlopen(
                f'http://127.0.0.1:{port}/json', timeout=3).read())
        except Exception:
            return None

    tabs = _tabs()
    if tabs is None:
        raise Exception(f'店铺窗口（端口 {port}）没开。请先在工具页点「🔑 打开新店铺窗口」'
                        f'把它开起来并登录，再执行上架')

    from listing_readback import identity_matches
    actual_identity = await _port_shop_identity(port, tabs)
    if not identity_matches(expected_identity, actual_identity):
        raise RuntimeError('Target shop identity is missing or changed; no form was filled')
    ready, tab = await _port_publish_page(port, tabs)
    if not ready and auto_open:
        # 先看它是不是停在「分类选择」页 —— 是的话当场把分类选了，**不用另开一个页**。
        # 原来这里是无条件开新页再死等 15 秒；那 15 秒实质上是在**等人手点分类**，
        # 因为分类页永远只有 1 分（见 _port_publish_page 的注释），等不到就报错。
        tab = await pick_category_and_wait(
            port, tabs, cat_index=cat_index, on_wait=on_wait, manual_timeout=manual_timeout)
        if tab is None:
            print(f'  [店铺] 端口 {port} 没停在发布商品页，在它自己的窗口里打开一个')
            # 用 goods/category，不要 goods_add —— 后者会卡在加载中（见 connect_pdd_tab 的注释）
            # 丢到线程里：open_new_tab 是同步的 urllib 调用，浏览器卡住时最长要等 10 秒。
            # 多店并行时它会把整个事件循环按住，别的店这 10 秒里一步都动不了。
            new_tab = await asyncio.to_thread(open_new_tab, port,
                                              'https://mms.pinduoduo.com/goods/category')
            now = _tabs() or []
            # open_new_tab 刚返回的那个页签，偶尔还没来得及出现在 /json 快照里。少了它，
            # 下面这轮 pick_category_and_wait 就变成「没有目标页可等」→ 当场返回 None →
            # 白开一个页还报错。宁可把返回的页签直接补进快照里。
            if new_tab and not any(t.get('id') == new_tab.get('id') for t in now):
                now = now + [new_tab]
            # 新开的正是分类页：同样替它把分类选好（有最近分类自动选，没有就等人手选）
            tab = await pick_category_and_wait(
                port, now, cat_index=cat_index, on_wait=on_wait,
                manual_timeout=manual_timeout)

    if not tab:
        # 还是没找到能填的。退回 _pick_pdd_tab 会给一个「看着像」的页面，交给
        # _ensure_on_spec_page 当场拒绝并说清原因 —— 比在这里抛个含糊的错有用
        tab = await _pick_pdd_tab(_tabs() or [])
    if not tab:
        raise Exception(f'端口 {port} 这家店找不到「发布商品」页。'
                        f'请在那个店铺窗口里打开「发布商品」，再执行上架')

    print(f'  [店铺] 端口 {port} → {(tab.get("url") or "")[:110]}')
    ws = await websockets.connect(tab['webSocketDebuggerUrl'], max_size=50 * 1024 * 1024)
    cdp = CDPClient(ws)
    await cdp.start()

    # 检查是否停在第一步，自动填写标题+上传主图+点击下一步
    if not identity_matches(expected_identity, await probe_shop_identity(cdp)):
        await ws.close()
        raise RuntimeError('Target shop changed before form entry')
    await _ensure_on_spec_page(cdp, title=title, main_images=main_images)

    return cdp, ws, port


async def _ensure_on_spec_page(cdp, title=None, main_images=None):
    """
    确保在规格编辑页面。
    支持两种PDD发布流程：
    - 旧流程：直接显示标题+下一步
    - 新流程（以图发品）：先上传主图→自动识别类目→点击下一步
    """
    # cdp.js 读不到页面时返回 None —— 直接 `in None` 会 TypeError 把整轮上架打断，
    # 而这里只是「看看现在在哪一页」，读不到就当空页处理并说一声
    page_text = await cdp.js('document.body.innerText.substring(0, 2000)') or ''
    if not page_text:
        print('  ⚠ 读不到页面内容（浏览器卡住或连接断了），无法判断当前在哪一步；接着按后面的现象处理')

    # 检测页面状态
    is_category_page = '以图发品' in page_text or '上传商品轮播图' in page_text
    is_first_step = False
    if page_text:
        if '完善商品信息' in page_text:
            is_first_step = True
        elif '商品标题' in page_text and ('下一步' in page_text or '保存草稿' in page_text):
            is_first_step = True

    if is_category_page:
        # ========== 新流程：以图发品 ==========
        print('  检测到"以图发品"页面，开始上传主图...')

        # 1. 上传主图（先上传到PDD服务器，再通过onUploadBegin+onUploadEnd写入状态）
        if main_images and len(main_images) > 0:
            print(f'  上传主图 ({len(main_images)}张)...')

            # 准备base64数据
            imgs_b64 = {}
            for _i, img_path in enumerate(main_images[:10]):
                if os.path.exists(img_path):
                    with open(img_path, 'rb') as f:
                        _name = os.path.basename(img_path)
                        # 用文件名当 key：不同目录下的同名图（都叫 main_1.jpg）会互相覆盖，
                        # 结果是少传一张而且全程无声。重名的加个序号区分开。
                        if _name in imgs_b64:
                            _stem, _ext = os.path.splitext(_name)
                            _name = f'{_stem}_{_i}{_ext}'
                        imgs_b64[_name] = base64.b64encode(f.read()).decode()

            if imgs_b64:
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
                            const byteChars = atob(b64);
                            const byteArray = new Uint8Array(byteChars.length);
                            for (let i = 0; i < byteChars.length; i++) {{
                                byteArray[i] = byteChars.charCodeAt(i);
                            }}
                            const blob = new Blob([byteArray], {{ type: "image/jpeg" }});
                            const file = new File([blob], name, {{ type: "image/jpeg" }});

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
                            onBegin([{{ id: uploadId, base64: b64.substring(0, 100), name: name }}]);
                            await new Promise(r => setTimeout(r, 200));
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
                print(f'    上传结果: {upload_result}')

                # 等待AI分类识别完成。原来每 2 秒才看一眼（最多 40 秒），识别早完了也得等
                # 到下一个 2 秒的整点；改成每 0.5 秒看一眼，总上限还是 40 秒。
                print('    等待AI分类识别...')
                for i in range(80):
                    await asyncio.sleep(0.5)
                    status = await cdp.js('''(() => {
                        const text = document.body.innerText;
                        if (text.includes("为您推荐分类") || text.includes("已为您识别")) return "CATEGORY_FOUND";
                        if (text.includes("识别中")) return "RECOGNIZING";
                        return "WAITING";
                    })()''')
                    if i % 12 == 0:
                        print(f'    [{(i+1)*0.5:.0f}s] {status}')
                    if status == "CATEGORY_FOUND":
                        break
            else:
                print('    无有效图片')

            # 等待PDD自动识别类目（可能出现类目推荐弹窗）
            check = await cdp.js('''
            (() => {
                let body = document.body.innerText;
                if (body.includes("为您推荐分类") || body.includes("请选择")) return "CATEGORY_POPUP";
                return "NO_POPUP";
            })()
            ''')
            if check == "CATEGORY_POPUP":
                print('    检测到类目推荐弹窗，等待用户确认...')
                await asyncio.sleep(3)

        # 2. 点击"下一步,完善商品信息"
        print('  点击"下一步,完善商品信息"...')
        r = await cdp.js(_A('_ensure_on_spec_page_610'))
        print(f'    下一步: {r}')
        # 原来是睡 5 秒再看状态；改成盯着页面变（进规格页/出商品标题），最多还是 5 秒
        _, check = await _wait_for(cdp, _JS_NEXT_STATE_FIRST,
                                   lambda v: v in ("IN_SPEC_PAGE", "IN_FORM"), timeout=5.0)
        print(f'    页面状态: {check}')

        # 如果需要确认类目，再点一次下一步
        if check == "STILL_LOADING":
            await asyncio.sleep(3)
            r2 = await cdp.js(_A('_ensure_on_spec_page_634'))
            print(f'    再次下一步: {r2}')
            # 第二次点完原来是睡 5 秒收工；一样改成等到进表单（最多 5 秒）
            _, _st = await _wait_for(cdp, _JS_NEXT_STATE_FIRST,
                                     lambda v: v in ("IN_SPEC_PAGE", "IN_FORM"), timeout=5.0)
            if _st not in ("IN_SPEC_PAGE", "IN_FORM"):
                # 两次都没进下一步：后面填规格/传图都会对着一页不是表单的页面做，
                # 与其一路安静地失败到底，不如在这里就说清楚（读不到也走这条）
                print(f'  ⚠ 点了两次「下一步」都没进到商品表单（现在: {_st}），'
                      f'后面的填规格/传图可能全都不对，请到商品页看一眼')

    elif is_first_step:
        # ========== 旧流程：标题+主图+下一步 ==========
        print('  检测到旧版第一步页面，正在填写...')

        # 1. 填写标题 — 优先用PDD的"AI智能推荐标题"的"立即使用"
        print('  处理标题...')
        await asyncio.sleep(2)
        title_result = 'MANUAL_NEEDED' if title else await cdp.js(_A('_ensure_on_spec_page_666'))
        print(f'    标题处理: {title_result}')

        if title_result == "AI_TITLE_USED":
            await asyncio.sleep(1)
        elif title_result == "AI_GENERATE_CLICKED":
            await asyncio.sleep(3)
            await cdp.js('''
            (() => {
                let links = document.querySelectorAll("a, button, span, div");
                for (let el of links) {
                    if ((el.innerText||"").trim() === "立即使用") {
                        el.click();
                        return "USED";
                    }
                }
            })()
            ''')
            await asyncio.sleep(1)
        elif title_result == "MANUAL_NEEDED" and title:
            print(f'    手动填写标题: {title[:30]}...')
            await cdp.js(f'''
            (() => {{
                let inputs = document.querySelectorAll("input, textarea");
                for (let input of inputs) {{
                    if ((input.placeholder||"").includes("标题")) {{
                        input.focus();
                        input.select();
                        document.execCommand("insertText", false, {json.dumps(title)});
                        if (input.value === {json.dumps(title)}) return "FILLED";
                        let setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set;
                        if (setter) {{
                            setter.call(input, {json.dumps(title)});
                            input.dispatchEvent(new Event("input", {{ bubbles: true }}));
                            input.dispatchEvent(new Event("change", {{ bubbles: true }}));
                            return "FILLED_SETTER";
                        }}
                        input.value = {json.dumps(title)};
                        input.dispatchEvent(new Event("input", {{ bubbles: true }}));
                        return "FILLED_DIRECT";
                    }}
                }}
                return "NOT_FOUND";
            }})()
            ''')
            await asyncio.sleep(1)

        # 2. 上传主图
        if main_images and len(main_images) > 0:
            print(f'  上传主图 ({len(main_images)}张)...')
            _js_img_count = '''(() => document.querySelectorAll('img[src*="pddpic"], img[src*="upload"]').length)()'''
            try:
                _img_before = int(await cdp.js(_js_img_count) or 0)
            except Exception:
                _img_before = 0
            doc = await cdp.call('DOM.getDocument')
            root_id = doc['result']['root']['nodeId']
            search = await cdp.call('DOM.querySelectorAll', {
                'nodeId': root_id,
                'selector': 'input[type=file]'
            })
            node_ids = search['result'].get('nodeIds', [])

            uploaded = False
            for nid in node_ids:
                attrs = await cdp.call('DOM.getAttributes', {'nodeId': nid})
                if attrs and 'result' in attrs:
                    attr_list = attrs['result']['attributes']
                    attr_dict = {}
                    for i in range(0, len(attr_list), 2):
                        attr_dict[attr_list[i]] = attr_list[i + 1]
                    accept = attr_dict.get('accept', '')
                    if 'image' in accept or not accept:
                        abs_paths = [os.path.abspath(p) for p in main_images[:5] if os.path.exists(p)]
                        if abs_paths:
                            await cdp.call('DOM.setFileInputFiles', {
                                'nodeId': nid,
                                'files': abs_paths
                            })
                            print(f'    已上传 {len(abs_paths)} 张主图')
                            uploaded = True
                        break

            if not uploaded:
                print('    未找到主图上传input，跳过')
            # 等缩略图冒出来（原来固定睡 3 秒）：页面上的图片数变了就算好了，没变就还等满 3 秒
            await _wait_for(cdp, _js_img_count,
                            lambda v: isinstance(v, int) and v > _img_before, timeout=3.0)

        # 3. 点击下一步（可能需要两次：第一次弹出类目选择，第二次确认类目）
        print('  点击"下一步，完善商品信息"...')
        for attempt in range(3):
            r = await cdp.js(_A('_ensure_on_spec_page_777'))
            if r == "NOT_FOUND":
                break
            print(f'    第{attempt+1}次点击: {r}')
            # 原来是点完睡 5 秒再看状态；改成盯着它变（进了规格页 / 弹出类目选择），
            # 通常一秒内就有结果，等不到才耗满这 5 秒。
            _, check = await _wait_for(
                cdp, _JS_NEXT_STATE,
                lambda v: v in ("IN_SPEC_PAGE", "CATEGORY_SELECTION"), timeout=5.0)
            print(f'    页面状态: {check}')
            if check == "IN_SPEC_PAGE":
                print('  已进入规格编辑页面')
                break
            elif check == "CATEGORY_SELECTION":
                print('  需要确认类目选择，再次点击下一步...')
        else:
            print('  警告：可能未进入规格编辑页面')

    else:
        # 以前这里是无条件的一句「已在商品编辑页面」—— 其实连到的是个空壳页（比如只有「选择类目」的
        # 侧边栏入口）也照样这么说，然后主图、规格、预览图、价格一路填不上，最后抛「页面上找不到规格表」
        # （2026-09-27）。填不动就当场停下来说清楚，别留下一串看不懂的失败。
        try:
            info = _markers_from(await cdp.js(_JS_PAGE_MARKERS()))
        except Exception as e:
            print(f'  警告：读不到页面内容（{e}），按原样继续')
            info = None
        if info is None or info['usable']:
            print('  已在商品编辑页面')
        elif info['picker']:
            # 走到这里说明 pick_category_and_wait 已经试过自动选分类了（它就在连上来之前跑）。
            # ★ 注意：**这里不知道**它到底为什么没成 —— 可能是最近分类是空的，也可能是读列表
            #   读不出来、或者页面刚渲染完没赶上。所以只报「看到的事实」，别替页面猜原因
            #   （2026-09-28 用户就是被一句「最近使用的分类是空的」指错了方向：他那个窗口里
            #   明明有两条最近分类）。
            raise Exception(
                f'这家店停在这个页面上，工具没能自动选到分类：'
                f'{info["url"] or "（拿不到网址）"}\n'
                f'  页面上确实有「选择类目」，所以不是商品页的问题 —— 别再去找商品页。\n'
                f'  请在**那个店铺窗口**里手动点一个分类（点完会自己进到发布页，'
                f'也可以顺手点「确认发布该类商品」），再执行上架。\n'
                f'  如果那个窗口的「最近使用的分类」是空的（新店、或者刚清空过），'
                f'就先手动发布一个商品，让它进到「最近使用的分类」里，以后就能自动选了。\n'
                f'  在这里停下是因为往下只会把主图、规格、价格全填失败。'
            )
        else:
            look = f'页面上没有「商品标题/规格与库存」这些能填的东西（开头是：{info["head"][:60]}）'
            raise Exception(
                f'连到的不是商品编辑页：{info["url"] or "（拿不到网址）"}\n'
                f'  {look}。\n'
                f'  请把要上架的那个商品页开到「规格与库存」那一步（页面上已经有规格值），'
                f'顺手把多余的商品页标签关掉，再点一次「执行上架」。\n'
                f'  在这里停下是因为往下只会把主图、规格、价格全填失败。'
            )


# ==================== 品牌和属性填写 ====================

async def fill_brand_and_attrs(cdp, brand=None, attrs=None):
    """
    填写品牌和商品属性（面料、功能、适用人群、弹力、长度等）。

    Args:
        cdp: CDPClient实例
        brand: 品牌名称，如 "Bingxiu/冰袖"
        attrs: 属性字典，如 {"面料俗称": "冰丝", "功能": "防晒", "适用人群": "通用", "弹力": "高弹", "长度": "350mm（含）-420mm（不含）"}
    """
    print('=== 填写品牌和属性 ===')

    # 滚动到属性区域
    await cdp.js('''
    (() => {
        let headings = document.querySelectorAll("h2, h3, h4, div, span");
        for (let h of headings) {
            if ((h.innerText||"").includes("商品属性")) {
                h.scrollIntoView({behavior:"smooth", block:"start"});
                return "SCROLLED";
            }
        }
        window.scrollBy(0, 500);
        return "SCROLLED_DOWN";
    })()
    ''')
    await asyncio.sleep(1)

    # 填写品牌
    if brand:
        print(f'  填写品牌: {brand}')
        brand_js = _js_string(brand)
        # 搜索品牌
        await cdp.js(f'''
        (() => {{
            const brand = {brand_js};
            let labels = document.querySelectorAll("label, span, div");
            for (let label of labels) {{
                if ((label.innerText||"").trim().includes("品牌")) {{
                    let row = label.closest("[class*=row], [class*=form-item], tr, [class*=FormItem]") || label.parentElement;
                    if (row) {{
                        let input = row.querySelector("input");
                        if (input) {{
                            let setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
                            setter.call(input, brand);
                            input.dispatchEvent(new Event("input", {{bubbles:true}}));
                            input.dispatchEvent(new Event("change", {{bubbles:true}}));
                            return "SEARCHED";
                        }}
                    }}
                }}
            }}
            return "NOT_FOUND";
        }})()
        ''')
        await asyncio.sleep(2)

        # 选择下拉选项
        r = await cdp.js(f'''
        (() => {{
            const brand = {brand_js};
            let options = document.querySelectorAll('[class*="option"], [role="option"], li, [class*="Option"]');
            for (let opt of options) {{
                let text = (opt.innerText||"").trim();
                let rect = opt.getBoundingClientRect();
                if (text.includes(brand) && rect.height > 0) {{
                    opt.click();
                    return "SELECTED:" + text;
                }}
            }}
            return "NO_MATCH";
        }})()
        ''')
        print(f'    品牌选择: {r}')
        await asyncio.sleep(1)

    # 填写其他属性（下拉框）
    if attrs:
        for attr_name, attr_value in attrs.items():
            print(f'  填写属性: {attr_name} → {attr_value}')
            attr_name_js = _js_string(attr_name)
            attr_value_js = _js_string(attr_value)

            # 点击下拉框
            open_r = await cdp.js(f'''
            (() => {{
                const attrName = {attr_name_js};
                let labels = document.querySelectorAll("label, span, div, td");
                for (let label of labels) {{
                    let text = (label.innerText||"").trim();
                    if (text === attrName || text === attrName + "：") {{
                        let row = label.closest("[class*=row], [class*=form-item], tr, [class*=FormItem]") || label.parentElement;
                        if (row) {{
                            let select = row.querySelector("select, [class*=select], [class*=Select], [class*=dropdown]");
                            if (select) {{
                                select.click();
                                return "OPENED";
                            }}
                            let placeholders = row.querySelectorAll("*");
                            for (let ph of placeholders) {{
                                if ((ph.innerText||"").trim() === "请选择") {{
                                    ph.click();
                                    return "PLACEHOLDER_CLICKED";
                                }}
                            }}
                        }}
                        return "ROW_FOUND_NO_SELECT";
                    }}
                }}
                return "LABEL_NOT_FOUND";
            }})()
            ''')
            await asyncio.sleep(1)

            if 'NOT_FOUND' in str(open_r):
                continue

            # 选择匹配的选项
            select_r = await cdp.js(f'''
            (() => {{
                const attrValue = {attr_value_js};
                let options = document.querySelectorAll('[class*="option"], [role="option"], li, [class*="Option"]');
                for (let opt of options) {{
                    let text = (opt.innerText||"").trim();
                    let rect = opt.getBoundingClientRect();
                    if (text.includes(attrValue) && rect.height > 0) {{
                        opt.click();
                        return "SELECTED:" + text;
                    }}
                }}
                return "NO_MATCH";
            }})()
            ''')
            print(f'    选择: {select_r}')
            await asyncio.sleep(0.5)

    # 填写商品货号（自动生成）
    import random
    code = f"BX{random.randint(10000, 99999)}"
    await cdp.js(f'''
    (() => {{
        let inputs = document.querySelectorAll("input");
        for (let input of inputs) {{
            let row = input.closest("[class*=row], [class*=form-item], tr");
            if (row && (row.innerText||"").includes("商品货号")) {{
                let setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
                setter.call(input, "{code}");
                input.dispatchEvent(new Event("input", {{bubbles:true}}));
                input.dispatchEvent(new Event("change", {{bubbles:true}}));
                return "SET:" + "{code}";
            }}
        }}
        return "NOT_FOUND";
    }})()
    ''')
    print(f'  商品货号: {code}')
    print('  品牌和属性填写完成')


async def fill_product_description(cdp, description: str):
    """填写 AI/人工整理的商品描述（若当前 PDD 页面提供该字段）。

    页面版本不同，有的使用 textarea，有的使用 contenteditable 富文本编辑器；
    这里只填字段，不提交发布，也不会覆盖已经上传的详情图片。
    """
    license.guard('填写商品描述')
    text = re.sub(r'\s+', ' ', str(description or '')).strip()[:3000]
    if not text:
        return 'EMPTY'
    payload = json.dumps(text, ensure_ascii=False)
    result = await cdp.js(f'''
    (() => {{
        const text = {payload};
        const fire = (el) => {{
            el.dispatchEvent(new InputEvent('input', {{bubbles:true, inputType:'insertText', data:text}}));
            el.dispatchEvent(new Event('change', {{bubbles:true}}));
            el.dispatchEvent(new Event('blur', {{bubbles:true}}));
        }};
        const candidates = Array.from(document.querySelectorAll('textarea, input, [contenteditable="true"]'));
        const score = (el) => {{
            const ph = ((el.getAttribute('placeholder') || '') + ' ' +
                        (el.getAttribute('name') || '') + ' ' +
                        (el.getAttribute('aria-label') || '')).toLowerCase();
            const parent = (el.closest('[class*=form], [class*=desc], [class*=detail], [class*=editor]')?.innerText || '').toLowerCase();
            let n = 0;
            if (/描述|详情|detail|desc|editor/.test(ph)) n += 10;
            if (/商品详情|图文详情|商品描述/.test(parent)) n += 6;
            if (el.tagName === 'TEXTAREA') n += 3;
            if (el.isContentEditable) n += 2;
            if (el.offsetHeight === 0 || el.offsetWidth === 0) n -= 20;
            return n;
        }};
        const el = candidates.sort((a,b) => score(b) - score(a))[0];
        if (!el || score(el) <= 0) return 'NOT_FOUND';
        if (el.isContentEditable) el.textContent = text;
        else {{
            const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set ||
                Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
            if (setter) setter.call(el, text); else el.value = text;
        }}
        fire(el);
        return el.isContentEditable ? 'CONTENTEDITABLE' : 'TEXTAREA';
    }})()
    ''')
    print(f'  商品描述: {result}')
    return result


# ==================== 规格类型/规格值手动填充 ====================

def _map_spec_type(name):
    """把淘宝抓取的规格类型名映射到拼多多规格类型下拉框的选项文字。"""
    name = name or ''
    if any(k in name for k in ['颜色', '色号', '花色', '花型']):
        return '颜色'
    if any(k in name for k in ['尺码', '尺寸', '大小', '码数']):
        return '尺码'
    if '款式' in name or '风格' in name:
        return '款式'
    if '型号' in name:
        return '型号'
    return name


def _clean_spec_value(val, dim_name):
    """清洗规格值：尺码只保留 S/M/L/XL 等核心词（与生成规格 Excel 的逻辑保持一致）。"""
    import re as _re
    if not val:
        return val
    size_keywords = ['尺码', '尺寸', '大小', '型号', '规格']
    if any(s in dim_name for s in size_keywords):
        m = _re.match(r'^([0-9]*[SMLXLsmlxl]+)', val.strip())
        if m:
            return m.group(1)
    return val


def _spec_vals_clean(values, kind):
    """规格值列表 → 页面上真正的写法（清洗 + 去重，顺序不变）。

    数「页面上应该有几行」要用这个：6 色 × 6 码 = 36 行，用的必须是清洗后的值，
    否则尺码「M(155-160)」和「M」会被算成两种。
    """
    out = []
    for v in values or []:
        v = _clean_spec_value(str(v).strip(), kind)
        if v and v not in out:
            out.append(v)
    return out


def _missing_vals(expect, seen):
    """expect 里哪些值没出现在 seen 里（都按去空白比较，页面上的空格未必和内填的一样）。"""
    got = set(''.join(str(x or '').split()) for x in (seen or []))
    return [v for v in (expect or []) if ''.join(str(v or '').split()) not in got]


async def detect_spec_interface(cdp):
    """检测规格编辑界面类型，返回 'new' / 'manual'。

    - 'new'：新版界面，「颜色分类」「尺码」大类已固定，用「选择或输入主色」「自定义尺码」直接填值
    - 'manual'：页面上有「添加规格类型」按钮，可手动逐个添加规格类型/值
    """
    r = await cdp.js(_A('detect_spec_interface_1039'))
    if r == "NEW":
        return "new"
    return "manual"  # 其余界面统一走手动填充


async def fill_specs_direct(cdp, specs):
    """
    直接在规格编辑页手动填充规格类型和规格值（不通过弹窗导入）。

    Args:
        cdp: CDPClient实例
        specs: {规格类型名: [规格值列表]}，如 {"颜色分类": ["红色","蓝色"], "尺码": ["S","M"]}

    流程（每个维度）:
        1. 点击「添加规格类型」
        2. 在新增行空下拉框里选择规格类型（颜色/尺码…）
        3. 在该行的「请输入规格名称」输入框逐个输入规格值并回车
    拼多多最多 2 个规格类型。
    """
    print('=== 手动填充规格 ===')
    if not specs:
        print('  无规格数据，跳过填充')
        return

    dims = list(specs.items())[:2]  # 拼多多最多 2 个规格类型，多的只能不要
    _dropped = [n for n, _ in list(specs.items())[2:]]
    if _dropped:
        # 静默丢一个维度，页面上的规格组合就会比工具里少（价格那步会报一堆「找不到对应行」，
        # 用户只看到「怎么少了几个」却不知道为什么），所以必须当场说出来。
        print(f'  ⚠ 拼多多只支持 2 个规格类型，已丢弃：{"、".join(_dropped)}'
              f'（页面上会少掉这些维度带来的组合，价格表里多出来的 SKU 会报「找不到对应行」）')

    for dim_name, values in dims:
        type_name = _map_spec_type(dim_name)

        # 1. 点击「添加规格类型」
        r = await cdp.js(_A('fill_specs_direct_1091'))
        if r != "CLICKED":
            print(f'  未找到「添加规格类型」按钮（可能已满 2 个），跳过「{dim_name}」')
            break
        await asyncio.sleep(1.5)

        # 2. 在新增行的空下拉框里选择规格类型
        sel_result = await cdp.js(f'''
        (async () => {{
            const sleep = ms => new Promise(r => setTimeout(r, ms));
            const type = {json.dumps(type_name, ensure_ascii=False)};
            const getOpts = () => Array.from(document.querySelectorAll("li[class*=cIL_item]")).filter(o => {{ let r = o.getBoundingClientRect(); return r.height > 0 && r.width > 0; }});
            // 找到「规格类型N」且 value 为空的下拉框（即新增的那一行）
            let combos = Array.from(document.querySelectorAll("input")).filter(i => (i.placeholder||"").indexOf("规格类型") >= 0 && (i.value||"").trim() === "");
            if (combos.length === 0) return "NO_EMPTY_COMBO";
            let inp = combos[0];
            let sel = inp.closest("[class*=ST_selectValueSingle]") || inp.closest("[class*=ST_head]");
            if (!sel) return "NO_SEL";
            sel.click();
            await sleep(1000);
            let opts = getOpts();
            if (opts.length === 0) {{ sel.click(); await sleep(1000); opts = getOpts(); }}
            if (opts.length === 0) return "NO_OPTIONS";
            let names = opts.map(o => (o.innerText||"").trim());
            // 找不到同名类型时**不要** `|| opts[0]` 随手挑第一个：那是替用户选了另一个规格类型
            // （「尺码」被填成「颜色」这种），后面价格表和规格编码会整体错位。
            // 只在恰好有一个候选与目标名互相包含时才认它，否则如实报出来（带上可选项）。
            let target = opts.find(o => (o.innerText||"").trim() === type);
            if (!target) {{
                let near = opts.filter(o => {{
                    let t = (o.innerText||"").trim();
                    return t && (t.indexOf(type) >= 0 || type.indexOf(t) >= 0);
                }});
                if (near.length === 1) target = near[0];
            }}
            if (!target) return "TYPE_NOT_FOUND|" + names.join("/");
            let t = (target.innerText||"").trim();
            target.click();
            await sleep(1500);
            return "SELECTED_" + t;
        }})()
        ''')
        _sel = str(sel_result or '')
        print(f'  规格类型「{dim_name}」→ {sel_result}')
        if not _sel.startswith('SELECTED_'):
            # 类型没选上（下拉里没有匹配的选项 / 下拉打不开）时，继续往下填值只会把
            # 这一维度的值塞进一个没选类型的行里 —— 页面上不是少一维就是错一维，
            # 价格那步会一片「找不到对应行」。宁可当场停下来说清楚。
            _opts = _sel.split('|', 1)[1].replace('/', '、') if '|' in _sel else ''
            print(f'  ⚠ 「{dim_name}」的规格类型页面上没选上（{_sel}）'
                  + (f'，下拉里只有：{_opts}' if _opts else '')
                  + '，这一维度先跳过（填下去会变成另一个规格类型）')
            continue

        # 3. 在该行（最后一行的值区域）逐个输入规格值并回车
        for v in values:
            v = _clean_spec_value(str(v).strip(), dim_name)
            if not v:
                continue
            ar = await cdp.js(f'''
            (async () => {{
                const sleep = ms => new Promise(r => setTimeout(r, ms));
                // 定位最新一行的值区域，避免误填到上一维度
                let boxes = document.querySelectorAll("[class*=goods-spec-row-box]");
                let box = boxes[boxes.length - 1];
                if (!box) return "NO_BOX";
                let inps = Array.from(box.querySelectorAll("input")).filter(i => (i.placeholder||"").indexOf("请输入规格名称") >= 0);
                let inp = inps.find(i => (i.value||"").trim() === "") || inps[inps.length - 1];
                if (!inp) return "NO_EMPTY_INPUT";
                inp.focus();
                await sleep(150);
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(inp, {json.dumps(v, ensure_ascii=False)});
                inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                await sleep(150);
                inp.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }}));
                await sleep(150);
                // 点一下框再点一下别处失焦，确保该值提交（有的界面回车不提交，必须失焦）
                try {{ inp.blur(); }} catch(e) {{}}
                document.body.click();
                await sleep(250);
                return "OK";
            }})()
            ''')
            if ar != "OK":
                print(f'  添加规格值「{v}」失败: {ar}')

        print(f'  维度「{dim_name}」: {len(values)} 个值')

    # 收尾：全部规格值填完后，逐个点一下「每个已填的输入框」再点空白处失焦，
    # 触发每个值提交（防止没加载出来），并避免光标干扰后续图片/Excel上传
    await cdp.js(_A('fill_specs_direct_1196'))

    await asyncio.sleep(1)
    await cdp.screenshot(_tagged('data/fill_specs_done.jpg'))
    print('=== 手动填充规格完成 ===')


async def fill_specs_new(cdp, specs, spec_rows=None):
    """
    新版界面直接填充规格值（颜色分类 + 尺码 两个大类已固定，无需也不能「添加规格类型」）。

    颜色值 → 逐个填入「选择或输入主色」输入框（回车提交成一个颜色项，自动出现下一个空框）
    尺码值 → 逐个填入「自定义尺码」输入框（同样的 chip 机制）

    Args:
        cdp: CDPClient实例
        specs: {规格类型名: [规格值列表]}，如 {"颜色分类": ["红色","蓝色"], "尺码": ["S","M"]}
        spec_rows: 多店铺并行时**每家店自己那份**「上一次数到的页面行数」。传了就写它，
                   不传才写模块级的 _SPEC_ROWS（见那个变量上面的说明）—— 两家店共用
                   模块级那份会互相串，A 填完 B 又填，B 的行数就成了 A 的对照基准。
    """
    license.guard('填充规格')
    print('=== 新界面填充规格 ===')
    if not specs:
        print('  无规格数据，跳过填充')
        return

    color_keywords = ['颜色', '色号', '花色', '花型', '款式', '风格']
    size_keywords = ['尺码', '尺寸', '大小', '码数']

    color_values = []
    size_values = []
    _ignored = []
    for dim_name, values in specs.items():
        dim_name = dim_name or ''
        vals = [v for v in values if str(v).strip()]
        if any(k in dim_name for k in color_keywords):
            # 原来是直接赋值 —— 于是「款式」会把「颜色分类」整个顶掉：真颜色一个都不填，
            # 「长袖/短袖」反倒被敲进「选择或输入主色」（淘宝商品带「款式」维度很常见）。
            # 现在第一个颜色类维度占主色，第二个（款式等）进尺码栏 —— 新版界面只有
            # 「主色 + 尺码」两栏，这样页面上的组合正好和前端 SKU 名（颜色 尺码）对得上。
            if not color_values:
                color_values = vals
            elif not size_values:
                size_values = vals
                print(f'  「{dim_name}」放进尺码栏（这个界面只有「主色 + 尺码」两栏）: {vals[:4]}')
            else:
                _ignored.append(dim_name)
        elif any(k in dim_name for k in size_keywords):
            if not size_values:
                size_values = vals
            else:
                _ignored.append(dim_name)
        else:
            # 其它维度（型号等）新版界面无对应固定大类
            _ignored.append(dim_name)
    if _ignored:
        print(f'  ⚠ 这些规格维度页面上没有对应栏位、没有填：{"、".join(_ignored)}'
              f'（拼多多这个界面只有「主色 + 尺码」两栏，多出来的维度填不进去，'
              f'价格表里带上它们的 SKU 会报「找不到对应行」）')

    def _tpl(js, placeholder, v='', idx=0):
        """填充 JS 模板里的占位符（避免 f-string 里大括号转义的坑）。"""
        return (js.replace('__PH__', json.dumps(placeholder))
                  .replace('__V__', json.dumps(v, ensure_ascii=False))
                  .replace('__I__', str(idx)))

    # 列出该 placeholder 的全部输入框（含值）
    _JS_INFO = '''
    (() => {
        const PH = __PH__;
        return JSON.stringify(Array.from(document.querySelectorAll("input"))
            .filter(i => (i.placeholder || "").indexOf(PH) >= 0)
            .map(i => ({v: i.value || ""})));
    })()
    '''

    # 滚到可见 + 聚焦第 idx 个输入框，并报告「这个坐标点下去是不是真能点到它」
    # （点不到的情况：紧邻的备注框、上传按钮、或盖在上面的弹窗）
    _JS_FOCUS_IDX = _A('_JS_FOCUS_IDX')

    # 真实键盘输入有没有落进框里
    _JS_TYPED = '''
    (() => {
        const PH = __PH__;
        const V = __V__;
        return Array.from(document.querySelectorAll("input"))
            .some(i => (i.placeholder || "").indexOf(PH) >= 0 && (i.value || "").indexOf(V) >= 0);
    })()
    '''

    # 兜底直接写值（原生 setter + input 事件）
    _JS_SET = _A('_JS_SET')

    # 加载判据：SKU 规格表里出现了这个值的单元格（= 已经进表了，别再加一遍）。
    # 比较时去掉所有空白：格子里的「3210白色-升级款 【春秋款】」这类值带空格，渲染出来
    # 空格数未必和内填的一模一样，严格相等会判成「不在表里」→ 再加一遍 → 页面上同名两条。
    _JS_IN_TABLE = r'''
    (() => {
        const V = __V__;
        const norm = (s) => (s || "").replace(/\s+/g, "");
        const NV = norm(V);
        if (!NV) return 0;
        let n = 0;
        for (const t of document.querySelectorAll("table")) {
            for (const c of t.querySelectorAll("td,th")) {
                if (norm(c.innerText) === NV) n++;
            }
        }
        return n;
    })()
    '''

    # 值到底落没落进页面 —— 填完一个值就判它（不是判「页面上有没有空单元格」）。
    # 两样任一成立就算数：① 同栏位的某个框里就是这个值（chip 提交后框里有时会留着值），
    # ② 规格表里有这个格子（提交成功后颜色/尺码会出现在 SKU 表里）。
    # 只看一样都会误判：只看表会把「已经躺进框、还没进表」判成失败 → 重试第二遍又往
    # 「最后那个空格子」里填一次同名颜色 → 页面上两条同名规格（见 _fill_chip 里的说明）。
    _JS_HAVE_VALUE = _A('_JS_HAVE_VALUE')

    # 找「点别处」的落点：挑一个点下去没有副作用的空白（不是按钮/输入框/图标/表格/弹窗）
    _JS_ELSEWHERE = _A('_JS_ELSEWHERE')

    # 页面上有没有盖住内容的弹窗（会吃掉所有坐标点击）
    _JS_MODAL = _A('_JS_MODAL')

    _JS_MODAL_CLOSE = _A('_JS_MODAL_CLOSE')

    async def _press(key, code, vk):
        """真实键盘按键（走浏览器输入管线，发给当前聚焦的元素）。"""
        for t in ('rawKeyDown', 'keyUp'):
            await cdp.call('Input.dispatchKeyEvent', {
                'type': t, 'key': key, 'code': code,
                'windowsVirtualKeyCode': vk, 'nativeVirtualKeyCode': vk,
            })

    async def _dump(tag):
        """把当前状态追加到磁盘日志（后台窗口的控制台输出读不到，只能看文件）。"""
        try:
            info = await cdp.js(_A('_dump_1467'))
            with open(_tagged('data/_spec_fill_debug.log'), 'a', encoding='utf-8') as f:
                f.write(tag + ' ' + str(info) + '\n')
        except Exception:
            pass

    async def _close_modal():
        """关掉盖住页面的弹窗（如「批量编辑/导入规格数据」），否则坐标点击全被它吃掉。"""
        info = await cdp.js(_JS_MODAL)
        if not info:
            return
        m = json.loads(info)
        r = await cdp.js(_JS_MODAL_CLOSE)
        await asyncio.sleep(0.5)
        if await cdp.js(_JS_MODAL) and r != 'CLICKED_X':
            ox = 30 if m['x'] > 80 else min(m['x'] + m['w'] + 60, m['vw'] - 20)
            oy = min(m['y'] + m['h'] // 2, m['vh'] - 10)
            await cdp.click(ox, oy)
            await asyncio.sleep(0.6)
        print(f'  检测到弹窗遮挡，已尝试关闭（{r}）')

    async def _elsewhere():
        """点一下「别处」（确认无副作用的空白处）—— 失焦即加载。"""
        info = await cdp.js(_JS_ELSEWHERE)
        if not info:
            return None
        e = json.loads(info)
        await cdp.click(e['x'], e['y'])
        await asyncio.sleep(0.35)
        return e

    async def _tap_box(placeholder, idx):
        """点一下第 idx 个框并确保它拿到焦点。返回状态串（OK / BLOCKED:... / NO_INPUT ...）。"""
        info = json.loads(await cdp.js(_tpl(_JS_FOCUS_IDX, placeholder, idx=idx)) or '{}')
        st = info.get('st', 'NO_INPUT')
        if st == 'OK':
            await cdp.click(info['x'], info['y'])
            # 等焦点真的落到这个框上：原来固定睡 0.2 秒再问一句，现在问到了就往下走
            # （上限还是 0.2 秒，只可能更快；st 的含义、问的还是同一段 JS）
            _, _raw = await _wait_for(cdp, _tpl(_JS_FOCUS_IDX, placeholder, idx=idx),
                                      lambda x: '"st":"OK"' in str(x), timeout=0.2, interval=0.05)
            try:
                st = (json.loads(_raw) or {}).get('st', 'NO_INPUT') if _raw else 'NO_INPUT'
            except Exception:
                st = 'NO_INPUT'
        return st

    async def _fill_chip(placeholder, values, kind):
        """按你观察到的节奏填：点框 → 输入 → 点别处（失焦即加载），最后再逐个框点一遍。

        实测要点（PDD 新版规格页）：
        - 坐标点击前必须先确认那个坐标上真的是这个框（elementFromPoint），否则会点到紧邻的
          「备注」框、上传按钮，或被弹窗整个吃掉 —— 这是之前规格值丢失的根源；
        - 只写值不点别处的话，值会躺在框里被 React 重渲染时重置掉；
        - 兜底：万一「点别处」没生效，用真实 Tab 键提交（实测 Tab 一定生效，回车无效）。

        返回：**没落进页面**的值（调用方要报出来、并据此核对页面上到底加了几行）。
        """
        not_landed = []
        for v in values:
            v = _clean_spec_value(str(v).strip(), kind)
            if not v:
                continue
            _t0 = time.time()          # 这一个值花了多久（用户说「填规格有点慢」，先量出来）
            # 已经在规格表里的值不要再加一遍。拼多多的 chip 输入框是「最后那个空格子」，
            # 同一个颜色加两次 → 页面上出现两条同名颜色（尺寸同理），整张 SKU 表跟着乱。
            # 2026-09-15 加：上一次上架只差主图/详情图没传，重跑时前面的规格早就填好了。
            # 注意 _tpl(js, placeholder, v) 的第二个参数是 placeholder：_JS_IN_TABLE 不用
            # placeholder，但要写 v，所以 placeholder 位得占上 —— 写成 _tpl(_JS_IN_TABLE, v)
            # 会把 v 塞进 placeholder，__V__ 变成空串，判的就成了「页面上有没有空单元格」，
            # 真页面上到处都是空单元格 → 所有规格值全被跳过。
            if await cdp.js(_tpl(_JS_IN_TABLE, placeholder, v)):
                print(f'  添加{kind}「{v}」跳过：规格表里已经有了（{time.time() - _t0:.1f}s）')
                continue
            made = False
            _why = ''
            for attempt in (1, 2):
                boxes = json.loads(await cdp.js(_tpl(_JS_INFO, placeholder)) or '[]')
                if not boxes:
                    _why = '没找到输入框'
                    break
                idx = None
                for i in range(len(boxes) - 1, -1, -1):
                    if not boxes[i]['v'].strip():
                        idx = i
                        break
                if idx is None:
                    _why = '没有空输入框（不硬填，免得覆盖）'
                    break

                st = await _tap_box(placeholder, idx)
                if st != 'OK' and not str(st).startswith('BLOCKED'):
                    _why = str(st)
                    break
                if st != 'OK':
                    await _dump(f'[{kind}]「{v}」第{idx}框点不到: {st}')

                # 真实键盘输入；没落进去再补一次，最后用原生 setter 兜底
                await cdp.call('Input.insertText', {'text': v})
                _hit, _ = await _wait_for(cdp, _tpl(_JS_TYPED, placeholder, v),
                                          lambda x: bool(x), timeout=0.4, interval=0.1)
                if not _hit:
                    await cdp.call('Input.insertText', {'text': v})
                    _hit, _ = await _wait_for(cdp, _tpl(_JS_TYPED, placeholder, v),
                                              lambda x: bool(x), timeout=0.4, interval=0.1)
                    if not _hit:
                        await cdp.js(_tpl(_JS_SET, placeholder, v))
                        await asyncio.sleep(0.3)

                # 点一下别处（失焦加载）
                await _elsewhere()
                # 判「值到底落没落进页面」（_JS_HAVE_VALUE：框里有 或 表里有）。
                # 2026-09-27 之前这处和下面兜底 Tab 那处的 _tpl 参数位写错了：_JS_IN_TABLE 的
                # __V__ 拿到的是空串，问的是「页面上有没有空单元格」——真页面上到处都是空单元格，
                # 于是 made 恒为真，重试和 Tab 兜底从来没跑到过，页面上到底有没有这个值也没人查。
                # 用户 2026-09-27「商品过多时规格并没有全部添加」而日志里 6 个颜色全报「已填」，
                # 就是这么来的：拿的是真值之后才可能发现某个颜色根本没进页面。
                made = bool(await cdp.js(_tpl(_JS_HAVE_VALUE, placeholder, v)))
                await _dump(f'[{kind}]#{attempt} 填「{v}」点别处 -> {"落进页面" if made else "没落进页面"}')

                if not made:
                    await _press('Tab', 'Tab', 9)      # 兜底：真实 Tab 提交
                    await asyncio.sleep(0.6)
                    made = bool(await cdp.js(_tpl(_JS_HAVE_VALUE, placeholder, v)))
                    await _dump(f'[{kind}]#{attempt} 兜底Tab「{v}」-> {"落进页面" if made else "没落进页面"}')
                if made:
                    break

            if made:
                print(f'  添加{kind}「{v}」已填（{time.time() - _t0:.1f}s）')
            else:
                not_landed.append(v)
                print(f'  添加{kind}「{v}」没落进页面: {_why or "填了两遍，页面上查不到这个值"}'
                      f'（{time.time() - _t0:.1f}s）')
        return not_landed

    async def _commit_pending(placeholder, values, kind):
        """把「框里已经有字、但没提交」的值提交掉：点一下那个框，再点别处（必要时补个 Tab）。

        **不重新敲值**：重新敲一遍同名颜色，拼多多页面上会出现两条同名规格，整张 SKU 表
        跟着乱（见 _fill_chip 里的说明）。这里只把已经躺在框里的字提交上去。
        返回：提交后仍然不在规格表里的值。
        """
        boxes = json.loads(await cdp.js(_tpl(_JS_INFO, placeholder)) or '[]')
        left = []
        for v in values:
            idx = None
            for i, b in enumerate(boxes):
                if _norm_spec_name(b.get('v')) == _norm_spec_name(v):
                    idx = i
                    break
            if idx is None:
                print(f'  「{v}」在页面上的框里都没有（不是没提交，是压根没填上）')
                left.append(v)
                continue
            await _tap_box(placeholder, idx)
            await _elsewhere()
            if not await cdp.js(_tpl(_JS_IN_TABLE, placeholder, v)):
                await _press('Tab', 'Tab', 9)          # 和填值时一样：Tab 一定生效
                await asyncio.sleep(0.5)
            if await cdp.js(_tpl(_JS_IN_TABLE, placeholder, v)):
                print(f'  把「{v}」提交进了规格表')
            else:
                left.append(v)
                print(f'  「{v}」在第 {idx} 个框里，点框 + Tab 都没能把它提交进规格表')
        return left

    await _close_modal()          # 「批量编辑/导入规格数据」弹窗会吃掉所有坐标点击，先关掉

    not_landed = []
    if color_values:
        print(f'  颜色 {len(color_values)} 个: {color_values[:5]}')
        not_landed += await _fill_chip("选择或输入主色", color_values, "颜色")

    if size_values:
        print(f'  尺码 {len(size_values)} 个: {size_values[:5]}')
        not_landed += await _fill_chip("自定义尺码", size_values, "尺码")

        # 尺码「启用」勾选：新版界面每个尺码值前面有一个启用 checkbox，必须勾选才能加载进 SKU 表
        await asyncio.sleep(0.5)
        en = await cdp.js(_A('fill_specs_new_1658'))
        print(f'  尺码启用勾选结果: {en}')

    await asyncio.sleep(1)

    # ---- 核对：页面的规格表里到底有几行 ----
    # 用户 2026-09-27：「商品过多时，添加商品时商品规格并没有全部添加」。以前填完只报
    # 「已填」就收工，从没数过页面上到底有几行（6 色 × 6 码应该 36 行，页面上只有 18 行
    # 这种事没人发现）。现在加完规格先数一遍：对不上就把还能补救的补一下，还不对就如实报。
    if not_landed:
        print(f'  ⚠ 这些值没落进页面: {"、".join(not_landed)}')
    _c_clean = _spec_vals_clean(color_values, "颜色")
    _s_clean = _spec_vals_clean(size_values, "尺码")
    expect = len(_c_clean) * len(_s_clean) if (_c_clean and _s_clean) else 0
    warn = None
    info = (await count_spec_rows(cdp)) if expect else None
    if expect and info:
        rows, page_colors, page_sizes = info['rows'], info['colors'], info['sizes']
        print(f'  页面上现在有 {rows} 行规格（{len(_c_clean)} 种颜色 × {len(_s_clean)} 种尺码，'
              f'应该有 {expect} 行）')
        if rows < expect:
            stuck = [(_missing_vals(_c_clean, page_colors), "选择或输入主色", "颜色"),
                     (_missing_vals(_s_clean, page_sizes), "自定义尺码", "尺码")]
            for vals, ph, kind in stuck:
                if not vals:
                    continue
                print(f'  页面上没有这些{kind}: {"、".join(vals)} —— 试着把它们提交上去…')
                await _commit_pending(ph, vals, kind)
            if any(v for v, _, _ in stuck):
                info = (await count_spec_rows(cdp)) or info
                rows, page_colors, page_sizes = info['rows'], info['colors'], info['sizes']
                print(f'  补过之后页面上有 {rows} 行规格')
            if rows < expect:
                left_c = _missing_vals(_c_clean, page_colors)
                left_s = _missing_vals(_s_clean, page_sizes)
                warn = (f'页面上只加载了 {rows} 行规格、应该有 {expect} 行'
                        f'（{len(_c_clean)} 种颜色 × {len(_s_clean)} 种尺码）'
                        + (f'，没进去的颜色：{"、".join(left_c)}' if left_c else '')
                        + (f'，没进去的尺码：{"、".join(left_s)}' if left_s else '')
                        + '；请到商品页「规格与库存」看一眼，缺的规格手动补一下（或者重跑一次）')
                print(f'  ⚠ {warn}')
        # 给「填价格」那步留个底：那时候页面上的行数掉了，要能立刻看出来
        # （spec_rows 传了就写自己那份 —— 多店铺并行时不能串到别人家的行数上）
        (spec_rows if spec_rows is not None else _SPEC_ROWS).update(info)
    elif expect:
        print('  （页面上数不出规格行数，这次跳过行数核对）')

    await cdp.screenshot(_tagged('data/fill_specs_new_done.jpg'))
    print('=== 新界面填充规格完成 ===' + ('' if not warn else f'（{warn}）'))
    return warn


# ==================== 价格库存批量填写 ====================

# SKU 名里当「词边界」的字符：空白 + 各种括号/分隔符。
# 「黑色【L】」切成 ['黑色','L']，按词比对才不会把 XL 里的 L 当成 L。
_SKU_SEP = set(' \t　[]【】()（）/\\|,，、;；:：-—_')


def _sku_words(text):
    """把 SKU 名切成词。空白和括号之类的分隔符都算边界。"""
    out, cur = [], []
    for ch in str(text or ''):
        if ch in _SKU_SEP:
            if cur:
                out.append(''.join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        out.append(''.join(cur))
    return out


def _words_contain(hay, needle):
    """needle 的这串词是否作为「连续的一段词」出现在 hay 里。

    关键：按词比对。以前用子串（'L' in '黑色 XL'）会把 XL 的价格/库存安到 L 上，
    某个颜色缺某个尺码时，那一行就会静默套上别的尺码的价格。
    """
    if not needle or len(needle) > len(hay):
        return False
    n = len(needle)
    return any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))


def _match_sku(sku_by_name, combo):
    """按「颜色 尺码」这样的完整规格组合，去匹配前端传来的 SKU。

    颜色值本身可能含空格（如「娃娃领衬衫 26W135」），所以先按整名精确匹配；
    再退化为「每个规格值都作为独立的词出现在 SKU 名里」。
    没匹配到返回 None，由调用方走旧的子串兜底。
    """
    if not sku_by_name:
        return None
    toks = [str(v).strip() for v in combo if str(v).strip()]
    if not toks:
        return None
    key = ' '.join(toks)
    if key in sku_by_name:
        return sku_by_name[key]
    # 退化为「每个规格值都作为独立的词出现在 SKU 名里」（词序不限，与旧行为一致，
    # 只是不再用子串：'L' 不会命中 'XL'）。
    wants = [_sku_words(t) for t in toks]
    for name, sku in sku_by_name.items():
        words = _sku_words(name)
        if all(_words_contain(words, w) for w in wants):
            return sku
    return None


def _num_text(v):
    """写进表格的值：能当数字就当数字（19.9 保留小数，100 不写成 100.0），否则原样。
    没值返回 None —— 没值就不写，别把拼多多原本有的值清掉。"""
    if v in (None, ''):
        return None
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return str(v)


# ================================================================================
# ⚠ 下面是「老路子」（让拼多多导出表 → 只改数字 → 传回去）留下的两个函数。
#    2026-09-15 真机上这条路走不通：挂上文件那一刻拼多多的渲染进程就冻住，
#    连它自己导出的、只换了几个数字的表都不行（详见 _JS_PRICE_TABLE 上面的说明）。
#    上架流程已经改成**直接在页面上填**（fill_prices_in_page / batch_fill_prices），
#    **别再把它接回流程里**。留着是因为这套「只改目标格子、其余逐字节照抄」的解析/填表逻辑
#    本身是对的、离线测过（_test_pdd_template_fill.py），将来离线处理它导出的表还用得上。
# ================================================================================
async def export_pdd_spec_excel(cdp, spec_excel_path, timeout=60):
    """让拼多多自己把当前规格表导出成 Excel，拿到本地文件，返回路径。

    ⚠ 上架流程已经不用它了（见上面的说明）。

    走的是浏览器自己的下载通道（把下载目录指到我们这边），和用户手点「导出当前规格数据」
    是同一条路 —— 不用猜下载地址、不碰 cookie、也不会掉进用户的下载文件夹。

    为什么要用它的表，而不是自己造一张：2026-09-15 真机上连着两次「挂上我们自己造的表格，
    页面进程当场就没了」。崩溃转储（browser_data_2/Crashpad/reports/*.dmp）里写得清清楚楚：
    `Allocation failed - JavaScript heap out of memory`、`young object promotion failed`，
    死在拼多多自己的 add.*.js 的 render 里。它的表有它自己的样子（前 4 行说明、合并单元格、
    样式、列位置），自己造的少了一整块。用它的表、只改数字，才是它一定认的东西。
    """
    license.guard('导出规格表')
    import glob

    download_dir = os.path.join(os.path.dirname(os.path.abspath(spec_excel_path)), 'pdd_downloads')
    os.makedirs(download_dir, exist_ok=True)

    # 下载目录指到自己的目录：不然文件会掉进用户「下载」文件夹，还可能弹保存框
    ok = False
    for method in ('Browser.setDownloadBehavior', 'Page.setDownloadBehavior'):
        r = await cdp.call(method, {'behavior': 'allow', 'downloadPath': download_dir,
                                    'eventsEnabled': True})
        if r and not r.get('error'):
            ok = True
            break
    print(f'  下载目录: {download_dir}' + ('' if ok else '（⚠ 没指成功，可能落到浏览器默认下载目录）'))

    before = set(glob.glob(os.path.join(download_dir, '*')))

    # 找按钮：挑「含这几个字、可见、最内层的 button/a」——外层那些大盒子也含同样的文字，
    # 点它们等于点了空气。顺便把坐标带回来，好用真鼠标点。
    found = await cdp.js(_A('export_pdd_spec_excel_1836'))
    try:
        info = json.loads(found) if isinstance(found, str) else None
    except Exception:
        info = None
    if not info or not info.get('ok'):
        raise Exception('弹窗里没找到「导出当前规格数据」按钮（拼多多弹窗没打开，或者改版了）。'
                        '请回到商品页的「规格与库存」再跑一次')
    bx, by, bw, bh = info['box']
    print(f'  找到导出按钮: <{info["tag"]}> {info["box"]}（同类候选 {info["n"]} 个，'
          f'class={info["cls"]}）')

    # 用**真鼠标**点。2026-09-15 真机上就是这么卡住的：JS 的 el.click() 是合成事件，
    # 页面上一点动静都没有（没有 console、没有新标签页、文件哪儿都没落），拼多多根本没理它。
    # 「确认编辑」当初也是同一个坑 —— 合成事件不算「用户操作」，导出的下载授权也给不了。
    ok_click = False
    cx, cy = bx + bw // 2, by + bh // 2
    if 0 <= cx < info.get('vw', 0) and 0 <= cy < info.get('vh', 0):
        await cdp.click(cx, cy)
        print(f'  已用真鼠标点「导出当前规格数据」({cx},{cy})')
        ok_click = True
    else:
        print(f'  按钮不在视口里（{cx},{cy}，视口 {info.get("vw")}×{info.get("vh")}），退回 JS 点击')
    if not ok_click:
        r = await cdp.js('(() => { const b = window.__ocl_export_btn;'
                         ' if (!b) return "GONE"; b.click(); return "CLICKED"; })()')
        print(f'  JS 点击: {r}')

    # 等文件下来。同时每 10 秒看一眼页面上说了什么 —— 超时的时候要把拼多多自己的话
    # 带进错误里（「按钮还是原样」和「它弹了句失败」是两回事，修法完全不同）。
    async def page_state():
        s = await cdp.js(_A('page_state_1889'))
        try:
            return json.loads(s) if isinstance(s, str) else None
        except Exception:
            return None

    def fmt(st):
        if not st:
            return '读到的东西：无'
        b = '、'.join(st.get('btns') or []) or '无'
        t = ' / '.join(st.get('toasts') or []) or '无'
        return f'导出相关按钮文字=[{b}]，页面提示=[{t}]'

    deadline = time.monotonic() + timeout
    got, last_state = None, None
    # 次数收口 + 真实时间兜底（跟 _wait_import_done 一个写法：测试里 sleep 被换成 0，
    # 只按真实时间收口会白转几百万圈）
    for tick in range(max(1, timeout // 2)):
        if time.monotonic() > deadline:
            break
        await asyncio.sleep(2)
        # 只认这一轮新出现的 .xlsx；.crdownload 是没下完的临时文件，等它下完再拿
        new = [p for p in glob.glob(os.path.join(download_dir, '*.xlsx')) if p not in before]
        if new:
            newest = max(new, key=os.path.getmtime)
            size = os.path.getsize(newest)
            await asyncio.sleep(1)
            if size > 0 and os.path.getsize(newest) == size:
                got = newest
                break
        if (tick + 1) % 5 == 0:               # 每 10 秒报一次，别让人以为卡死了
            st = await page_state()
            if st != last_state:
                last_state = st
                print(f'  等拼多多导出…（已等 {(tick + 1) * 2} 秒）{fmt(st)}')
            else:
                print(f'  等拼多多导出…（已等 {(tick + 1) * 2} 秒）')
    if not got:
        st = last_state or await page_state()
        raise Exception(f'点了「导出当前规格数据」后 {timeout} 秒没等到文件下来'
                        f'（下载目录 {download_dir}）。{fmt(st)}。'
                        '按钮文字还是原样没变 → 多半是这一下没点动，重跑一次；'
                        '拼多多给了提示 → 按它的提示处理（比如「规格数据为空」先去把规格填好）')
    print(f'  拼多多导出的表格: {got}（{os.path.getsize(got)} 字节）')
    return got


# ==================== 在拼多多自己导出的表上填数（只换数字，不重新生成工作簿） ====================
#
# 2026-09-15 真机上：凡是用 openpyxl 存出来的表，挂到拼多多的上传控件那一刻，它的页面就冻住/
# 崩掉（崩溃转储里是它自己 add.*.js 的 V8 堆耗尽）；它自己导出的表填完传回去则一路顺利
# （4240 字节的表填了 10 行）。openpyxl 存回来会丢 xl/sharedStrings.xml、塞进 10KB 的
# xl/theme/theme1.xml、重写 styles，还会写出 `<c r="A1" s="1" t="inlineStr"></c>` 这种
# 「声明了内联字符串却没有 <is>」的畸形格子 —— 每一样都是拼多多没见过的写法。
# 所以这里改做 xml 手术：只替换它那份 sheet1.xml 里目标格子的值，其它部件一个字节不动。
_XLSX_ROW_RE = re.compile(r'(<row\b[^>]*?\br="(\d+)"[^>]*?)(?:/>|>(.*?)</row>)', re.S)
_XLSX_CELL_RE = re.compile(r'<c\b([^>]*?)(?:/>|>(.*?)</c>)', re.S)
_XLSX_REF_RE = re.compile(r'\br="([A-Z]+)(\d+)"')
_XLSX_T_RE = re.compile(r'\bt="([^"]*)"')


def _col_idx(letters):
    """'A' → 1，'AB' → 28"""
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def _col_letters(idx):
    """1 → 'A'，28 → 'AB'"""
    s = ''
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def _xlsx_sheet_part(zf):
    """找工作表那一个部件：按 workbook.xml 的关系走，找不到再退到 sheet1.xml / 第一个 worksheets/*.xml。"""
    names = zf.namelist()
    try:
        wb_xml = zf.read('xl/workbook.xml').decode('utf-8')
        rels = zf.read('xl/_rels/workbook.xml.rels').decode('utf-8')
        rid = re.search(r'<sheet\b[^>]*?\br:id="([^"]+)"', wb_xml)
        if rid:
            tgt = re.search(r'<Relationship\b[^>]*?\bId="%s"[^>]*?\bTarget="([^"]+)"'
                            % re.escape(rid.group(1)), rels)
            if tgt:
                name = tgt.group(1).lstrip('/')
                if not name.startswith('xl/'):
                    name = 'xl/' + name
                if name in names:
                    return name
    except Exception:
        pass
    if 'xl/worksheets/sheet1.xml' in names:
        return 'xl/worksheets/sheet1.xml'
    cands = sorted(n for n in names if n.startswith('xl/worksheets/') and n.endswith('.xml'))
    if not cands:
        raise Exception('这个 Excel 里没有工作表（不是拼多多导出的那张表？）')
    return cands[0]


def _xlsx_shared_strings(zf):
    """共享字符串表。拼多多自己导出的表不用它（文字都内联在格子里），
    但商家用 Excel/WPS 打开再存过的表里，文字全在这里。"""
    try:
        x = zf.read('xl/sharedStrings.xml').decode('utf-8')
    except Exception:
        return []
    out = []
    for si in re.findall(r'<si\b[^>]*?(?:/>|>(.*?)</si>)', x, re.S):
        out.append('' if si is None else
                   ''.join(html.unescape(t) for t in re.findall(r'<t[^>]*>(.*?)</t>', si, re.S)))
    return out


def _xlsx_cell_text(attrs, inner, shared):
    """读一个格子的值：内联字符串 / 共享字符串 / 纯数字三种写法都认。"""
    m = _XLSX_T_RE.search(attrs)
    t = m.group(1) if m else ''
    if inner is None:                     # <c .../> = 空格子
        return ''
    if t == 'inlineStr':
        m = re.search(r'<t[^>]*>(.*?)</t>', inner, re.S)
        return html.unescape(m.group(1)) if m else ''
    m = re.search(r'<v>(.*?)</v>', inner, re.S)
    if not m:
        return ''
    if t == 's':
        try:
            return shared[int(m.group(1))]
        except (ValueError, IndexError):
            return ''
    return html.unescape(m.group(1))


def _xlsx_read_cells(sheet_xml, shared):
    """把工作表读成 {(行号, 列号): 文本}。"""
    cells = {}
    for rm in _XLSX_ROW_RE.finditer(sheet_xml):
        body = rm.group(3)
        if not body:
            continue
        r = int(rm.group(2))
        for cm in _XLSX_CELL_RE.finditer(body):
            attrs = cm.group(1) or ''
            ref = _XLSX_REF_RE.search(attrs)
            if not ref:
                continue
            cells[(r, _col_idx(ref.group(1)))] = _xlsx_cell_text(attrs, cm.group(2), shared)
    return cells


def _xlsx_set_cell(attrs, inner, value):
    """把一个格子的值换成 value —— **保持它原来的写法**：拼多多导出的格子是内联字符串
    （`t="inlineStr"` + `<is><t>`），Excel/WPS 存过的价格/库存是 `<v>数字`。
    只换值，不改类型、不动 s= 样式。

    万一真机上出现「导入成功、价格却没生效」：第一个要试的就是把价格格子强制写成数字
    `<v>`（商家拿 Excel 存出来的表就是那种写法，`~/Downloads/规格批量编辑 (16).xlsx`
    里的库存就是 `<c r="C6" s="1"><v>100</v></c>`）。拼多多自己的导出把库存写成内联字符串
    也照样认，所以先按它的写法来。"""
    esc = saxutils.escape(str(value))
    m = _XLSX_T_RE.search(attrs)
    t = m.group(1) if m else ''
    if t == 'inlineStr' and inner and '<is>' in inner:
        return '<c%s>%s</c>' % (attrs, re.sub(r'<is>.*?</is>', '<is><t>%s</t></is>' % esc,
                                             inner, count=1, flags=re.S))
    if not t and inner and '<v>' in inner:
        return '<c%s>%s</c>' % (attrs, re.sub(r'<v>.*?</v>', '<v>%s</v>' % esc,
                                             inner, count=1, flags=re.S))
    # 其余的（共享字符串格子、空格子、少见的写法）写成内联字符串：合法写法，
    # 而且不用去改 xl/sharedStrings.xml（改它得同步 count/uniqueCount，更容易出错）
    return '<c%s t="inlineStr"><is><t>%s</t></is></c>' % (_XLSX_T_RE.sub('', attrs).rstrip(), esc)


def _xlsx_add_cells(sheet_xml, changes):
    """原文件里根本没有那个 <c> 时补一个（拼多多的表每行都带全了格子，这里只是兜底）：
    按列序插进它自己那一行。样式取同行已有格子的；这一行一个带样式的格子都没有就**不写 s**
    （不写 = 默认样式）—— 写死一个 s="2" 会指向 styles.xml 里不存在的样式，整份文件就废了。"""
    def repl(rm):
        r = int(rm.group(2))
        todo = sorted((c, v) for (rr, c), v in changes.items() if rr == r)
        if not todo:
            return rm.group(0)
        body = rm.group(3) or ''
        m = re.search(r'<c\b[^>]*?\bs="(\d+)"', body)
        style = ' s="%s"' % m.group(1) if m else ''
        items = []
        for cm in _XLSX_CELL_RE.finditer(body):
            ref = _XLSX_REF_RE.search(cm.group(1) or '')
            items.append((_col_idx(ref.group(1)) if ref else 0, cm.group(0)))
        for c, v in todo:
            items.append((c, '<c r="%s%d"%s t="inlineStr"><is><t>%s</t></is></c>'
                          % (_col_letters(c), r, style, saxutils.escape(str(v)))))
        items.sort(key=lambda kv: kv[0])
        return rm.group(1) + '>' + ''.join(x for _, x in items) + '</row>'

    return _XLSX_ROW_RE.sub(repl, sheet_xml)


def fill_pdd_spec_excel(template_path, out_path, sku_by_name=None, price_map=None, stock=100):
    """在拼多多自己导出的规格表上填价格/库存，另存到 out_path。

    ⚠ 上架流程已经不用它了（见 export_pdd_spec_excel 上面的说明）。填出来的文件是对的，
    但拼多多解析它的那一刻会冻住，所以别再传回页面。

    **不重新生成工作簿**：只把它那份 xl/worksheets/sheet1.xml 里目标格子的值换掉。别的部件
    （前 4 行说明、合并单元格、样式、主题、共享字符串表）、它给的「规格编码」、未匹配行的所有
    格子，一个字节都不动。传回去的文件除了我们填的那几个数字，和它导出的原件一模一样。

    为什么非得这么小心：2026-09-15 真机上，凡是用 openpyxl 存出来的表，挂到拼多多的上传控件
    那一刻它的页面就冻住/崩掉（转储里是它自己 add.*.js 的 V8 堆耗尽）；它自己导出的表填完传
    回去则一路顺利。openpyxl 存回来会丢 xl/sharedStrings.xml、塞进 10KB 的 xl/theme/theme1.xml、
    重写 styles，还会写出 `<c r="A1" s="1" t="inlineStr"></c>` 这种「声明了内联字符串却没有
    <is>」的畸形格子 —— 每一样都是拼多多没见过的写法。只换数字就绕开了所有这些。

    返回 (数据行数, 匹配到几行)。
    """
    license.guard('填规格表')
    with zipfile.ZipFile(template_path) as zf:
        # 全读进内存：out_path 有可能就是 template_path（就地改），边读边写会把原件截断
        parts = [(info, zf.read(info.filename)) for info in zf.infolist()]
        sheet_name = _xlsx_sheet_part(zf)
        shared = _xlsx_shared_strings(zf)
    blob = {info.filename: data for info, data in parts}
    if sheet_name not in blob:
        raise Exception('这个 Excel 里找不到工作表（不是拼多多导出的那张表？）')
    sheet_xml = blob[sheet_name].decode('utf-8')
    cells = _xlsx_read_cells(sheet_xml, shared)

    # 表头行：拼多多是第 5 行（前 4 行是说明），但不写死，按「哪一行同时有库存和拼单价」找
    header_row, col_of = None, {}
    for r in sorted({rr for rr, _ in cells}):
        if r > 12:
            break
        texts = [(c, (cells.get((r, c)) or '').strip())
                 for c in sorted(cc for rr, cc in cells if rr == r)]
        vals = [t for _, t in texts]
        if any('库存' in t for t in vals) and any('拼单价' in t for t in vals):
            header_row = r
            for c, t in texts:
                if t and t not in col_of:
                    col_of[t] = c
            break
    if not header_row:
        raise Exception('拼多多导出的表格里找不到表头（库存/拼单价），这份导出不完整，请重试')

    def find_col(*keys):
        for name, c in sorted(col_of.items(), key=lambda kv: kv[1]):
            if any(k in name for k in keys):
                return c
        return None

    c_stock, c_price = find_col('库存'), find_col('拼单价')
    c_single, c_code = find_col('单买价'), find_col('规格编码')
    c_enabled = find_col('启用状态')
    fixed = {c for c in (c_stock, c_price, c_single, c_code, c_enabled) if c}
    # 规格列 = 表头里不是那几列固定列的（拼多多把规格列放在前面，但不按位置假设）
    spec_cols = [c for _, c in sorted(col_of.items(), key=lambda kv: kv[1]) if c not in fixed]
    if not spec_cols:
        raise Exception('拼多多导出的表格里没有规格列（颜色分类/尺码），说明商品页上的规格表还没生成好。'
                        '请确认「规格与库存」里有规格后再重试')

    max_row = max((r for r, _ in cells), default=header_row)
    changes = {}
    rows = matched = 0
    for r in range(header_row + 1, max_row + 1):
        combo = tuple(cells.get((r, c)) for c in spec_cols)
        if not any(str(v).strip() for v in combo if v is not None):
            continue                      # 空行（拼多多有时会留格式行），跳过
        rows += 1
        sku = _match_sku(sku_by_name, combo) if sku_by_name else None
        if sku is None and price_map:
            row_text = ' '.join(str(v) for v in combo if v is not None)
            for key, (p, sp) in price_map.items():
                if key and key in row_text:
                    sku = {'price': p, 'single_price': sp, 'enabled': True}
                    break
        if sku is None:
            continue
        matched += 1
        st = sku.get('stock')
        row_stock = stock if st in (None, '') else st
        # 没值的不写：留着拼多多原有的值，别把商家填过的值清掉
        for c, val in ((c_stock, row_stock), (c_price, sku.get('price')),
                       (c_single, sku.get('single_price'))):
            v = _num_text(val)
            if c and v is not None:
                changes[(r, c)] = v
        if c_enabled:
            changes[(r, c_enabled)] = '0' if sku.get('enabled') is False else '1'
        # 「规格编码」是拼多多自己的行标识，一个字都不改

    def sub(m):
        ref = _XLSX_REF_RE.search(m.group(1) or '')
        if not ref:
            return m.group(0)
        key = (int(ref.group(2)), _col_idx(ref.group(1)))
        if key not in changes:
            return m.group(0)
        return _xlsx_set_cell(m.group(1), m.group(2), changes.pop(key))

    new_xml = _XLSX_CELL_RE.sub(sub, sheet_xml)
    if changes:                           # 原文件里就没这些格子，只能补（少见）
        new_xml = _xlsx_add_cells(new_xml, changes)

    with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zo:
        for info, data in parts:
            if info.filename == sheet_name:
                data = new_xml.encode('utf-8')
            zo.writestr(info, data)
    return rows, matched


# ==================== 弹窗小工具（只用来收拾上一次留下的弹窗） ====================

# 拼多多的弹窗类名（popupContent / MDL_container / …）每个版本都可能换，按类名找不靠谱：
# 线上就是这么变成「关弹窗返回 CLOSED_0、一个都没关掉」，弹窗留在页面上把商品页盖住。
# 改成按弹窗自己的按钮文字找：挑「含这些文字、在视口里、宽高像个弹窗的最小盒子」= 弹窗本体。
def _JS_FIND_DIALOG():
    """原第 2227 行的模块级长常量（2697 字符）：已移到 assets_src/，

    加密后随包分发。做成函数是为了**延迟解密** —— import 期还没激活，没钥匙。
    """
    return _A('_JS_FIND_DIALOG')


def _JS_DIALOG():
    """原第 2277 行的模块级拼接语句（拼了 1 处已加密资产）：已移到 assets_src/，

    加密后随包分发。做成函数是为了**延迟解密** —— import 期还没激活，没钥匙。
    """
    return '(() => {' + _JS_FIND_DIALOG() + r'''
        if (!dlg) return null;
        const r = dlg.getBoundingClientRect();
        return JSON.stringify({x: Math.round(r.x), y: Math.round(r.y),
                               w: Math.round(r.width), h: Math.round(r.height),
                               vw: window.innerWidth, vh: window.innerHeight});
    })()'''


# 点弹窗右上角的 ✕：只在弹窗（和它的直接外层）里找，而且只认右上角那个小方块，
# 不去满页面找 close 类名（那样会点到别的东西）
def _JS_DIALOG_CLOSE():
    """原第 2287 行的模块级长常量（1046 字符）：已移到 assets_src/，

    加密后随包分发。做成函数是为了**延迟解密** —— import 期还没激活，没钥匙。
    """
    return '(() => {' + _JS_FIND_DIALOG() + _A('_JS_DIALOG_CLOSE_2287')



async def close_spec_dialog(cdp, tries=3):
    """把页面上残留的「Excel批量编辑规格」弹窗关掉，返回是否已经干净。

    新流程不会自己打开这个弹窗了，但上一次跑失败会把它留在页面上 —— 两个害处：
    它带遮罩盖住商品页（后面所有鼠标点击都落到遮罩上），而且它自己那张预览表里
    同样有「拼单价」表头，会把我们找规格表的逻辑带偏（填到弹窗里那张表上，
    页面上什么都不会变，日志却显示填好了）。
    """
    for _ in range(max(1, tries)):
        info = await cdp.js(_JS_DIALOG())
        if isinstance(info, str) and info.strip():
            try:
                info = json.loads(info)
            except Exception:
                info = None
        if not info:
            return True
        how = await cdp.js(_JS_DIALOG_CLOSE())
        await asyncio.sleep(0.6)
        if not await cdp.js(_JS_DIALOG()):
            print(f'  关掉了上一次留下的「Excel批量编辑规格」弹窗（{how}）')
            return True
    return False


# ==================== 直接在页面的规格表里填价格/库存（不走 Excel 导入） ====================
#
# 2026-09-15 定案：Excel 导入这条路在真机上过不去。
#   08:13 挂着我们自己造的表 → 拼多多渲染进程 V8 堆耗尽，直接崩（有 Crashpad 转储）。
#   09:21 挂着 openpyxl 存回来的表 → 解析那一刻冻住，没转储（主线程卡死）。
#   09:37 挂着**拼多多自己导出的表、只换了几个数字**（其它字节一模一样）→ 同样冻住。
# 三个文件三种写法，全都冻在同一个位置，所以问题不在文件，在「让它解析一张表格」这件事本身。
# 改成直接在页面规格表里填：不碰文件、不开弹窗，拼多多没有任何东西要解析。
#
# 规格表是虚拟滚动的（DOM 里只有视口附近的行），所以整件事放在一段 JS 里做：
# 自己滚自己的容器、边滚边填、填完再滚一遍回读核对。__SKUS__/__PIPED__/__DOFILL__ 由 Python 侧替换。
def _JS_PRICE_TABLE():
    """原第 2347 行的模块级长常量（14420 字符）：已移到 assets_src/，

    加密后随包分发。做成函数是为了**延迟解密** —— import 期还没激活，没钥匙。
    """
    return _A('_JS_PRICE_TABLE')



def _price_row(sku, stock):
    """一个 SKU → 写进页面的值（没值的为 None = 不碰那一格）。

    第 5 个是「规格编码」。它是用户自己在工具表里排的编码（界面上有「起始编号 + 按顺序填充」），
    必须原样带到页面上 —— 以前根本没带，页面上的编码是工具自己从 1 重新数出来的，
    加一个新尺码就会和已有的号撞上，用户看到的就是「加新尺码时规格编码完全乱了」（2026-09-27）。
    编码走 str 不走 _num_text：那会把 "007" 变成 "7"。
    """
    st = sku.get('stock')
    code = str(sku.get('spec_code') or '').strip()
    return [_num_text(sku.get('price')), _num_text(sku.get('single_price')),
            _num_text(stock if st in (None, '') else st),
            0 if sku.get('enabled') is False else 1,
            code or None]


# 上一次「填规格」数到的页面行数（{"rows": 36, "colors": [...]}）。
# 填价格那步拿来对比：PDD 重渲染、或用户在页面上动过规格，都可能让行变少 —— 行数掉了
# 必须说出来。用户 2026-09-27 遇到的就是「规格没全加上 + 改的价格没上去」，而日志一路报成功。
#
# 这是**单店铺的默认存放处**。多店铺并行时每家店要传自己那份（fill_specs_new /
# batch_fill_prices 的 spec_rows 参数）：两家店共用模块级这一份，A 填完 B 又填，
# B 的行数就变成了 A 的对照基准 —— A 会误报「规格表掉了」，或者 B 掉了行反而报不出来。
_SPEC_ROWS = {}


def _rows_ref(spec_rows):
    """该读哪份「上一次数到的行数」：传了就用传进来的，没传用模块级那份。"""
    return spec_rows if spec_rows is not None else _SPEC_ROWS


# 当前这个异步任务在给哪家店干活（截图、调试日志的文件名要靠它分开）。
#
# 为什么用 ContextVar 而不是模块级全局：多店铺并行是 asyncio.gather 在**同一个线程**里跑
# 多个任务，模块级全局会被交错覆盖 —— A 设成 9223、B 设成 9224，A 再截图就写进了 B 的文件。
# ContextVar 是「每个 task 一份」的（asyncio 建 task 时会复制当前上下文），各写各的，天然隔离。
# 单店铺时它是空串，路径和以前完全一样，不产生任何新文件。
_SHOP_TAG = contextvars.ContextVar('pdd_shop_tag', default='')


def set_shop_tag(tag):
    """标记「本任务在给哪家店干活」。传端口号，或空表示主浏览器/单店铺。

    只影响截图和调试日志的**文件名**，不影响任何业务逻辑。
    """
    _SHOP_TAG.set(str(tag) if tag else '')


def _tagged(name):
    """把固定文件名按店铺岔开：data/batch_fill_done.jpg → data/batch_fill_done_9223.jpg。

    没有店铺标记时原样返回（单店铺行为不变）。这两个名字都是给用户/我们排错看的，
    并行时互相覆盖等于没留证据 —— 出了事只能看到最后那家店的画面。
    """
    tag = _SHOP_TAG.get()
    if not tag:
        return name
    root, ext = os.path.splitext(name)
    return f'{root}_{tag}{ext}'


async def count_spec_rows(cdp):
    """数一遍页面的规格表：返回 {'rows': 行数, 'colors': [...], 'sizes': [...]}；数不出来返回 None。

    只读：拿 _JS_PRICE_TABLE 的回读模式扫一遍（SKUS 传空、且 RO=true = 一个格子都不碰，
    连「顺手给空格补规格编码」都不会发生）。
    「商品多的时候规格没全加上」得有人数行数 —— 以前从没有过这一步。
    """
    raw = await cdp.js(_JS_PRICE_TABLE()
                       .replace('__SKUS__', '{}')
                       .replace('__PIPED__', '{}')
                       .replace('__DOFILL__', 'false')
                       .replace('__RO__', 'true'), wait=90)
    if not isinstance(raw, str):
        return None
    try:
        rep = json.loads(raw)
    except Exception:
        return None
    if not isinstance(rep, dict) or rep.get('err'):
        return None
    return {'rows': rep.get('pageRows') or 0,
            'colors': rep.get('pageColors') or [],
            'sizes': rep.get('pageSizes') or []}


async def fill_prices_in_page(cdp, sku_by_name, price_map=None, stock=100, spec_rows=None):
    """直接在商品页的规格表里填拼单价/单买价/库存，返回一句要提醒用户的话（没问题返回 None）。

    不用 Excel 导入 —— 那条路在真机上会让拼多多的渲染进程冻住（见 _JS_PRICE_TABLE 上面的说明）。
    填的是页面上每行规格自己的输入框，跟人手动一个个填是一回事，只是滚得比人快。

    spec_rows：本次这一家店的行数记录（多店铺并行时必须传，见 _rows_ref 的说明）。
    """
    license.guard('填价格库存')
    skus = {}
    for name, sku in (sku_by_name or {}).items():
        skus[_norm_spec_name(name)] = _price_row(sku, stock)
    if not skus:
        raise Exception('工具里没有可填的 SKU（规格数据是空的），价格库存没法填')

    async def run(piped, do_fill):
        js = (_JS_PRICE_TABLE()
              .replace('__SKUS__', json.dumps(skus, ensure_ascii=False))
              .replace('__PIPED__', json.dumps(piped, ensure_ascii=False))
              .replace('__DOFILL__', 'true' if do_fill else 'false')
              .replace('__RO__', 'false'))
        raw = await cdp.js(js, wait=90)
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return None
        return None

    rep = await run({}, True)
    if not rep:
        raise Exception('读页面的规格表时没有回应（页面卡住了？），价格库存没填。'
                        '刷新商品页、确认「规格与库存」里有规格，再重跑一次')
    if rep.get('err'):
        if rep['err'] == 'NO_TABLE':
            raise Exception(f'页面上找不到规格表（没有「拼单价」那几列，页面上只有 '
                            f'{rep.get("trs", 0)} 行表格）。请确认商品页停在「规格与库存」'
                            '已经有规格值的那一步再重跑')
        raise Exception(f'填价格时页面上报错：{rep["err"]}')

    # 名称对不上的行：用工具里那套匹配规则再解一次（淘宝来的规格值和拼多多的写法常对不齐）
    retry = {}
    for m in (rep.get('miss') or []):
        c, s = m.get('c') or '', m.get('s') or ''
        sku = _match_sku(sku_by_name, (c, s)) if sku_by_name else None
        if sku is None and price_map:
            txt = f'{c} {s}'
            for key, (p, sp) in price_map.items():
                if key and key in txt:
                    sku = {'price': p, 'single_price': sp, 'enabled': True}
                    # 价格表的 key 常常就是工具里的 SKU 名 → 那行的规格编码也一并带上
                    # （用户自己排的编码，能带上就别丢）
                    _src = (sku_by_name or {}).get(key) or {}
                    if _src.get('spec_code'):
                        sku['spec_code'] = _src['spec_code']
                    break
        if sku:
            retry[_norm_spec_name(c) + '|' + _norm_spec_name(s)] = _price_row(sku, stock)
    if retry:
        print(f'  有 {len(retry)} 行规格的写法跟工具里不一样，用匹配规则再填一遍…')
        rep2 = await run(retry, True)
        if rep2 and not rep2.get('err'):
            rep = rep2

    print(f'  页面上匹配到 {rep.get("rows", 0)} 行规格，填了 {rep.get("filled", 0)} 行'
          + (f'，写了 {rep["code"]} 个规格编码'
             + (f'（其中 {rep["recode"]} 个按工具表改了过来）' if rep.get('recode') else '')
             if rep.get('code') else '')
          + (f'，勾掉了 {rep["off"]} 行不启用的规格' if rep.get('off') else '')
          + ('' if rep.get('scrolled') else '（表格没滚动条，只有当前这一屏）')
          + f'（滚动 {rep.get("passes", 1)} 遍核对）')
    if rep.get('virtual_bad'):
        print(f'  （回读第一遍有 {rep["virtual_bad"]} 处读到空值、第二遍没复现 —— '
              f'虚拟滚动重建行时的虚报，没算数）')
    if rep.get('refilled'):
        print(f'  有几处回读两次都没留住，已经重填了 {rep.get("refill_rows") or 0} 行再核对')

    # 这一轮发现的问题都收在这儿（可能是好几件事一起发生：规格少了 + 值没留住），
    # 以前是「后一个盖掉前一个」，只报最后一件事。
    notes = []
    bad = rep.get('bad') or []
    badcode = rep.get('badcode') or []

    # ---- 页面上的行数 vs 工具里的 SKU 数 ----
    # 用户 2026-09-27：「商品过多时，添加商品时商品规格并没有全部添加」。行数在「填规格」
    # 那步数过一次，这里再数一次：那时有多少行、现在多少行，一眼能看出规格是什么时候少的。
    page_rows = rep.get('pageRows') or 0
    tool_rows = len(skus)
    if page_rows and tool_rows:
        page_colors = rep.get('pageColors') or []
        print(f'  页面上共 {page_rows} 行规格（工具里 {tool_rows} 个 SKU'
              + (f'；页面上的颜色：{"、".join(page_colors)}' if page_colors else '') + '）')
    _rows = _rows_ref(spec_rows)
    before = _rows.get('rows')
    if before and page_rows and page_rows < before:
        gone_c = _missing_vals(_rows.get('colors') or [], rep.get('pageColors') or [])
        gone_s = _missing_vals(_rows.get('sizes') or [], rep.get('pageSizes') or [])
        notes.append(f'规格表掉了：填完规格时页面上有 {before} 行，现在只剩 {page_rows} 行'
                     + (f'（掉下去的颜色：{"、".join(gone_c)}）' if gone_c else '')
                     + (f'（掉下去的尺码：{"、".join(gone_s)}）' if gone_s else '')
                     + '，价格只填到了页面上现有的这些行')
    elif page_rows and tool_rows and page_rows < tool_rows:
        notes.append(f'页面上只有 {page_rows} 行规格、工具里有 {tool_rows} 个 SKU，'
                     '少了的那几行没有价格')

    if bad:
        ctx = (rep.get('badctx') or [''])[0]
        if ctx:
            print(f'  [诊断] 第一处没留住的现场: {ctx}')
        notes.append(f'填进去的价格有 {len(bad)} 处没留住（拼多多把值改回去了'
                     + ('，已经重填一遍还是没留住' if rep.get('refilled') else '')
                     + f'），比如 {bad[0]}')
    elif badcode:
        notes.append(f'有 {len(badcode)} 行的规格编码没留住，比如 {badcode[0]}')
    elif rep.get('noinput'):
        notes.append(f'有 {len(rep["noinput"])} 行没找到价格输入框，没填上，'
                     f'比如 {rep["noinput"][0]}')
    left = [(f'{m.get("c")} {m.get("s")}'.strip()) for m in (rep.get('miss') or [])]
    if left:
        notes.append(f'有 {len(left)} 行规格在工具里找不到对应的 SKU，这 {len(left)} 行没填：'
                     f'{left[:4]}')
    if notes:
        warn = '；'.join(notes)
        print(f'  ⚠ {warn}')
        return warn
    return None


async def batch_fill_prices(cdp, price_map, stock=100, spec_excel_path=None, sku_by_name=None,
                            spec_rows=None):
    """把价格/库存填进拼多多商品页的规格表，返回一句要提醒用户的话（没问题返回 None）。

    走的是**直接在页面上填**这条路：不开「Excel批量编辑规格」弹窗、不传文件。
    为什么换掉 Excel 导入，见 fill_prices_in_page 和 _JS_PRICE_TABLE 上面的说明。

    spec_excel_path 只为兼容老调用方保留，现在不用了 —— 商家下载的那份规格表仍然由
    web/app.py 生成，跟这里无关。

    spec_rows：多店铺并行时每家店自己那份行数记录，原样透传给 fill_prices_in_page。
    """
    print('=== 批量填写价格库存 ===')

    # 上一次跑失败可能把这个弹窗留在页面上：它带遮罩，而且它自己那张预览表也有「拼单价」表头，
    # 会把找规格表的逻辑带偏（填到弹窗里那张表上，页面上什么都不会变，日志却显示填好了）
    if not await close_spec_dialog(cdp):
        raise Exception('页面上还盖着「Excel批量编辑规格」弹窗，关不掉。它会挡住整个商品页，'
                        '后面的步骤全都会点到遮罩上；请在页面上手动关掉它再重跑一次')

    warn = await fill_prices_in_page(cdp, sku_by_name, price_map, stock=stock, spec_rows=spec_rows)

    await cdp.screenshot(_tagged('data/batch_fill_done.jpg'))
    print('=== 批量填写价格库存完成 ===' + ('' if not warn else f'（{warn}）'))
    return warn


# ==================== 主流程 ====================

def _norm_spec_name(s):
    """规格值名称归一化：去掉所有空白，便于与 spec_images 的键名匹配。"""
    return ''.join(str(s or '').split())


# ==================== 规格预览图上传用到的 JS ====================
# 规格表列定位 + 虚拟滚动容器定位，两个 JS 共用同一套「找表头行」的逻辑。

def _JS_HEADER_FIND():
    """原第 2838 行的模块级长常量（797 字符）：已移到 assets_src/，

    加密后随包分发。做成函数是为了**延迟解密** —— import 期还没激活，没钥匙。
    """
    return _A('_JS_HEADER_FIND')


# 把这一屏里所有「还缺预览图、且颜色能匹配到本地图」的行都打上标记，
# 之后用 [data-pdd-upload-target="<本轮标识>-<序号>"] 一个个精确定位 nodeId。
# 一次标一批（而不是只标一个）：一屏几行可以一起塞文件、只等一次，省掉每行各等一次的固定开销。
# 颜色必须在判断「有没有 input」之前就读出来：已经传过图的行（没有 input）恰恰是
# 每个颜色组的第一行，跳过它们不读颜色 → 组内后面的行会继承上一个组的颜色而串图。
# 标记值带 __TAG__（Python 每轮换一个新的）：万一本轮的「清标记」那次 JS 没跑成，
# 上一轮残留下来的标记也绝不会和这一轮的串号 —— 否则会把 A 的图塞进 B 的行。
# 序号是用来一对一取 nodeId 的，不能用「DOM 顺序」代替：中间有一行取不到时，
# 后面的行会整体错位，图就静默地传错行了。
def _JS_MARK_UPLOAD_TARGET():
    """原第 2863 行的模块级长常量（2482 字符）：已移到 assets_src/，

    加密后随包分发。做成函数是为了**延迟解密** —— import 期还没激活，没钥匙。
    """
    return r'''
(() => {
    try {
''' + _JS_HEADER_FIND() + _A('_JS_MARK_UPLOAD_TARGET_2866')


# 一批文件塞进去之后看整屏还剩多少「待传」：
#   pend —— 页面上还有几个待传的文件框（一行一个）。这是「图有没有真的上去」唯一可靠的数：
#           标记会被 PDD 重渲染冲掉，待传框的个数不会骗人（图真上去了，那一行的框才会消失）。
_JS_UPLOAD_CHECK = r'''
(() => {
    return JSON.stringify({
        pend: document.querySelectorAll('tr input[type=file][accept*=image]').length
    });
})()
'''

# 清掉上一轮「挑行」的标记。每轮挑行前都要清：
# 标记是「这一轮要往哪些输入框塞文件」的凭据，留着上一轮的旧标记，
# 下面按 DOM 顺序取到的输入框就会和这一轮挑出来的行对不上 —— 把 A 的图塞进 B 的行。
_JS_CLEAR_UPLOAD_MARK = (
    "document.querySelectorAll('[data-pdd-upload-target]')"
    ".forEach(function(e){ e.removeAttribute('data-pdd-upload-target'); }); 'ok'"
)

# 全部清掉（开始跑之前兜底清一次、跑完再清一次）
_JS_CLEAR_ALL_MARKS = (
    "document.querySelectorAll('[data-pdd-upload-target]')"
    ".forEach(function(e){ e.removeAttribute('data-pdd-upload-target'); }); 'ok'"
)

# 找规格表所在的滚动容器（虚拟滚动容器）：从第一条 SKU 行往上找第一个能滚的祖先
def _JS_SPEC_SCROLLER():
    """原第 2948 行的模块级长常量（434 字符）：已移到 assets_src/，

    加密后随包分发。做成函数是为了**延迟解密** —— import 期还没激活，没钥匙。
    """
    return r'''
(() => {
    try {
''' + _JS_HEADER_FIND() + _A('_JS_SPEC_SCROLLER_2951')


# 把规格表滚到指定位置（__TOP__ 由 Python 侧替换成 px 数值）
def _JS_SPEC_SCROLL_TO():
    """原第 2966 行的模块级拼接语句（拼了 1 处已加密资产）：已移到 assets_src/，

    加密后随包分发。做成函数是为了**延迟解密** —— import 期还没激活，没钥匙。
    """
    return r'''
(() => {
    try {
''' + _JS_HEADER_FIND() + r'''
        var anyTr = trs[headerIdx + 1];
        var el = anyTr ? anyTr.parentElement : null;
        while (el) {
            if (el.scrollHeight > el.clientHeight + 20) break;
            el = el.parentElement;
        }
        if (!el) return 'NO_SCROLLER';
        el.scrollTop = __TOP__;
        return 'ok';
    } catch(e) { return 'ERR'; }
})()
'''



async def _read_sku_row_colors(cdp):
    """读 SKU 表每行的颜色，返回按行顺序的颜色列表（一行一个元素）。

    颜色列有 rowspan 合并：同一颜色只有第一行显示颜色文字，其余尺码行的颜色单元格为空，
    所以空单元格继承上一行的颜色。只统计「有尺码」的行（真正的 SKU 行）。
    """
    js = _A('js')
    raw = await cdp.js(js)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


async def upload_spec_preview_images(cdp, spec_images: dict):
    """
    上传规格预览图到PDD商品编辑页的规格行。

    预览图按「颜色」匹配而非按行循环：SKU 表每行是一个「颜色×尺码」组合，
    同一颜色下的不同尺码共用同一张预览图。所以先读表里每行的颜色（颜色列有
    rowspan 合并，空单元格继承上一行颜色），再按颜色名去 spec_images 里查图上传。

    Args:
        cdp: CDPClient实例
        spec_images: {规格值名称(颜色/款式): 本地图片路径}
    """
    license.guard('上传规格预览图')
    if not spec_images:
        print("[SPEC_PREVIEW] 没有规格预览图需要上传")
        return

    print(f"\n=== 上传规格预览图 ({len(spec_images)}个) ===")

    # 筛选有效图片
    valid_images = {k: v for k, v in spec_images.items() if os.path.exists(v) and os.path.getsize(v) > 512}
    if not valid_images:
        print("  没有有效的预览图文件")
        return '规格预览图的文件都不在了或不是有效图片，一张都没传'

    # 颜色名归一化（去空白）→ 本地图片路径
    norm_map = {}
    for k, v in valid_images.items():
        norm_map[_norm_spec_name(k)] = v

    def lookup(color):
        if not color:
            return None
        c = _norm_spec_name(color)
        if c in norm_map:
            return norm_map[c]
        # 兜底：子串匹配（颜色名可能带不同前后缀）
        for k, v in norm_map.items():
            if k and (k in c or c in k):
                return v
        return None

    # PDD 规格表是虚拟滚动的：DOM 里只有视口附近的行，视口外的行连 file input 都没有，
    # 所以必须滚过去、每个视口重新查一遍 DOM。
    # 另外 setFileInputFiles 返回成功 ≠ 图片已经上去了：PDD 是异步处理的（实测 0.8 秒到几秒），
    # 不等它生效就接着塞下一批，后面的会被无声丢掉（16 行掉 5 行，位置随机）。
    # 所以：滚到每个视口 → 把这一屏缺图的行一起打标记（一批最多 BATCH 个）→ 一起塞进去
    # → 一起等（看页面上的待传框少了没有）→ 还缺的下一轮重新标记再传，每行最多 3 次。
    # 一起等、而不是一行一行等，省掉的是每行的固定开销（一屏 6 行 9 秒 → 3 秒）。
    # 传完再回到原地。
    uploaded = 0
    skipped_colors = set()
    # 每一行单独计次：同一行跨视口重复失败只算一行，不会因为同一行被数了 3 次就整体收工。
    # 这两个都记在 Python 里 —— DOM 属性会被 PDD 重渲染冲掉，靠不住。
    attempts = {}
    dead = set()

    def row_key(color, size):
        return _norm_spec_name(color) + '|' + _norm_spec_name(size)

    def mark_js_src(tag):
        """挑行用的 JS：把「本地有哪些颜色」「哪几行已经放弃」「本轮标识」一起传进去"""
        return (_JS_MARK_UPLOAD_TARGET()
                .replace('__HAVE__', json.dumps(list(norm_map.keys()), ensure_ascii=False))
                .replace('__SKIP__', json.dumps(sorted(dead), ensure_ascii=False))
                .replace('__TAG__', json.dumps(str(tag), ensure_ascii=False)))

    # 开始前先清一次（上一次跑失败/页面被手动点过，可能留下标记）
    await cdp.js(_JS_CLEAR_ALL_MARKS)

    # 上传过程同时写一份日志（控制台关了也能事后查）
    _logf = None
    try:
        _logf = open(_tagged('data/_spec_preview_debug.log'), 'a', encoding='utf-8')
        _logf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"预览图上传开始（{len(valid_images)} 张图）=====\n")
    except Exception:
        _logf = None

    def _log(msg):
        print(msg)
        if _logf:
            try:
                _logf.write(msg + '\n')
                _logf.flush()
            except Exception:
                pass

    # 一批最多并行塞几个：并发上传要看页面的承受力。早先「把整屏一起塞进去」实测是
    # 16 行掉 5 行、位置随机（页面内部有并发上限，超出的那些会被静默丢掉）。
    # 4 个一组是折中：一屏 6 行只要 2 批，而每批仍然走下面「数待传框」的核对，掉了下一轮捡回来。
    BATCH = 4

    async def _mark_all(tag):
        """清掉旧标记，再把这一屏里「还缺图且颜色能匹配到本地图」的行全打上标记"""
        await cdp.js(_JS_CLEAR_UPLOAD_MARK)
        raw = await cdp.js(mark_js_src(tag))
        try:
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def _send_batch(tag, items):
        """把这一批文件一起塞进去（每行一个输入框，各塞各的），返回真正送出去的那些行。

        items 里每项带着它在这轮标记结果里的序号 mi —— **必须按 mi 去查那个标记**，
        不能用「这一批里的第几个」：items 是标记结果里筛掉「没本地图/试满 3 次」之后的子集，
        一旦前面筛掉一个，后面每个的序号就整体错位，图会静默地塞到别人那一行去。
        读不到页面结构时返回 None。查不到的行不动它，下一轮重新标、重新传。
        """
        doc = await cdp.call('DOM.getDocument')
        if not doc:
            # 页面卡住/连接断了时 call 返回 None（不是抛异常），doc.get 会直接 AttributeError
            return None
        root_node = doc.get('result', {}).get('root', {}).get('nodeId')
        sent = []
        for color, size, path, key, label, mi in items:
            q = await cdp.call('DOM.querySelector', {
                'nodeId': root_node,
                'selector': f'[data-pdd-upload-target="{tag}-{mi}"] input[type=file][accept*=image]'
            })
            nid = ((q or {}).get('result') or {}).get('nodeId') or 0
            if not nid:
                _log(f'  [{tag}] 「{label}」这一行在页面上没找到（标记被重渲染冲掉了），下一轮重传')
                continue
            r = await cdp.call('DOM.setFileInputFiles', {
                'nodeId': nid,
                'files': [os.path.abspath(path)]
            })
            if r is None:
                err = '浏览器没响应（页面卡住或连接断了）'
            elif r.get('error'):
                err = str(r['error'].get('message') or r['error'])[:70]
            else:
                sent.append((color, size, path, key, label, mi))
                continue
            # 「标记没了」「nodeId 失效」都是 PDD 重渲染造成的时序问题，不是图片有问题：
            # 下一轮重新打标记再传（每轮都重新打，旧标记本来就可能已经被冲掉了）
            _log(f'  [{tag}] 「{label}」没送出去（{err}），下一轮重传')
        return sent

    async def _check():
        """读页面上还剩多少个待传框。**读不到一律返回 None**。

        以前读不到是返回 {} —— 而调用方是按 `alive` 为假、`pend` 缺省 0 来判的：
        `0 < before` 只要页面上还有待传的 input 就成立 → 没传上去的图被记成「已传」。
        cdp.js 返回 None（Runtime.evaluate 超时、JS 抛异常、连接断开）在这里是常态，
        所以这不是理论问题：正好废掉了下面那段「只有待传数确实少了才算数」的防线。
        顺带要求真的读到 pend 键（JS 返回一个空对象同样不算数）。
        """
        raw = await cdp.js(_JS_UPLOAD_CHECK)
        if not raw:
            return None
        try:
            v = json.loads(raw)
        except Exception:
            return None
        return v if isinstance(v, dict) and 'pend' in v else None

    async def _wait_batch(tag, before, n):
        """等这一批图落到页面上，返回 'all'（待传框正好少了这一批的个数）或 'partial'。

        「待传框的个数」是唯一可靠的判据：标记会被 PDD 重渲染冲掉，框的个数不会骗人
        （图真的上去了，那一行的框才会消失）。读不到页面就别比大小 —— 比了等于放行，
        这正是之前「没传上的被记成已传」的来源。
        """
        if before is None:
            # 基线没读到：给页面一点时间把图吃下去，这一批到底成没成交给下一轮「重新标记」逐行判定
            _log(f'  [{tag}] 没读到待传数，这一批只按「下一轮重新标记」逐行判定')
            await asyncio.sleep(2.0)
            return 'partial'
        target = max(0, before - n)
        timeout = max(8.0, 3.0 + 2.0 * n)
        waited = 0.0
        while waited < timeout:
            # 0.15 秒一问（原来 0.4）：一批图通常 0.5~1 秒就渲染好了，
            # 0.4 的粒度会把整批拖成「每批多等 0.4 秒」白等
            await asyncio.sleep(0.15)
            waited += 0.15
            ck = await _check()
            if ck is None:
                continue          # 读不到页面 → 不知道成没成，绝不能算成功，等满上限按失败重传
            if ck['pend'] <= target:
                return 'all'
        _log(f'  [{tag}] 这一批 {n} 个等满 {timeout:.0f} 秒还没生效，重传')
        return 'partial'

    async def _drain_viewport(tag):
        """把当前视口里还缺图的行分批传完；返回 'ok' / 'need_scroll' / 'giveup'

        每一轮：先把上一轮送出去的结算掉（还留在「缺图」名单里的就是没上去），
        再把这一屏缺图的行取一批（最多 BATCH 个）一起塞进去、一起等。
        「一起塞」省掉的就是原来每行各等一次的固定开销（一屏 6 行 ~9 秒 → ~3 秒）。
        """
        nonlocal uploaded
        prev_sent = []           # 上一轮送出去的行，下一轮开头结算
        for _round in range(12):     # 每行上限 3 次、一批 BATCH 个，几轮就收敛；这里是兜底
            rtag = f'{tag}#{_round + 1}'    # 每轮换一个标识：塞文件时按它去查标记，必须和标记时一致
            m = await _mark_all(rtag)
            if m is None:
                _log(f'  [{tag}] 读不到规格表（页面上找不到「预览图」表头），本视口跳过')
                return 'giveup'
            if m.get('err'):
                _log(f'  [{tag}] 读规格表出错: {m["err"]}')
                return 'giveup'

            rows = m.get('rows') or []
            still = {(r.get('c') or '', r.get('s') or '') for r in rows}
            if m.get('unknown'):
                # 有行的颜色读不出来（虚拟滚动把颜色单元格截掉了）→ 这一轮认不全「谁还缺图」，
                # 那就不能拿「名单里没有」当「传上了」：宁可在下一轮重传一次，
                # 也不能把没上去的记成上去了（这个视口就是这么掉图的）
                _log(f'  [{tag}] 有 {m["unknown"]} 行的颜色读不出来，这一轮不结算，下一轮重看')
            else:
                for color, size, path, key, label, _mi in prev_sent:
                    if (color, size) in still:
                        continue         # 还在缺图名单里 → 图没上去，交给本轮重传
                    # 不在名单里 = 这一行已经没有待传框了（缩略图渲染出来，input 消失了）
                    uploaded += 1
                    attempts.pop(key, None)
                    _log(f'  [{tag}] 「{label}」→ {os.path.basename(path)}（已生效）')
            prev_sent = []

            if not m.get('ok'):
                if m.get('needScroll'):
                    return 'need_scroll'
                miss = m.get('miss') or []
                if miss:
                    colors = sorted({s.split(' ')[0] for s in miss})
                    skipped_colors.update(colors)
                    _log(f'  [{tag}] 这些颜色没有对应预览图，跳过 {len(miss)} 行: {colors[:4]}')
                return 'ok'

            items = []
            for mi, it in enumerate(rows):   # mi = 这一行在这轮标记结果里的序号，查 nodeId 要用它
                color = it.get('c') or ''
                size = it.get('s') or ''
                label = (color + ' ' + size).strip()
                key = row_key(color, size)
                path = lookup(color)
                if not path:
                    dead.add(key)
                    _log(f'  [{tag}] 「{label}」匹配不到本地图，跳过')
                    continue
                if attempts.get(key, 0) >= 3:
                    # 这一行试满 3 次都没成：记一笔、别再纠缠它，后面没传的行照传。
                    # （以前这里是「3 行传不上就整张表收工」，结果后面还全都没传的行跟着一起废掉 ——
                    #   用户遇到的「最后两行没图」就是这么来的。只有一行都没成功才说明是页面/网络出了问题。）
                    if key not in dead:
                        dead.add(key)
                        _log(f'  [{tag}] 「{label}」试了 3 次都没传上去，先放过这一行')
                    continue
                items.append((color, size, path, key, label, mi))

            if not items:
                # 这一屏剩下的行要么没本地图、要么试满了 → 没有可传的了
                if len(dead) >= 3 and uploaded == 0:
                    _log('  一行都没传上去、已经有 3 行卡住，停止上传预览图（请检查网络或页面状态）')
                    return 'giveup'
                return 'ok'

            batch = items[:BATCH]        # 一次最多 BATCH 个，剩下的留给下一轮（会重新标记再挑）
            _b = await _check()
            before = _b['pend'] if _b else None    # 基线读不到就别比（比了等于放行）
            for _c, _s, _p, key, _label, _mi in batch:
                attempts[key] = attempts.get(key, 0) + 1
            sent = await _send_batch(rtag, batch)
            if sent is None:
                _log(f'  [{tag}] 读页面结构没响应（浏览器卡住或连接断了），本视口跳过')
                return 'giveup'
            if not sent:
                continue                 # 一个都没送出去：下一轮重新标记再试（每行上限 3 次，不会空转）
            if await _wait_batch(tag, before, len(sent)) == 'all':
                # 页面上的待传框正好少了这一批的个数 → 这一批都上去了，不用再逐行判断
                uploaded += len(sent)
                for color, size, path, key, label, _mi in sent:
                    attempts.pop(key, None)
                    _log(f'  [{tag}] 「{label}」→ {os.path.basename(path)}（已生效）')
                prev_sent = []
            else:
                # 没等全 → 到底哪几个上去了、哪几个没上去，交给下一轮重新标记时逐行看
                prev_sent = sent

        # 轮数用完（正常几轮就收工，这里是兜底）：最后再确认一次最后一轮的结果
        m = await _mark_all(f'{tag}#end')
        if m is not None and not m.get('unknown'):
            still = {(r.get('c') or '', r.get('s') or '') for r in (m.get('rows') or [])}
            for color, size, path, key, label, _mi in prev_sent:
                if (color, size) not in still:
                    uploaded += 1
                    attempts.pop(key, None)
                    _log(f'  [{tag}] 「{label}」→ {os.path.basename(path)}（已生效）')
        return 'ok'

    # 规格表的滚动容器 + 滚动范围（虚拟滚动要滚过去，行才会被渲染出来）
    scroll_info = None
    raw_scroll = await cdp.js(_JS_SPEC_SCROLLER())
    try:
        scroll_info = json.loads(raw_scroll) if raw_scroll else None
    except Exception:
        scroll_info = None

    step = 0
    steps = [None]
    if scroll_info and scroll_info.get('client') and scroll_info.get('height', 0) > scroll_info['client']:
        span = scroll_info['height'] - scroll_info['client']
        step = max(int(scroll_info['client'] * 0.7), 80)
        steps = list(range(0, span, step)) + [span]
    if scroll_info:
        _log(f"  规格表滚动遍历: {len(steps)} 个视口"
             f"（表高 {scroll_info.get('height')}，可视 {scroll_info.get('client')}）")
    else:
        _log("  找不到规格表滚动容器，按单个视口处理")

    async def _sweep(round_label):
        """把整张表从头滚到尾传一遍；返回 False 表示中途放弃了"""
        for n, st in enumerate(steps, 1):
            tag = f'{round_label}视口{n}/{len(steps)}'
            if st is not None:
                await cdp.js(_JS_SPEC_SCROLL_TO().replace('__TOP__', str(int(st))))
                await asyncio.sleep(0.5)     # 等虚拟滚动把新行渲染出来
            cur_px = st
            for _try in range(4):
                r = await _drain_viewport(tag)
                if r != 'need_scroll':
                    if r == 'giveup':
                        return False
                    break
                # 这个视口是从颜色组中间截开的，上面可能还有能传的行 → 往上滚一格重试
                if cur_px is None or cur_px <= 0 or not step:
                    _log(f'  {tag} 顶部有颜色读不出来的行，放弃这个视口')
                    break
                cur_px = max(0, cur_px - step)
                await cdp.js(_JS_SPEC_SCROLL_TO().replace('__TOP__', str(int(cur_px))))
                await asyncio.sleep(0.5)
        return True

    aborted = not await _sweep('')
    if not aborted:
        # 复核：整张表再走一遍。PDD 每次重渲染都可能把某一行的待传状态吞掉，
        # 一遍过完看不出来（用户遇到的就是最后两行没图），所以传完再确认一次。
        if dead or skipped_colors:
            _log('开始复核整张规格表（确认没有漏行的）')
        if not await _sweep('复核 '):
            aborted = True

    # 清掉标记、还原滚动位置，别把页面留在奇怪的地方
    await cdp.js(_JS_CLEAR_ALL_MARKS)
    if scroll_info and steps[0] is not None:
        await cdp.js(_JS_SPEC_SCROLL_TO().replace('__TOP__', str(int(scroll_info.get('top') or 0))))

    if skipped_colors:
        _log(f"  以下颜色没有预览图（保持空白）: {sorted(skipped_colors)}")
    failed = len(dead)
    if failed:
        _log("  这些行没传上预览图（保持空白）: "
             + '、'.join(sorted(k.replace('|', ' ') for k in dead)))
    _log(f"共上传 {uploaded} 个规格预览图" + (f"，失败 {failed} 个" if failed else ""))
    if _logf:
        try:
            _logf.close()
        except Exception:
            pass

    await cdp.screenshot(_tagged('data/spec_preview_upload_done.jpg'))
    print("=== 规格预览图上传完成 ===")

    # 少传了要如实报给调用方（它会拼进最终提示里给用户看）。
    # 以前只 print 一句就 return None，进度条照样写「规格预览图」、最终 message 照样写「完成」，
    # 用户得一张张对着商品页才发现少了几行。
    if not uploaded:
        return '规格预览图一张都没传上去（页面或连接出问题了），请到商品页确认'
    if failed:
        return f'规格预览图有 {failed} 行没传上去（这些行的预览图是空的）'
    return None


async def connect_taobao_tab(url):
    """连接到淘宝页面的CDP WebSocket"""
    tabs = json.loads(urllib.request.urlopen('http://127.0.0.1:9222/json', timeout=10).read())
    for t in tabs:
        if 'taobao.com' in t.get('url', '') and url.split('id=')[-1].split('&')[0] in t.get('url', ''):
            ws = await websockets.connect(t['webSocketDebuggerUrl'], max_size=50*1024*1024)
            cdp = CDPClient(ws)
            await cdp.start()
            return cdp, ws

    # 打开新tab（新版 Chrome 必须用 PUT）
    open_new_tab(9222, url)
    product_id = url.split('id=')[-1].split('&')[0]
    # 等这个标签页出现（原来固定睡 5 秒）
    await _wait_tab_ready(product_id, 9222, timeout=10.0)

    tabs = json.loads(urllib.request.urlopen('http://127.0.0.1:9222/json', timeout=10).read())
    for t in tabs:
        if 'taobao.com' in t.get('url', '') and product_id in t.get('url', ''):
            ws = await websockets.connect(t['webSocketDebuggerUrl'], max_size=50*1024*1024)
            cdp = CDPClient(ws)
            await cdp.start()
            return cdp, ws

    raise Exception('无法连接到淘宝页面')


# ==================== 主图上传到轮播图 ====================

async def upload_main_images_to_carousel(cdp, image_paths):
    """
    在规格页面直接把主图上传到PDD的轮播图位置。
    即使carousel input是隐藏的，DOM.setFileInputFiles也能工作。

    Args:
        cdp: CDPClient实例
        image_paths: 主图本地路径列表

    返回 None 表示没出岔子；返回一句中文表示主图没传成（调用方要把它带给用户看，
    不能静默 —— 2026-09-15 那次就是「主图一张没传」却没报任何东西）。
    """
    if not image_paths:
        return '没有主图可传'

    abs_paths = [os.path.abspath(p) for p in image_paths if os.path.exists(p) and os.path.getsize(p) > 1024]
    if not abs_paths:
        print('  没有有效的主图需要上传到轮播图')
        return '主图文件都不是有效图片（不存在或小于 1KB），一张都没传'

    print(f'  上传 {len(abs_paths)} 张主图到轮播图位置...')

    # 找carousel_img_localfile_upload input。找不到别急着说「没有」——页面（尤其刚填完规格的
    # 大表单）可能还没把这块渲染出来，原来直接 return 就成了静默失败。
    node_ids = []
    _no_page = False
    deadline = time.monotonic() + 5.0
    while True:
        doc = await cdp.call('DOM.getDocument')
        if not doc:
            # call 超时/断连是返回 None：doc['result'] 会直接 TypeError 把整轮上架打断
            _no_page = True
        else:
            root_id = doc.get('result', {}).get('root', {}).get('nodeId')
            search = await cdp.call('DOM.querySelectorAll', {
                'nodeId': root_id,
                'selector': 'input[data-tracking-click-viewid="carousel_img_localfile_upload"]'
            })
            node_ids = ((search or {}).get('result') or {}).get('nodeIds') or []
        if node_ids or time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.4)

    if not node_ids:
        if _no_page:
            return '读页面结构没响应（浏览器卡住或连接断了），主图没传上去'
        # 返回一句中文而不是 None：调用方要把它带进最终结果里告诉用户，
        # 不能像 2026-09-15 那次一样「主图没传」却全程一个字都没说。
        return '轮播图上传框没找到（页面上没有 carousel_img_localfile_upload），主图没传上去'

    # 验证上传结果：等到数量够了就走（原来固定睡 5 秒再读一次）。
    # 只数上传域（pddpic/upload）的图：页面别处也有 swiper（推荐位、模板轮播），
    # 按 [class*=swiper] 里的 img 无条件数，判据会恒真 —— 「一张都没传上」也当成功。
    _js_carousel_count = '''(() => {
        const sel = 'img[src*="pddpic"], img[src*="upload"]';
        const carousel = document.querySelector('[class*="carousel"], [class*="swiper"]');
        if (carousel) {
            const n = carousel.querySelectorAll(sel).length;
            if (n) return n;
        }
        return document.querySelectorAll(sel).length;
    })()'''

    # 上传前的基线：重跑场景（上一次只差主图没传）页面上本来就有图，
    # 「> 0」这种判据第一轮轮询就成立，等于没验证 —— 要比的是这一步新增了几张。
    _b = await cdp.js(_js_carousel_count)
    before = _b if isinstance(_b, int) else None

    # 这个上传框不支持 multiple 的话，Chrome 只会留最后一张（详情图那条路有同样的兜底）
    _m = await cdp.js('''(() => {
        const el = document.querySelector('input[data-tracking-click-viewid="carousel_img_localfile_upload"]');
        return el ? !!el.multiple : null;
    })()''')
    if _m is False and len(abs_paths) > 1:
        print(f'  ⚠ 轮播图上传框不是多选的，{len(abs_paths)} 张里只有最后一张能进去（页面上确认一下）')

    # 直接设置文件（即使input隐藏也能工作）
    r = await cdp.call('DOM.setFileInputFiles', {
        'nodeId': node_ids[0],
        'files': abs_paths
    })
    if r is None:
        return f'设置主图失败：浏览器没响应（页面卡住或连接断了），{len(abs_paths)} 张主图都没传上去'
    if r.get('error'):
        # nodeId 失效在这里是真实现象（页面重渲染），原来直接把返回值丢了 → 一张没传也不吭声
        return f'设置主图失败：{str(r["error"].get("message") or r["error"])[:70]}（主图没传上去）'
    print(f'  已设置 {len(abs_paths)} 张主图到轮播图')

    want = len(abs_paths)
    if before is None:
        _, count = await _wait_for(cdp, _js_carousel_count,
                                   lambda v: isinstance(v, int) and v > 0, timeout=5.0)
        print(f'  轮播图区域图片数: {count}（上传前的张数没读到，只能确认「不是 0」）')
        if not count:
            return '主图设置进去了但轮播图里一张都没显示，请到商品页确认主图'
        return None

    _, count = await _wait_for(cdp, _js_carousel_count,
                               lambda v: isinstance(v, int) and v - before >= want, timeout=5.0)
    count = count if isinstance(count, int) else before
    print(f'  轮播图区域图片数: {before} → {count}（要传 {want} 张）')
    if count - before < want:
        return (f'主图只上去 {count - before} 张（要传 {want} 张），'
                f'请到商品页对着看一遍轮播图')
    return None


# ==================== 详情图上传 ====================

# 详情图一批塞几张 —— **1 张**。一张一张传。
# 2026-09-27 在用户的 47 张商品上量出来的（同一个页面、同一批图，只有「一次塞几张」不同）：
#   * 一次塞 5 张：前 7 批各要 25~30 秒，之后拼多多的上传器越来越慢，最后**完全不收了**
#     —— 后面所有批次等到超时也没反应（那一轮 47 张只上去 35 张，白等了 883 秒）
#   * 一次塞 1 张：**2~4 秒就落地**，稳稳当当。47 张按这个速度 ≈3 分钟
# 所以别再「一批几张小步快跑」了 —— 批次越大越慢、还会把上传器堵死。逐张传最快也最稳。
DETAIL_BATCH = 1

# 一张最多等多久落地（秒）。实测 2~4 秒就上去了，10 秒是 2.5~5 倍余量 ——
# 用户 2026-09-27 说了「不要等太久了」，所以这里不给它留几分钟的傻等时间。
# 那一天实测的两个 20 秒超时（一共 80 秒）全花在「等一张根本不会来的图」上：
# 缩短上限以后，真没上去的图几秒内就认栽，剩下的时间拿来传后面的。
DETAIL_ONE_WAIT_SECS = 10.0

# 一批（多张时才会用到）最多等多久。只有把 DETAIL_BATCH 调大才会走到这个上限。
DETAIL_BATCH_WAIT_SECS = 120.0

# 整个「详情图」这一步最多花多少秒（含补传）。到点了就收工报账，不陪页面耗着 ——
# 一张 4 秒的话 47 张 ≈ 190 秒，300 秒足够；页面真卡死了也不至于把人等成雕像。
DETAIL_TOTAL_BUDGET_SECS = 300.0

# 连着几张没落地就不传了（「实在上传不成功就别上传」，别一直往里塞）。
DETAIL_GIVEUP_STREAK = 3

# 小于这个字节数的图不传（8KB）。2026-09-27 实测：`detail_2.jpg` 只有 2KB，
# 三次上架全都是它「等了 20 秒没落地」—— 它不是正常的详情图（正常详情图几百 KB），
# 拼多多不收也不给红格子，就这么静静地不收。与其白等一轮再报「少了一张」，
# 不如开头就挑出来直接告诉用户「这几张不像详情图，换掉」。
DETAIL_MIN_BYTES = 8192

# 补传最多几轮 —— 只补「塞进去了、但迟迟没落地」的那些（拼多多还没表态，是真丢了）。
# 用户嫌等太久，所以只给一轮。
# ★ 拼多多标了「上传失败」的图**一律不补传**：2026-09-27 用户定的调子 —— 抓来的图里本来就有
#   些不是真详情图，拼多多不认的就是没用的图，没必要硬塞（硬塞还会打断在飞的那一批）。
#   收尾时只把这些红格子删掉，别留在页面上。
DETAIL_REPAIR_ROUNDS = 1


def _detail_batch_wait(chunk_len):
    """这一批最多等多久（秒）。

    逐张传（现在就是这个）就给 DETAIL_ONE_WAIT_SECS；万一把 DETAIL_BATCH 调大了，
    再多给点排队时间（实测多张一起塞会越来越慢，等再久也没用）。
    """
    if chunk_len <= 1:
        return DETAIL_ONE_WAIT_SECS
    return min(DETAIL_BATCH_WAIT_SECS, 30.0 + 15.0 * chunk_len)


# 「详情图到底上去几张」的唯一可信信号 —— 直接数详情区的格子。
# ★ 千万别再用页面上那行「已上传N/50张」：**它把上传失败的也算进去**。2026-09-27 实测：
#   那行字写着「已上传25/50张」，详情区里只有 15 张真图，另外 10 个格子挂着「上传失败」的
#   红色占位块（拼多多自己的 192x192 灰图 + 红边 + 「上传失败」三个字）。拿那行字当成绩单，
#   就会出现「日志里说都对上了、页面上躺着一片红」—— 那轮就是这么把用户蒙过去的。
#   好图 = 格子里 <img> 的 src 是真地址（拼多多的图床链接）；失败 = 格子挂着红色失败标记；
#   其余 = 还在传（灰占位图，既没红标也没有真地址）。
def _JS_DETAIL_STATE():
    """原第 3609 行的模块级长常量（1211 字符）：已移到 assets_src/，

    加密后随包分发。做成函数是为了**延迟解密** —— import 期还没激活，没钥匙。
    """
    return _A('_JS_DETAIL_STATE')


# 删掉一个「上传失败」的格子。一次只删一个，删完由 Python 重新数一遍再决定要不要继续；
# 从最后一个往前删 —— 删除会让后面的格子往前挪，从后往前删才不会删错人。
def _JS_DELETE_ONE_FAILED():
    """原第 3636 行的模块级长常量（843 字符）：已移到 assets_src/，

    加密后随包分发。做成函数是为了**延迟解密** —— import 期还没激活，没钥匙。
    """
    return _A('_JS_DELETE_ONE_FAILED')



def _parse_detail_state(v):
    """把 _JS_DETAIL_STATE 的返回值解析成 dict；读不到/形状不对返回 None（=None 不是 0 张）。

    good/bad/pending 一律要过一遍 bool 检查 —— bool 是 int 的子类，别把 true 当成「1 张」。
    """
    try:
        d = json.loads(v or 'null')
    except Exception:
        return None
    if not isinstance(d, dict) or not d.get('ok'):
        return None
    out = {}
    for k in ('good', 'bad', 'pending', 'total'):
        n = d.get(k)
        out[k] = n if isinstance(n, int) and not isinstance(n, bool) else 0
    out['counter'] = d.get('counter') if isinstance(d.get('counter'), str) else ''
    return out


async def upload_detail_images(cdp, image_paths, stats=None):
    """
    上传商品详情图到PDD。

    Args:
        cdp: CDPClient实例
        image_paths: 图片路径列表
        stats: 可选。传个 dict 进来，会把「要传几张、真上去几张」写进去 —— 给调用方的
               进度条用。别再拿「提交了 47 张」当「47 张都上去了」（用户 2026-09-27：
               「没有说少传几张」—— 时间轴上写着 47，实际只上去 44）。
    """
    license.guard('上传详情图')
    # 报账用的一份数（也是给调用方的 stats）
    _out = stats if stats is not None else {}
    _out.update({'total': 0, 'good': None, 'sent': 0, 'miss': None, 'rejected': None,
                 'tiny': 0, 'dead': False})

    print('=== 上传详情图 ===')

    # 筛选有效图片：先按大小分三档 —— 用不了的小图，别让它白占一轮等待
    abs_paths, _tiny, _gone = [], [], []
    for p in image_paths:
        if not os.path.exists(p):
            _gone.append(os.path.basename(p))
            continue
        try:
            _sz = os.path.getsize(p)
        except OSError:
            _gone.append(os.path.basename(p))
            continue
        if _sz <= 1024:
            _gone.append(os.path.basename(p))
        elif _sz < DETAIL_MIN_BYTES:
            _tiny.append(os.path.basename(p))
        else:
            abs_paths.append(os.path.abspath(p))

    _out['total'] = len(abs_paths)
    _out['tiny'] = len(_tiny)
    _tiny_note = ''
    if _tiny:
        # 这类小图传了也是白等（实测 detail_2.jpg 只有 2KB，连着三轮上架都没落地，
        # 拼多多连红格子都不给）—— 不如直接说清楚，让用户换图。
        _shown = '、'.join(_tiny[:5]) + ('…' if len(_tiny) > 5 else '')
        print(f'  跳过 {len(_tiny)} 张太小的图（不到 {DETAIL_MIN_BYTES // 1024}KB，不像详情图）：{_shown}')
        _tiny_note = (f'有 {len(_tiny)} 张图小得不像详情图（{_shown}），这类图拼多多一直不收，'
                      f'已跳过 —— 建议把这几个文件换掉')
    if not abs_paths:
        print('  没有有效的详情图需要上传')
        _why_none = '详情图的文件都不在了或不是有效图片，一张都没传'
        return f'{_tiny_note}；{_why_none}' if _tiny_note else _why_none
    print(f'  待上传 {len(abs_paths)} 张详情图')
    if _gone:
        print(f'  另有 {len(_gone)} 张文件不在了，跳过：{"、".join(_gone[:5])}')

    # 用data-tracking-click-viewid精确定位详情图上传input
    doc = await cdp.call('DOM.getDocument')
    if not doc:
        # call 超时/断连返回 None，doc.get 会 AttributeError 打断整轮上架（后面填价格全不跑）
        print('  读页面结构没响应（浏览器卡住或连接断了），详情图跳过')
        return '读页面结构没响应（浏览器卡住或连接断了），详情图没传上去'
    root_id = doc.get('result', {}).get('root', {}).get('nodeId')

    # 先尝试通过data-tracking-click-viewid找到
    search = await cdp.call('DOM.querySelectorAll', {
        'nodeId': root_id,
        'selector': 'input[data-tracking-click-viewid="detail_img_localfile_upload"]'
    })
    node_ids = ((search or {}).get('result') or {}).get('nodeIds') or []

    if not node_ids:
        # 备选：在"商品详情"区域找accept含image的input
        print('  未找到detail_img_localfile_upload，尝试备选方案...')
        all_search = await cdp.call('DOM.querySelectorAll', {
            'nodeId': root_id,
            'selector': 'input[type=file][accept*=image]'
        })
        all_node_ids = ((all_search or {}).get('result') or {}).get('nodeIds') or []
        # 取最后一个visible的image input（详情图通常在页面底部）
        for nid in reversed(all_node_ids):
            attrs = await cdp.call('DOM.getAttributes', {'nodeId': nid})
            if attrs and 'result' in attrs:
                attr_list = attrs['result']['attributes']
                attr_dict = {}
                for i in range(0, len(attr_list), 2):
                    attr_dict[attr_list[i]] = attr_list[i + 1]
                # 检查是否在详情区域
                desc = await cdp.call('DOM.describeNode', {'nodeId': nid})
                # 简单检查：不是主图input（主图input的tracking id是upload_image_label）
                tracking_id = attr_dict.get('data-tracking-click-viewid', '')
                if tracking_id != 'upload_image_label':
                    node_ids = [nid]
                    break

    if not node_ids:
        print('  未找到详情图上传input，跳过')
        return '详情图上传框没找到（页面上没有 detail_img_localfile_upload），详情图没传上去'

    # 先让input可见（visibility:hidden会导致React不触发handler）
    await cdp.js(_A('upload_detail_images_3774'))
    await asyncio.sleep(0.3)

    async def _state():
        """详情区现在什么样：真好图 / 上传失败 / 还在传 各几张。读不到返回 None（≠ 0 张）。"""
        return _parse_detail_state(await cdp.js(_JS_DETAIL_STATE()))

    _st0 = await _state()
    if _st0 is None:
        print('  读不到详情区的格子（改版了？）—— 这一轮没法替你核对，传完请对着商品页看一眼')
    else:
        print(f'  这轮开始前：详情区真好图 {_st0["good"]} 张、挂着「上传失败」{_st0["bad"]} 张')

    # 手动触发change事件确保React感知（复用同一段 JS）
    _js_poke = '''(() => {
        let inp = document.querySelector('input[data-tracking-click-viewid="detail_img_localfile_upload"]');
        if (inp) {
            inp.dispatchEvent(new Event('change', {bubbles: true}));
            inp.dispatchEvent(new Event('input', {bubbles: true}));
        }
    })()'''

    _root = {'id': None}        # 文档根 nodeId（见 _fresh_node 里的说明）

    async def _fresh_node():
        """取上传框**当前**的 nodeId（每一张都要重取）。

        页面（React）一重渲染，老 nodeId 就指向一个「已脱离文档」的节点，而
        setFileInputFiles 对着脱离的节点要么报错、要么更糟 —— 不报错但文件悄悄丢了。
        所以每一张都重新查一次。

        但**不必**每次都 `DOM.getDocument`：那一步会把整棵 DOM 序列化一遍，这页很大，
        一次几百毫秒，47 张光学它就要几十秒。文档根 nodeId 在 React 重渲染时是不变的
        （换的是里面那些节点），所以缓存它，每张只做一次 querySelectorAll（很便宜）。
        万一文档真被换掉了（整页跳转），querySelectorAll 会报错或查不到 —— 那就把缓存
        丢掉再取一次，别拿一个废根一直问。
        """
        for _attempt in (1, 2):
            if _root['id'] is None:
                _d = await cdp.call('DOM.getDocument')
                if not _d:
                    return None
                _root['id'] = _d.get('result', {}).get('root', {}).get('nodeId')
                if _root['id'] is None:
                    return None
            _r = await cdp.call('DOM.querySelectorAll', {
                'nodeId': _root['id'],
                'selector': 'input[data-tracking-click-viewid="detail_img_localfile_upload"]'
            })
            if _r is None:
                return None                      # 超时/断连：当没取到，别拿老节点硬塞
            if 'result' not in _r:
                _root['id'] = None               # 根不管用了 → 丢掉缓存，重来一次
                continue
            _ids = (_r.get('result') or {}).get('nodeIds') or []
            if _ids:
                return _ids[0]
            if _attempt == 1:
                _root['id'] = None               # 查不到也可能是根过期了，再确认一遍
                continue
            return None                          # 两次都查不到：上传框真不在页面上
        return None

    # ★ 一张一张塞（DETAIL_BATCH=1）。上传框虽然是 multiple，但「能多选」不等于「能一次吃下几张」——
    # 2026-09-27 实测：一次塞几十张，拼多多渲染进程当场卡死、CDP 连接直接断（整轮上架崩）；
    # 一次塞 5 张，撑到 35 张之后上传器就彻底不收了。逐张传反而最快（2~4 秒一张）。
    _sent = 0              # 塞进去的张数（读不到格子时只能按这个记账）
    _dead = False          # 浏览器连接断了：这时候说什么「读不到格子」都是误导
    _retry = []            # 没落地的批（每个元素是一个 chunk），等会儿补传

    _base_good = _st0['good'] if _st0 is not None else 0     # 这轮开始前详情区已有的图
    _need_total = _base_good + len(abs_paths)
    _unit = '张' if DETAIL_BATCH == 1 else '批'

    async def _send_batch(chunk, tag, budget_left=None):
        """塞一批进去、等它**真的落到页面上**。返回：
             'ok'       这批的图都在详情区出现了
             'rejected' 页面上冒出「上传失败」的红格子 —— 拼多多不收这几张（不补传，收尾时删掉）
             'unclear'  没落地、也没标失败（可能还在排队）
             'unknown' 读不到详情区格子（核不了）
             'cdp'     没塞进去（CDP 报错/没响应）
             'dead'    浏览器连接断了
             'nonode'  上传框找不到（页面重渲染/改版）
        """
        nonlocal _sent
        _cnt = f'（{len(chunk)} 张）' if len(chunk) > 1 else ''
        _node = await _fresh_node()
        if not _node:
            # 连不上和「页面改版了」是两回事，别把前者说成后者
            if getattr(cdp, 'closed_error', None):
                print(f'  {tag}：浏览器连接断了（{cdp.closed_error}）')
                return 'dead'
            print(f'  {tag}：上传框找不到了（页面重渲染/改版了）')
            return 'nonode'
        _before = await _state()
        _r = await cdp.call('DOM.setFileInputFiles', {'nodeId': _node, 'files': chunk})
        # ★ 没回复也算失败！_r 是 None 意味着超时或连接断了（见 CDPClient.call），
        # 老代码 `if _r and 'error' in _r` 把 None 当成功 —— 于是连接断了它照样往下走，
        # 白等几十秒，最后报个「只多出 0 张」就当提醒过了。
        if not _r or 'error' in _r:
            if getattr(cdp, 'closed_error', None):
                print(f'  {tag}{_cnt}没塞进去（浏览器连接断了：{cdp.closed_error}）')
                return 'dead'
            _why = ((_r or {}).get('error', {}).get('message') or '页面没响应（卡住或连接断了）')
            print(f'  {tag}{_cnt}没塞进去（{str(_why)[:70]}），跳过')
            return 'cdp'
        _sent += len(chunk)
        await cdp.js(_js_poke)
        _need = len(chunk) if _before is None else _before['good'] + len(chunk)
        _tmo = _detail_batch_wait(len(chunk))
        if _before is None:
            _tmo = min(_tmo, 10.0)      # 读不到格子时判据永远不成立，等也是白等
        if budget_left is not None:
            _tmo = min(_tmo, max(1.0, budget_left))   # 别为了这一张把整步的总时间耗光
        await _wait_for(
            cdp, _JS_DETAIL_STATE(),
            lambda v, _n=_need: (_parse_detail_state(v) or {}).get('good', -1) >= _n,
            timeout=_tmo, interval=1.0)
        _now = await _state()
        if _now is None:
            print(f'  {tag}{_cnt}已塞入（读不到详情区格子，核不了）')
            return 'unknown'
        if _now['good'] - (_before['good'] if _before else 0) >= len(chunk):
            print(f'  {tag} 已落地（详情区 {_now["good"]}/{_need_total} 张、失败 {_now["bad"]} 张）')
            return 'ok'
        if _before is not None and _now['bad'] > _before['bad']:
            # 拼多多明确不收这几张 → 不硬塞（这些图本来就不是有效详情图）。只在最后把红格子清掉。
            print(f'  {tag}{_cnt}里有 {_now["bad"] - _before["bad"]} 张拼多多没认'
                  f'（页面上的红格子）—— 按没用的图跳过，不补传')
            return 'rejected'
        print(f'  {tag}{_cnt}等了 {_tmo:.0f} 秒没落地、也没标失败')
        return 'unclear'

    # 一张一张塞。上传框是不是 multiple 都走这条路 —— DETAIL_BATCH=1 时每条 _chunks 就是一个文件。
    _chunks = [abs_paths[_i:_i + DETAIL_BATCH] for _i in range(0, len(abs_paths), DETAIL_BATCH)]
    print(f'  逐张传 {len(abs_paths)} 张（一张一张最快也最稳；整步最多花 {DETAIL_TOTAL_BUDGET_SECS:.0f} 秒）...')
    _stalled = 0        # 连着几张没落地。不先赋值的话，第一次就 UnboundLocalError 把整轮打断
    _t0 = time.time()
    _ran_out = False
    for _bi, _chunk in enumerate(_chunks):
        _left = DETAIL_TOTAL_BUDGET_SECS - (time.time() - _t0)
        if _left <= 0:
            _ran_out = True
            print(f'  时间到了（整步上限 {DETAIL_TOTAL_BUDGET_SECS:.0f} 秒），'
                  f'剩下 {len(_chunks) - _bi} 张不传了 —— 实在传不上去就不耗着（用户 2026-09-27）')
            break
        _res = await _send_batch(_chunk, f'第{_bi + 1}{_unit}', budget_left=_left)
        if _res in ('dead', 'nonode'):
            _dead = (_res == 'dead')
            _retry.extend(_chunks[_bi:])
            break
        if _res == 'ok':
            _stalled = 0
            continue
        if _res == 'unknown':
            # 读不到格子 ≠ 这张失败。接着往下传，最后如实说「核不了」——
            # 总比停在这儿、剩一半没传要强。
            continue
        if _res == 'rejected':
            # 拼多多不认的图：跳过，接着传下一张（也不记进 _retry —— 补传名单里不能有它）
            continue
        _retry.append(_chunk)
        _stalled += 1
        if _stalled >= DETAIL_GIVEUP_STREAK:
            # 连着几张都不落地 = 拼多多这会儿不收了。再塞下去只是白等（上一天实测：
            # 卡住之后又硬等了 500 多秒，一张都没多上去），剩下的交给补传那一轮。
            print(f'  连着 {_stalled} 张都没落地 —— 先停手不塞了，剩下的等会儿补一次')
            _retry.extend(_chunks[_bi + 1:])
            break

    # 补传：只补「塞进去了、但迟迟没落地」的那些（_retry 里只有这种）。
    # ★ 拼多多标了「上传失败」的图不在这个名单里 —— 那些是拼多多不收的图，跳过就行。
    if _retry and not _dead and _st0 is not None:
        for _round in range(1, DETAIL_REPAIR_ROUNDS + 1):
            _left = DETAIL_TOTAL_BUDGET_SECS - (time.time() - _t0)
            if _left <= 0:
                print('  时间到了，补传也不补了')
                break
            _st = await _state()
            if _st is None:
                break
            if _st['good'] - _base_good >= len(abs_paths):
                # 有的图是「慢半拍」：等待上限内没落地，后来自己上去了。已经齐了就别再塞一遍
                # （再塞一遍就是重复图，用户还得手动删）。
                print('  刚才超时的那几张后来自己上去了（详情区已经齐了），不用补')
                _retry = []
                break
            print(f'  补一轮：{sum(len(c) for c in _retry)} 张没落地'
                  f'（现在详情区真好图 {_st["good"]} 张、失败 {_st["bad"]} 张）')
            _again = []
            for _ci, _chunk in enumerate(_retry):
                _res = await _send_batch(_chunk, f'补第{_ci + 1}{_unit}',
                                         budget_left=DETAIL_TOTAL_BUDGET_SECS - (time.time() - _t0))
                if _res in ('dead', 'nonode'):
                    _dead = (_res == 'dead')
                    _again = []
                    break
                if _res in ('cdp', 'unclear'):
                    _again.append(_chunk)
            _retry = _again
            if not _retry:
                break

    # 收尾：还有灰格子（正在传）就等它们定下来 —— 只读的等待，不会有风险。
    _st_end = await _state()
    if _st_end is not None and _st_end['pending'] and _sent and not _dead:
        print(f'  还有 {_st_end["pending"]} 张在传（页面上的灰格子），等它们定下来...')
        await _wait_for(
            cdp, _JS_DETAIL_STATE(),
            lambda v: (_parse_detail_state(v) or {}).get('pending', 1) == 0,
            timeout=min(60.0, 10.0 + 5.0 * _st_end['pending']), interval=1.0)
        _st_end = await _state()

    # 清场：把「上传失败」的红格子删掉。拼多多不收的图不该留在页面上（用户 2026-09-27：
    # 这些图本来就不是有效详情图，没必要硬塞）—— 删格子不动已经传上去的图，所以这一步安全。
    # _cleaned 记着「删掉几个红格子」= 有几张是拼多多不认的，最后报账时不能把它们算成「丢了」。
    _cleaned = 0
    _counter_seen = ''
    if not _dead and _st0 is not None:
        _st_now = await _state()
        if _st_now is not None and _st_now['bad']:
            # 删之前先记下页面那行字当时写的是什么 —— 它把不认的也算成「已上传」，
            # 报账时拿它当反例（说明为什么只认格子数）
            _counter_seen = _st_now['counter']
            print(f'  清掉 {_st_now["bad"]} 个「上传失败」的红格子（拼多多不认这几张，不补传）...')
            for _ in range(_st_now['bad'] + 2):
                _r = await cdp.js(_JS_DELETE_ONE_FAILED())
                if _r != 'clicked':
                    if _r not in (None, 'none-failed'):
                        print(f'  删红格子没做成（{_r}），剩下的不删了')
                    break
                _cleaned += 1
                await asyncio.sleep(0.8)     # 等页面把格子删掉再删下一个
            print(f'  删掉 {_cleaned} 个红格子')
            _st_end = await _state()

    # 传完回头核对。核对不上必须说出来 ——「静默少几张」比整个失败还难查。
    _out['sent'] = _sent
    _warn = None
    if _dead:
        _out['dead'] = True
        _warn = (f'详情图只塞进去 {_sent}/{len(abs_paths)} 张 —— 浏览器连接断了'
                 f'（{getattr(cdp, "closed_error", "") or "页面关了或卡死了"}），后面的没传完，'
                 f'请到商品页对着看一遍')
        print(f'  ⚠ {_warn}')
    elif _st_end is None:
        # 格子读不到 → 不能假装核对过，更不能拿页面上那行「已上传」当成绩单（它把失败也算进去）。
        _warn = (f'详情图塞进去了 {_sent}/{len(abs_paths)} 张，但读不到详情区的格子，'
                 f'没法替你核对，请到商品页对着看一遍')
        print(f'  ⚠ {_warn}')
    else:
        # ★ 这轮开始前就有 1 张的话，它是「原有的」，不算这 47 张里的成绩 —— 不扣掉的话
        #   会把「少了 12 张」报成「少了 11 张」（少报一张，用户按这个数去补就会漏一张）。
        _good = max(0, _st_end['good'] - _base_good)
        # 拼多多不认的张数 = 清掉的红格子 + 没清掉的（清场那步没跑成时才会剩）
        _rejected = _cleaned + _st_end['bad']
        # 「没进详情区」= 连格子都没占上。必须把「拼多多不认的」单独扣掉，不然清完红格子以后
        # 它们会被算成「真丢了」——「不收 5 张」和「还有 5 张没上去」完全是两回事。
        _miss = max(0, len(abs_paths) - _good - _rejected)
        _out['good'] = _good
        _out['miss'] = _miss
        _out['rejected'] = _rejected
        # ★ 每一档都要把「要传几张、上去几张」说全 —— 用户 2026-09-27：「没有说少传几张」。
        #   时间轴上原来写的是「详情图（47张）」，那是**提交**的张数，不是上去的张数。
        if _good >= len(abs_paths) and _rejected == 0:
            print(f'  要传 {len(abs_paths)} 张、上去 {_good} 张 —— 都对上了')
        elif _miss == 0:
            # 每张都有交代（上去的 + 拼多多不收的）。这是正常收场，不是「少了几张」——
            # 抓来的图里本来就有不是真详情图的，拼多多不认就是不认，跳过即可。
            print(f'  详情区真好图 {_good} 张；另外 {_rejected} 张拼多多不认，红格子已清掉'
                  f'—— 这些图本来就不是有效详情图，跳过')
            print(f'    （删除前页面那行写的是「{_counter_seen or _st_end["counter"] or "已上传?"}」，'
                  f'把不认的也算进「已上传」了 —— 所以只认格子数）')
            _warn = (f'详情图要传 {len(abs_paths)} 张、上去 {_good} 张，'
                     f'{_rejected} 张拼多多不认已跳过（不影响上架）')
        else:
            _why_left = '（这步到时间了就先收工了）' if _ran_out else ''
            # 点名是哪几张。只说「少了 3 张」用户没法下手 —— 得知道是哪个文件。
            _miss_files = [os.path.basename(_p) for _c in _retry for _p in _c]
            _who = ''
            if _miss_files:
                _shown = '、'.join(_miss_files[:6]) + ('…' if len(_miss_files) > 6 else '')
                print(f'    没上去的是：{_shown}')
                _who = f'（{_shown}）'
            print(f'  详情图要传 {len(abs_paths)} 张、上去 {_good} 张，少 {_miss} 张{_who}{_why_left}')
            if _rejected:
                print(f'    （另有 {_rejected} 张拼多多不认，红格子已清掉，不算在这 {_miss} 张里）')
            print(f'    页面那行「{_st_end["counter"] or "已上传?"}」把失败/不认的也算进去了，'
                  f'不能当成绩单用')
            # 少几张是常事（抓来的图本来就有杂七杂八的），照实说就行，别吓唬人。
            _warn = (f'详情图要传 {len(abs_paths)} 张、上去 {_good} 张，少 {_miss} 张{_who}{_why_left}'
                     + (f'；另有 {_rejected} 张拼多多不认已跳过' if _rejected else '')
                     + '。少几张不影响上架')

    # 恢复input原始状态（隐藏）。连浏览器都断了的话别在这儿抛异常 —— 详情图这段已经完事了，
    # 抛出去会把整轮上架打断（2026-09-27 就是这一句把 run 打死的，日志里只剩个 [LISTING ERROR]）。
    try:
        await cdp.js(_A('upload_detail_images_4084'))
        # 让界面画完再截图（原来睡 3 秒）
        await asyncio.sleep(1)
        await cdp.screenshot(_tagged('data/detail_upload_done.jpg'))
    except Exception as e:
        print(f'  收尾（还原上传框/截图）没做成：{type(e).__name__}: {e}')

    # 「太小的图被跳过」也得说一声。不说的话用户只看到「少了几张」，分不清是
    # 「图本身不行，换个正常大小的就行」还是「工具没干成」—— 这两种处理方式完全不同。
    if _tiny_note:
        _warn = f'{_warn}；{_tiny_note}' if _warn else _tiny_note

    print(f'=== 详情图上传完成 ({len(abs_paths)}张) ===')
    return _warn

