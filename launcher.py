"""
拼多多一键上架 - 启动器

打包后的统一入口，负责：
1. 启动内置 Chromium（Playwright），暴露 CDP 9222 端口供自动化代码连接
2. 启动 FastAPI 服务器（端口 3000）
3. 打开淘宝 + 拼多多商家后台标签页
4. 打开工具界面
"""
import os
import sys
import time
import json
import threading
import webbrowser
from pathlib import Path

import license


def get_base_dir() -> Path:
    """获取程序根目录（兼容 PyInstaller 打包与源码运行）。"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = get_base_dir()

# web 服务线程启动失败时记一笔（端口被别人占了等），主线程据此决定「不要再往下走」
_WEB_START_ERROR = {}


def start_web_server():
    """在后台线程启动 FastAPI 服务器。"""
    import uvicorn
    from web.app import app
    try:
        uvicorn.run(app, host='127.0.0.1', port=3000, log_level='warning')
    except BaseException as e:
        # 端口被占（另一个实例刚起来）时 uvicorn 是 sys.exit(1)，SystemExit 不是 Exception，
        # 必须抓 BaseException，否则主线程毫不知情地继续往下走
        _WEB_START_ERROR['err'] = str(e) or repr(e)
        print(f'[错误] web 服务没能启动：{_WEB_START_ERROR["err"]}')


def find_chrome() -> str:
    """定位内置 Chromium 可执行文件（兼容打包后 browsers 目录 / 源码 build_browsers / 默认 ms-playwright）。"""
    candidates = []
    for rel in ('browsers', 'build_browsers'):
        base = BASE_DIR / rel
        if base.exists():
            candidates.extend(str(p) for p in base.glob('chromium-*/chrome-win*/chrome.exe'))
    local_app = os.environ.get('LOCALAPPDATA')
    if local_app:
        ms = Path(local_app) / 'ms-playwright'
        if ms.exists():
            candidates.extend(str(p) for p in ms.glob('chromium-*/chrome-win*/chrome.exe'))
    for c in candidates:
        if os.path.exists(c):
            return c
    raise FileNotFoundError('未找到 Chromium 浏览器内核')


def _chrome_pid_file() -> Path:
    data_dir = BASE_DIR / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / 'chrome.pid'


def _kill_chrome_tree(pid):
    """杀掉 chromium 进程树（taskkill /T），避免残留子进程占用 9222 端口。"""
    import subprocess
    try:
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)],
                       capture_output=True, creationflags=0x08000000)
    except Exception:
        pass


def _cleanup_orphan_chrome():
    """杀掉上次未正常退出（直接关窗口）遗留的 chromium，否则 9222 端口/浏览器数据锁被占用，
    再次启动会失败或异常。"""
    import subprocess
    pid_file = _chrome_pid_file()
    try:
        if not pid_file.exists():
            return
        old_pid = int(pid_file.read_text(encoding='utf-8').strip())
        # 确认还是 chrome.exe 才杀，避免 PID 被系统复用后误杀别的程序
        # errors='replace'：tasklist 的输出是控制台代码页（中文系统上是 GBK），
        # 而工具跑在 UTF-8 模式下，按 UTF-8 硬解会抛 UnicodeDecodeError —— 异常被下面的
        # except 吞掉，后果是「残留的 chromium 没被杀掉、pid 文件也一直留着」。
        out = subprocess.run(['tasklist', '/FI', f'PID eq {old_pid}', '/FO', 'CSV', '/NH'],
                             capture_output=True, text=True, errors='replace',
                             creationflags=0x08000000).stdout
        if 'chrome.exe' in out.lower():
            _kill_chrome_tree(old_pid)
        pid_file.unlink(missing_ok=True)
    except Exception:
        pass


def _launch_chrome(port: int, profile_dir: Path, urls: list):
    """用原生 Chromium 启动一个独立的调试实例（不经 Playwright）。已经在跑就返回 None。

    不设 --enable-automation，navigator.webdriver 天然为 false，淘宝不会误判自动化。
    强制调试端口绑定 IPv4 的 127.0.0.1：Chromium 1228 在部分 Windows 上默认绑到
    IPv6 的 ::1，导致连接代码里写死的 127.0.0.1 连不上、误判浏览器未启动而重复拉起窗口。

    port: CDP 调试端口；profile_dir: 独立的 --user-data-dir（不同店铺隔离登录态）；
    urls: 启动时打开的页面列表。

    **已经有实例在这个端口上跑时，绝不重复拉起**（返回 None，让调用方复用）：
    对同一个 profile 再执行一次 chrome.exe，Chromium 不会给你开一个新窗口，而是把命令行里
    这些 URL 当成「又开了几个标签页」塞进那个已经在跑的窗口 —— 于是「点一次按钮」的观感就是
    冒出来好几个一模一样的页面（2026-09-27 用户报「一次打开，打开了四个」，店铺窗口里堆了
    5 个同款拼多多登录页就是这么来的）。
    """
    import subprocess

    if _port_alive(port):
        print(f'[浏览器] {port} 端口上已经有实例在跑，直接复用，不再拉起一个')
        return None

    chrome = find_chrome()
    profile_dir.mkdir(parents=True, exist_ok=True)

    # 清理上次未正常退出遗留的 Singleton 锁文件，否则 chromium 会误判已运行而启动失败。
    # 删不掉的就别硬删：Windows 上文件被占用说明确实有实例正拿着这个 profile，
    # 留着它 Chromium 自己就会把 URL 转交给那个实例（一个窗口），比我们删掉锁、
    # 让它再起一个第二个实例要安全得多。
    for name in ('SingletonLock', 'SingletonCookie', 'SingletonSocket', 'lockfile'):
        fp = profile_dir / name
        if fp.exists():
            try:
                fp.unlink()
            except OSError:
                print(f'[浏览器] {name} 删不掉（profile 正被占用），留给 Chromium 自己处理')

    cmd = [
        chrome,
        f'--remote-debugging-port={port}',
        '--remote-debugging-address=127.0.0.1',
        f'--user-data-dir={profile_dir}',
        '--disable-blink-features=AutomationControlled',
        '--no-first-run',
        '--no-default-browser-check',
        '--disable-features=TranslateUI,MediaRouter',
        # 崩溃日志：拼多多的页面进程在上传 Excel 那一刻崩过（2026-09-15 08:13），
        # 光看现象查不出为什么崩。让 Chromium 自己写下来 —— 崩溃时刻、退出码都在里面。
        # 日志写在各自店铺的 profile 里（删 profile 就一起没了），不往程序目录里丢东西。
        '--enable-logging',
        '--log-file=' + str(profile_dir / 'chrome_debug.log'),
        '--enable-crash-reporter',
        '--crash-dumps-dir=' + str(profile_dir / 'Crashpad'),
    ] + list(urls)
    return subprocess.Popen(cmd)


def start_browser(include_shopping: bool = True):
    """启动内置 Chromium（不经 Playwright），暴露 CDP 9222。

    工具页 http://127.0.0.1:3000 始终在本浏览器里打开——而不用系统默认浏览器，
    以免租户默认浏览器（如夸克）与系统代理冲突，连不上本机服务（表现为 'Failed to fetch'）。
    已授权时额外打开淘宝 + 拼多多后台。
    """
    browser_data_dir = BASE_DIR / 'browser_data'
    urls = ['http://127.0.0.1:3000']
    if include_shopping:
        urls += ['https://www.taobao.com', 'https://mms.pinduoduo.com']
    proc = _launch_chrome(9222, browser_data_dir, urls)
    if proc is None:
        # 已经在跑：chrome.pid 里记的就是那个实例，别覆盖成别的（覆盖了下次就杀错/杀不掉）
        return None
    try:
        _chrome_pid_file().write_text(str(proc.pid), encoding='utf-8')
    except Exception:
        pass
    return proc


# ==================== 多店铺浏览器（每个店铺独立 profile + 独立调试端口） ====================

def _shop_registry_file() -> Path:
    data_dir = BASE_DIR / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / 'shop_browsers.json'


def _load_shop_registry() -> dict:
    try:
        return json.loads(_shop_registry_file().read_text(encoding='utf-8'))
    except Exception:
        return {"next_id": 2, "shops": []}


def _save_shop_registry(reg: dict):
    try:
        _shop_registry_file().write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass


def _port_in_use(port: int) -> bool:
    """本地这个端口有没有人在监听（用来判断「工具已经在跑了」）。不请求 HTTP，只连一下。"""
    import socket
    s = socket.socket()
    try:
        s.settimeout(1.0)
        return s.connect_ex(('127.0.0.1', port)) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _port_bindable(port: int) -> bool:
    """这个端口现在能不能真的绑上（能绑 = 没有别人占着）。

    比 _port_in_use 严格：后者是「连一下看有没有人应答」，而刚双击启动的第二个实例探测时，
    第一个实例可能还在 import fastapi/web.app（冷启动 1~3 秒），端口还没开始监听 ——
    连接失败 → 判定「没在跑」→ 继续往下，然后（Windows 上 bind 失败起不来服务，但主进程
    不退出）执行到 _cleanup_orphan_chrome()，把第一个实例的浏览器杀掉。这个探测在这段
    窗口里也能挡住：端口被占时 bind 会直接失败。
    """
    import socket
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', port))
        return True
    except OSError:
        return False
    except Exception:
        return True                      # 探测本身出问题就不拦（宁可放行，别把工具挡在门外）
    finally:
        try:
            s.close()
        except Exception:
            pass


def _port_alive(port: int) -> bool:
    import urllib.request
    try:
        urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=2)
        return True
    except Exception:
        return False


def _wait_port(port: int, seconds: float = 20.0) -> bool:
    """等调试端口起来（Chromium 冷启动要好几秒）。返回是否等到了。

    没有它就会把「刚启动、还没绑端口」的浏览器当成「窗口已经关了」。
    """
    import time
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _port_alive(port):
            return True
        time.sleep(0.3)
    return False


def list_shop_ports() -> list:
    """返回所有可能需要搜索的 CDP 调试端口：已登记的店铺窗口在前，主浏览器 9222 兜底。

    多店铺上架时按此顺序找「停在发布商品页」的那个浏览器。店铺窗口优先，是因为主浏览器
    上架后往往残留一个 goods/category 标签，若优先搜主浏览器会误填到第一家店。
    """
    ports = []
    reg = _load_shop_registry()
    for s in reg.get('shops', []):
        p = s.get('port')
        if p and p not in ports:
            ports.append(p)
    if 9222 not in ports:
        ports.append(9222)
    return ports


def shop_slots() -> list:
    """已登记的店铺槽位 + 存活状态（「选择上架到哪几家店」的弹窗用）。

    比 list_shop_ports() 多两样：槽位号（用户认窗口靠它）和窗口活没活。
    主浏览器 9222 不在登记表里，单独用 slot=None 追加在最后 —— 它也可能登着一家店
    （单店铺模式下本来就是它在干活），不该在弹窗里消失。
    """
    out = []
    seen = set()
    for s in _load_shop_registry().get('shops', []):
        p = s.get('port')
        if not p or p in seen:
            continue
        seen.add(p)
        out.append({'slot': s.get('id'), 'port': p, 'alive': _port_alive(p)})
    if 9222 not in seen:
        out.append({'slot': None, 'port': 9222, 'alive': _port_alive(9222)})
    return out


_OPEN_SHOP_LOCK = threading.Lock()   # 「判断槽位死没死 → 启动浏览器」这一对必须原子（见 open_new_shop）


def open_new_shop() -> int:
    """打开一个店铺浏览器窗口：独立 profile + 独立调试端口，导航到 PDD 商家后台登录页。

    优先复用「已登记但窗口已关」的店铺槽位（登录态保留在 profile 里，重开即免登录）；
    没有可复用的才新建下一个槽位。返回该窗口的调试端口号。

    整段套一把锁，而且**启动完要等端口真的通了才返回**：工具页那句提示是「想登录更多店
    就再点一次本按钮」，用户连点两下（或者点在浏览器还没绑上端口的那两三秒里）就会走进来
    第二次；只要端口还没通，第二次就会把同一个槽位判成「窗口已关」再拉一次 —— 对同一个
    profile 的第二次启动不会开新窗口，只会往那个窗口里再塞一个一模一样的登录页。
    2026-09-27 用户报「一次打开，打开了四个」，那个店铺窗口里就堆了 5 个同款标签页。
    """
    with _OPEN_SHOP_LOCK:
        reg = _load_shop_registry()
        shops = reg.get('shops', [])

        # 1. 复用已登记但窗口已关的槽位
        for s in shops:
            if _port_alive(s['port']):
                continue      # 这个槽位的窗口还开着，看下一个
            profile_dir = BASE_DIR / s['profile']
            proc = _launch_chrome(s['port'], profile_dir, ['https://mms.pinduoduo.com/'])
            if proc is not None:
                s['pid'] = proc.pid
            _save_shop_registry(reg)
            if not _wait_port(s['port']) and proc is not None:
                raise RuntimeError(f'店铺窗口（端口 {s["port"]}）启动后 20 秒内没能就绪')
            return s['port']

        # 2. 新建下一个店铺槽位
        next_id = int(reg.get('next_id', 2))
        port = 9221 + next_id
        profile_dir = BASE_DIR / f'browser_data_{next_id}'
        proc = _launch_chrome(port, profile_dir, ['https://mms.pinduoduo.com/'])
        shops.append({
            'id': next_id,
            'port': port,
            'profile': f'browser_data_{next_id}',
            'pid': proc.pid if proc is not None else None,
        })
        reg['next_id'] = next_id + 1
        _save_shop_registry(reg)
        if not _wait_port(port) and proc is not None:
            raise RuntimeError(f'店铺窗口（端口 {port}）启动后 20 秒内没能就绪')
        return port


def _kill_shop_browsers():
    """退出时杀掉所有店铺浏览器进程，避免遗留占用调试端口。登录态保留在各自 profile 里。"""
    import subprocess
    reg = _load_shop_registry()
    for s in reg.get('shops', []):
        pid = s.get('pid')
        if not pid:
            continue
        try:
            out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
                                 capture_output=True, text=True, errors='replace',
                                 creationflags=0x08000000).stdout
            if 'chrome.exe' in out.lower():
                _kill_chrome_tree(pid)
        except Exception:
            pass


def main():
    os.chdir(BASE_DIR)

    # 授权校验（启动门禁）
    try:
        license.check_and_update()
    except Exception as e:
        print(f'[警告] 授权校验异常: {e}')

    # 单实例：工具已经在跑就别再开第二个。
    # 第二次启动做的事是破坏性的 —— 它的 _cleanup_orphan_chrome() 会先把第一个实例的浏览器
    # 杀掉（chrome.pid 是同一个文件），再自己拉起一个，于是两边抢 3000/9222，标签页也会
    # 混在一起。用户双击图标两下就能撞上（2026-09-27 报「一次打开，打开了四个」）。
    # 已经在跑就把工具页开给他，然后直接退出；这个检查必须在 _cleanup_orphan_chrome 之前。
    if _port_in_use(3000) or not _port_bindable(3000):
        print('=' * 52)
        print('  工具已经在运行了，不用再开一个')
        print('  工具页: http://127.0.0.1:3000')
        print('  （这个窗口可以直接关掉）')
        print('=' * 52)
        try:
            webbrowser.open('http://127.0.0.1:3000')
        except Exception:
            pass
        return

    print('正在启动服务...')
    server_thread = threading.Thread(target=start_web_server, daemon=True)
    server_thread.start()
    time.sleep(2)

    # 还是撞上了（两下双击挨得极近、上面的探测都过了）：web 服务没起来就别再往下走 ——
    # 再往下就是 _cleanup_orphan_chrome()，它会按 chrome.pid 把另一个实例的浏览器杀掉。
    # 这里宁可自己退出，也不去动别人的浏览器。
    if _WEB_START_ERROR:
        print('=' * 52)
        print('  端口 3000 已被占用：工具应该已经在跑了（或上一次还没退干净）。')
        print('  这次不再继续启动（继续会杀掉另一个窗口的浏览器、抢标签页）。')
        print('  请先看工具页 http://127.0.0.1:3000；真要重开，先把旧窗口都关掉再启动。')
        print('=' * 52)
        return

    _cleanup_orphan_chrome()

    proc = None
    include_shopping = bool(license.LICENSE_STATE.get('ok'))
    if not include_shopping:
        print(f'[授权] 校验未通过（{license.LICENSE_STATE.get("reason")}），请在工具页输入激活码')

    try:
        print('正在启动内置浏览器...')
        proc = start_browser(include_shopping)
    except Exception as e:
        print(f'[警告] 内置浏览器启动失败: {e}，改用系统默认浏览器打开工具页')
        try:
            webbrowser.open('http://127.0.0.1:3000')
        except Exception:
            pass

    print('=' * 52)
    print('  拼多多一键上架 已启动')
    print('  工具页: http://127.0.0.1:3000')
    print('  首次使用请先输入激活码，再登录淘宝和拼多多商家后台')
    print('  关闭本窗口即停止服务')
    print('=' * 52)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print('正在关闭...')
    finally:
        if proc is not None:
            _kill_chrome_tree(proc.pid)
        _kill_shop_browsers()
        try:
            _chrome_pid_file().unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == '__main__':
    main()
