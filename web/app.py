"""
拼多多一键上架 - Web可视化界面
"""
import sys
from pathlib import Path

# 确保项目根目录在 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import license

# 管理端（总控台）共用发码逻辑：只有 launcher_admin.py 会把 license.ADMIN_MODE 置为 True，
# 用户端永远 False，所以 admin_core 里的私钥读取路径在用户端根本不会被调用。
import admin_core

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, Response
from pydantic import BaseModel
from typing import Dict, List, Optional
import uvicorn
import json
import os
import time
import asyncio
import re
import random         # _img_pace() 的随机间隔要用（风控专项，2026-09-28）
import threading      # _MIGRATED_LOCK / _SCRAPE_GUARD 要用；原来只在文件中间 import 过一次，
                     # 而锁在文件开头就建了 → import web.app 直接 NameError（2026-09-27）

if getattr(sys, 'frozen', False):
    # PyInstaller 打包后：静态文件在 _internal/web/，数据目录在 exe 同目录（保证可写）
    _APP_DIR = Path(sys.executable).resolve().parent
    BASE_DIR = Path(getattr(sys, '_MEIPASS', _APP_DIR)) / 'web'
    PROJECT_DIR = _APP_DIR
else:
    BASE_DIR = Path(__file__).resolve().parent  # web/
    PROJECT_DIR = BASE_DIR.parent  # project root

app = FastAPI(title="拼多多一键上架")
from local_security import LocalSessionBoundary
app.add_middleware(LocalSessionBoundary)

# 不开放跨域：界面是同源加载的（都用相对路径调接口），根本不需要 CORS。
# 原来写的是 allow_origins=["*"]，等于告诉浏览器「任何网站都能读这个本地服务的返回」——
# 用户拿普通浏览器随便打开哪个网页，那个网页就能调 /api/download-single-image 读本机文件
# （管理端机器上 私钥 keygen/private_key.pem 就在 exe 旁边）。宁可少一层方便，也不留这个口子。


@app.middleware("http")
async def _no_cache(request, call_next):
    """禁用缓存：本地工具改了前端代码立即生效，避免浏览器用旧 JS/HTML"""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

# 静态文件
_static_dir = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# data目录只暴露图片预览；不能把整个 data 目录挂成静态目录，
# 否则 data/license.json、usage.json、商品台账等本地文件会被浏览器直接读取。
_data_dir = PROJECT_DIR / "data"
_data_dir.mkdir(parents=True, exist_ok=True)
_image_dirs = []
for _subdir in ("main_images", "detail_images", "spec_images", "products", "ai_jobs"):
    _path = _data_dir / _subdir
    _path.mkdir(parents=True, exist_ok=True)
    _image_dirs.append(_path)

_PREVIEW_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

# 数据目录
DATA_DIR = PROJECT_DIR / "data"


@app.get("/", response_class=HTMLResponse)
async def home():
    """主页"""
    return FileResponse(str(BASE_DIR / "index.html"))


# ---------- 授权依赖 ----------
# 必须定义在**所有用到它的路由之前**：装饰器的 dependencies=[...] 是在 import 时求值的，
# 放到文件后半段（原来在「授权」那一节）会让 @app.get("/api/migrated-products", ...)
# 抛 NameError，整个 web.app 都 import 不进来（工具直接起不来）。
def require_license():
    """依赖：未授权时拒绝关键操作（纵深防御，防止绕过 UI 直接调接口）。

    每次请求重新校验一次（含分钟级到期），这样 2 分钟测试码在程序运行期间
    到点后也能立刻被拒，而不是沿用启动时缓存的授权状态。
    """
    if not license.check_now().get('ok'):
        raise HTTPException(status_code=403, detail=license.LICENSE_STATE.get('reason', 'unlicensed'))


@app.get("/data/{relative_path:path}", dependencies=[Depends(require_license)])
async def preview_image(relative_path: str):
    """只返回工具生成目录中的图片，拒绝读取许可证、台账和任意路径。"""
    candidate = (_data_dir / relative_path).resolve()
    if candidate.suffix.lower() not in _PREVIEW_EXTS or not candidate.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
    if not any(root == candidate or root in candidate.parents for root in _image_dirs):
        raise HTTPException(status_code=403, detail="只允许预览图片")
    return FileResponse(str(candidate))


# ---------- 搬家商品存储（抓取的商品可保存、复用、修改） ----------

class MigratedProduct(BaseModel):
    model_config = {"extra": "allow"}
    id: str = ""
    schema_version: int = 2
    title: str = ""
    source_url: str = ""
    brand: str = ""
    attrs: dict = {}
    description: str = ""
    category: str = ""
    specs: dict = {}
    spec_images: dict = {}
    main_images: List[str] = []
    detail_images: List[str] = []
    sku: list = []
    module_outputs: list = []
    review: dict = {}
    version_history: list = []
    metadata: dict = {}
    revision: int = 0
    job_id: str = ""
    created_at: str = ""
    updated_at: str = ""


MIGRATED_FILE = DATA_DIR / "migrated_products.json"
MIGRATED_IMG_DIR = DATA_DIR / "products"


def _load_migrated() -> list:
    if MIGRATED_FILE.exists():
        try:
            with open(MIGRATED_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


_MIGRATED_LOCK = threading.Lock()


def _save_migrated(items: list):
    """原子写 + 加锁。

    原来是直接 open(..., "w")：先截断再写，写到一半被杀/磁盘满就留下半个 JSON，
    而 _load_migrated 遇到坏文件返回 []，于是「整份商品列表」静默消失，下一次保存
    还把空列表覆盖回去（用户看到的就是「我保存的商品全没了」）。
    改成写临时文件再 os.replace（同盘 rename 是原子的）。
    """
    tmp = MIGRATED_FILE.with_suffix(".json.tmp")
    with _MIGRATED_LOCK:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, MIGRATED_FILE)


def _resolve_abs(p):
    """相对路径(如 data/products/..) → 绝对路径，供前端重新上架/预览；已绝对则原样返回。"""
    if not p:
        return p
    if os.path.isabs(p):
        return p
    return str((PROJECT_DIR / p).resolve())


def _upload_path(p):
    """把前端保存的 data/ 相对路径转换成绝对路径，避免服务从别的 cwd 启动时找不到图片。"""
    if not p or str(p).startswith(('http://', 'https://')):
        return p
    return _resolve_abs(str(p))


_IMG_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.avif'}


def _safe_image_path(p) -> bool:
    """这个路径能不能当图片读。

    下载接口原来对传进来的路径完全不设限，等于给了「读本机任意文件」的能力：
    {"path": "keygen/private_key.pem"} 也好、任意绝对路径也好，都能把文件读出去。
    两个下载接口本来就只下载图片，所以这里两条都卡住：
      1. 必须是工具自己目录（PROJECT_DIR）里的文件（挡 ../ 和任意绝对路径）
      2. 必须是图片扩展名（挡 keygen/private_key.pem、licenses.db、data/usage.json 这些）
    """
    try:
        fp = Path(p)
        if not fp.is_absolute():
            fp = PROJECT_DIR / p
        fp = fp.resolve()
        fp.relative_to(PROJECT_DIR)                       # 不在工具目录里 → 抛异常
        return fp.suffix.lower() in _IMG_EXTS and fp.is_file()
    except Exception:
        return False


def _to_rel(p):
    """绝对路径 → 相对 PROJECT_DIR 的路径（用 / 分隔），便于项目整体拷贝迁移。"""
    if not p:
        return p
    try:
        return os.path.relpath(str(p), str(PROJECT_DIR)).replace("\\", "/")
    except Exception:
        return str(p).replace("\\", "/")


# 规格预览图只对应「颜色/款式」那一维（尺码等后续维度没有图）。这一维**不能靠顺序猜**：
# 2026-09-27 BUNACC 那单，页面 DOM 里「尺码」排在「颜色分类」前面，老代码把第一个键当颜色维度
# → 颜色图被过滤光、主图兜底又塞给了 M/L/XL，上架时按颜色名找图一张都找不到（预览图列全空），
# 界面上还表现为「尺码那行有预览图、颜色那行空着」。
_COLOR_DIM_RE = re.compile(r'色|款式|花色|图案|花型|配色|色系')
_SIZE_DIM_RE = re.compile(r'尺码|尺寸|大小|码数')


def _norm_val(v) -> str:
    """规格值比对用：去掉所有空白（「黑色 」和「黑 色」当同一个）。"""
    return re.sub(r'\s+', '', str(v or ''))


def _pick_image_dim(spec_dict: dict, spec_images: dict) -> str:
    """这批规格预览图属于哪个规格维度（正常就是颜色/款式），认不出返回 ''。

    依据是数据本身：**哪个维度的值带图，预览图就是哪个维度的**；同名时优先名字像颜色的
    （颜色/款式/花色…），并且不考虑尺码维度 —— 尺码值上带的图是占位图，混进来会按序上传错位。
    """
    if not spec_dict or not spec_images:
        return ''
    have = {_norm_val(k) for k in spec_images}
    counts = {}
    for dim, vals in spec_dict.items():
        n = sum(1 for v in (vals or []) if _norm_val(v) in have)
        if n > 0 and not _SIZE_DIM_RE.search(str(dim)):
            counts[dim] = n
    if not counts:
        return ''
    color_like = {d: n for d, n in counts.items() if _COLOR_DIM_RE.search(str(d))}
    pool = color_like or counts
    return max(pool.items(), key=lambda kv: kv[1])[0]


def _name_dim(spec_dict: dict) -> str:
    """只按名字挑「颜色/款式」那一维（一张图都没抓到、没有数据可依据时用）。"""
    dims = [str(d) for d in (spec_dict or {}).keys()]
    for d in dims:
        if _COLOR_DIM_RE.search(d) and not _SIZE_DIM_RE.search(d):
            return d
    for d in dims:
        if not _SIZE_DIM_RE.search(d):
            return d
    return dims[0] if dims else ''


def _drop_placeholder_spec_images(spec_images: dict):
    """丢掉淘宝的**懒加载占位图**（2×2 的 tps-2-2.png），返回 (留下的图, 说明或 None)。

    ⚠ 这里只认「一眼假」的占位图，**不认「好几个颜色共用一张照片」**：
      · 单个规格值只有一张图 —— 单色商品本来就这样，不能扔（2026-09-27 用户那单栽在这儿，
        真图被当成占位图扔掉、换成主图，界面上预览图全成了主图）；
      · 几个颜色共用一张照片 —— 那是卖家自己没给每个颜色单独设图，淘宝照实返回，
        工具没拿错（见 _describe_same_pictures）。换成主图才是错上加错。
    """
    if not spec_images:
        return spec_images, None
    if len(set(spec_images.values())) <= 1 and len(spec_images) > 1:
        # 文件名是规格值名的哈希，正常情况下值名不同、路径就不同 —— 走到这儿说明上游出了问题
        return {}, f"{len(spec_images)} 个规格值指向同一个文件，判断为占位图，改用主图代替"
    keep, tiny = {}, 0
    for name, path in spec_images.items():
        if _is_tiny_picture(path):
            tiny += 1
            continue
        keep[name] = path
    if tiny:
        return keep, f"{tiny} 张是淘宝那种 2×2 的懒加载占位图（不是真图），已丢掉"
    return keep, None


_PIC_FP_SIZE = 24


def _pic_fingerprint(path, size: int = _PIC_FP_SIZE):
    """图片的「画面指纹」：缩成 24×24 的像素值，用来判断两张图是不是同一个画面。

    为什么不比文件字节：同一张照片经过 CDN 转码（webp → jpeg 重压、加水印参数）后字节
    完全不同，但画面还是那张 —— 2026-09-27 用户那单，淘宝给「白色」的颜色图和商品主图
    是两个不同的文件（md5 不同、大小 457KB vs 177KB），逐像素比下来却是同一张照片。
    读不了（文件坏了/不是图）返回 None。
    """
    try:
        from PIL import Image
        im = Image.open(path)
        im.draft('RGB', (size, size))   # JPEG 可以按比例粗解码，快很多
        return im.convert('RGB').resize((size, size)).tobytes()
    except Exception:
        return None


def _same_picture(fp_a, fp_b, tol: float = 6.0) -> bool:
    """两个画面指纹是不是同一张照片（tol 容忍转码带来的轻微差别，0~255 的平均差）。"""
    if not fp_a or not fp_b or len(fp_a) != len(fp_b):
        return False
    return sum(abs(x - y) for x, y in zip(fp_a, fp_b)) / len(fp_a) <= tol


def _is_tiny_picture(path, max_px: int = 8) -> bool:
    """是不是淘宝那个 2×2 的懒加载占位图（真图不可能这么小）。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
        return w <= max_px and h <= max_px
    except Exception:
        return False


def _describe_same_pictures(spec_images: dict, main_images=None, fingerprint=None) -> str:
    """预览图看着「不对」时，把真实情况说清楚 —— 是淘宝给的图就这样，不是工具挑错了。

    2026-09-27 用户报「预览图传的还是主图」，两种真实情况都能对上：
      · 单色那单：淘宝给「白色」的颜色图**就是主图那张照片**（两个不同文件、逐像素是同一张）；
      · 6 色那单：淘宝对每个颜色值返回同一个图片地址，6 个文件字节完全相同 —— 上架后
        预览图那列 6 行一模一样，看着也像主图。
    卖家没给每个颜色单独设图，工具无从变出别的图 —— 所以照传，但要说出来。
    返回空串表示各值都有各自的图、且都不跟主图撞脸（正常情况）。
    """
    fp = fingerprint or _pic_fingerprint
    groups = []          # [(画面指纹, [规格值名, ...])]
    for name, path in (spec_images or {}).items():
        key = fp(path)
        if key is None:
            continue
        for other, names in groups:
            if _same_picture(other, key):
                names.append(name)
                break
        else:
            groups.append((key, [name]))
    notes = []
    shared = [(k, ns) for k, ns in groups if len(ns) > 1]
    if shared:
        _key, names = shared[0]
        notes.append(f"{len(names)} 个规格值在淘宝上是同一张图（卖家没给每个颜色单独设图），"
                     f"预览图这列会是同一张：「{names[0]}」等")
    # 预览图跟主图撞脸：说清楚，省得用户以为是工具挑错了（用户 09-27 就是这么以为的）
    main_fps = [(_m, fp(p)) for _m, p in enumerate(main_images or [], start=1)]
    for key, names in groups:
        for idx, main_fp in main_fps:
            if _same_picture(key, main_fp):
                notes.append(f"「{names[0]}」这张预览图跟主图第 {idx} 张是同一个画面"
                             f"（淘宝给的颜色图就长这样，不是工具挑错了图）")
                break
    return '；'.join(notes)


def _store_images(entries, target_dir: Path, prefix: str):
    """把图片（绝对路径或 http URL）复制/下载进 target_dir，返回相对路径列表（保序）。

    ⚠ 本地路径必须过 `_safe_image_path`：这个接口（/api/migrated-products）**没有鉴权**，
    而它写出去的每个文件都能从无鉴权的 /data 静态目录直接下载 —— 不过滤的话，
    传个 `keygen/private_key.pem` 进来就能把签名私钥读走（有了私钥可以伪造任意激活码），
    或者把 browser_data 里的 Cookies 读走。以前只卡了两个下载接口，这里是绕过去的路。
    """
    import shutil
    target_dir.mkdir(parents=True, exist_ok=True)
    out = []
    blocked = 0
    for i, src in enumerate(entries):
        if not src:
            continue
        src = str(src)
        if not src.startswith("http") and not _safe_image_path(src):
            blocked += 1
            continue
        ext = os.path.splitext(src.split("?")[0])[1].lower()
        if ext not in (".jpg", ".jpeg", ".png"):
            ext = ".jpg"
        dst = target_dir / f"{prefix}_{i + 1}{ext}"
        try:
            if src.startswith("http"):
                _download_image(src, str(dst))
            else:
                sp = Path(_resolve_abs(src))
                if not sp.exists():
                    continue
                # 本地老图也可能是「名叫 .jpg 的 webp」（以前下载时留下的），复制时一并纠正
                _write_image_consistent(sp.read_bytes(), dst)
            if dst.exists() and dst.stat().st_size > 0:
                out.append(_to_rel(str(dst)))
        except Exception:
            continue
    if blocked:
        print(f'  [图片] 跳过了 {blocked} 个不在工具目录里的路径（只允许复制工具自己抓下来的图）')
    return out



def _draft_store():
    from local_drafts import import_legacy
    from task_store import TaskStore, StoreError
    store = TaskStore(PROJECT_DIR)
    try:
        import_legacy(PROJECT_DIR, store, 'local')
    except StoreError as exc:
        raise HTTPException(409, str(exc)) from exc
    return store


@app.get("/api/migrated-products", dependencies=[Depends(require_license)])
def get_migrated_products():
    items = _draft_store().list_drafts('local')
    for item in items:
        for field in ('main_images', 'detail_images'):
            item[field] = [_resolve_abs(p) for p in item.get(field, [])]
        item['spec_images'] = {k: _resolve_abs(v) for k, v in item.get('spec_images', {}).items()}
    return items


@app.post("/api/migrated-products", dependencies=[Depends(require_license)])
def save_migrated_product(product: MigratedProduct):
    from local_drafts import save_package
    from task_store import StoreError
    from safe_http import OutboundError
    try:
        record = save_package(PROJECT_DIR, _draft_store(), 'local', product.model_dump(exclude_unset=True))
    except (StoreError, ValueError, OutboundError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {'success': True, 'id': record['id'], 'revision': record['revision'], 'product_package': record, 'message': '完整商品草稿已保存'}


@app.delete("/api/migrated-products/{pid}", dependencies=[Depends(require_license)])
def delete_migrated_product(pid: str):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', pid):
        raise HTTPException(422, '商品编号无效')
    store = _draft_store()
    if not store.delete_draft('local', pid):
        raise HTTPException(404, '商品不存在')
    # Old JSON is only a one-time import source. Keep a tombstone so a deleted
    # legacy draft is not resurrected on the next refresh.
    if MIGRATED_FILE.exists():
        with _MIGRATED_LOCK:
            old = _load_migrated()
            temporary = MIGRATED_FILE.with_suffix('.json.tmp')
            temporary.write_text(json.dumps([x for x in old if x.get('id') != pid], ensure_ascii=False), 'utf-8')
            os.replace(temporary, MIGRATED_FILE)
    return {'success': True, 'message': '草稿已删除；共享图片将在保留期后清理'}


@app.get("/api/config")
async def get_config():
    """获取配置"""
    return {
        "platform": "pdd",
        "price_margin": 1.3,
        "admin_mode": bool(license.ADMIN_MODE),  # 管理端为 True：前端据此显示「管理」页签、隐藏授权门禁
    }


# ---------- 授权 ----------
# require_license 定义在上面（所有依赖它的路由之前），这里只剩 require_admin
def require_admin():
    """依赖：仅管理端可用（launcher_admin.py 启动时置 license.ADMIN_MODE=True）。

    用户端不会置位，且用户端根本不含私钥，即便有人手动调这些接口，也会被 404 拒之门外。
    """
    if not license.ADMIN_MODE:
        raise HTTPException(status_code=404, detail='not found')


class ActivateLicenseRequest(BaseModel):
    code: str


@app.get("/api/license/status")
async def license_status():
    fresh = license.check_now()  # 实时重验（含分钟级到期），让界面上的到期/倒计时与实际拦截保持一致
    usage = license.get_usage()
    return {
        "state": fresh.get("reason") or license.LICENSE_STATE.get("reason", "unlicensed"),
        "ok": bool(fresh.get("ok")),
        "expiry": license.LICENSE_STATE.get("expiry"),
        "code": license.LICENSE_STATE.get("code", ""),
        "expire_at": license.LICENSE_STATE.get("expire_at"),
        "minutes_left": license.minutes_left(),
        "machine_id": license.LICENSE_STATE.get("machine_id") or license.get_machine_id(),
        "days_left": license.days_left(),
        "used": usage["used"],
        "quota": usage["quota"],
        "admin_mode": bool(license.ADMIN_MODE),
        # 授权有效但码是旧版（不带资产种子）→ 界面要提示「找出租方换一张新版激活码」
        "upgrade_needed": license.upgrade_needed(),
        # 钥匙有了、但出租方发来的那个资产包还没导入 → 界面要提示「导入资产包」。
        # 一码一份：光有码解不开任何东西，必须配上这台机器专属的那个 assets_<验证码>.zip。
        "pack_ready": _pack_ready(),
    }


def _pack_ready() -> bool:
    try:
        import asset_store
        return bool(asset_store.pack_ready())
    except Exception:
        return False


@app.post("/api/license/import-pack")
async def license_import_pack(file: UploadFile = File(...)):
    """导入出租方发来的资产包（assets_<验证码>.zip）。

    为什么要这一步：用户端包里**不带**可解密的资产 —— 每个租户的密文是单独一份、
    只用他那张激活码里的种子打得开（见 asset_store.py 开头）。所以激活之后还要把
    出租方给的那个包装进来，工具才拿得到干活的 JS。
    """
    import asset_store
    mid = license.LICENSE_STATE.get("machine_id") or ""
    if not mid:
        return {"success": False, "message": "请先激活（输入出租方发回的激活码），再导入资产包"}
    try:
        from safe_http import read_upload
        data = await read_upload(file, 8 * 1024 * 1024)
    except Exception as e:
        return {"success": False, "message": f"读取文件失败：{e}"}
    if not data:
        return {"success": False, "message": "这个文件是空的，重新下载一次出租方发的资产包"}
    try:
        n = asset_store.import_pack(data, mid)
    except asset_store.PackInvalid as e:
        return {"success": False, "message": str(e)}
    except Exception as e:
        return {"success": False, "message": f"导入失败：{e}"}
    return {"success": True, "count": n,
            "message": f"资产包导入成功（{n} 份）。现在可以用上架功能了。"}


@app.post("/api/license/reset-usage", dependencies=[Depends(require_admin)])
async def license_reset_usage():
    """清零本月已上架计数（测试额度用）。只有管理端能用，客户端调不动。"""
    license.reset_usage()
    usage = license.get_usage()
    return {"success": True, "used": usage["used"], "quota": usage["quota"]}


@app.post("/api/license/activate")
async def license_activate(req: ActivateLicenseRequest):
    return license.activate_and_update(req.code)


# ---------- 管理端（总控台）发码后台 ----------
class AdminGenRequest(BaseModel):
    machine_id: str
    months: int = 1
    days: int = 0      # 零头天数：半个月 = 0月15天，一个半月 = 1月15天
    quota: int = 1000
    minutes: int = 0
    note: str = ""
    amount: float = 0.0


class AdminExtendRequest(BaseModel):
    id: int
    months: int = 1
    days: int = 0
    amount: float = 0.0
    note: str = ""


class AdminUpdateRequest(BaseModel):
    id: int
    note: Optional[str] = None
    amount: Optional[float] = None
    status: Optional[str] = None


class AdminDecodeRequest(BaseModel):
    code: str


def _admin_ok(**kw):
    return {"success": True, **kw}


@app.get("/api/admin/stats", dependencies=[Depends(require_admin)])
async def admin_stats():
    return admin_core.stats()


@app.get("/api/admin/list", dependencies=[Depends(require_admin)])
async def admin_list(status: str = None, q: str = None):
    return {"items": admin_core.list_codes(status=status, q=q)}


@app.get("/api/admin/export", dependencies=[Depends(require_admin)])
async def admin_export():
    """导出全部激活码为 CSV（含 BOM，Excel 打开不乱码）。"""
    csv_text = admin_core.export_csv()
    from fastapi.responses import Response
    return Response(
        content=csv_text.encode('utf-8-sig'),
        media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename="licenses.csv"'}
    )


@app.post("/api/admin/gen", dependencies=[Depends(require_admin)])
async def admin_gen(req: AdminGenRequest):
    try:
        rec = admin_core.gen_code(
            machine_id=req.machine_id,
            months=req.months,
            days=req.days,
            quota=req.quota,
            minutes=req.minutes,
            note=req.note,
            amount=req.amount,
        )
        return _admin_ok(record=rec)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/admin/decode", dependencies=[Depends(require_admin)])
async def admin_decode(req: AdminDecodeRequest):
    try:
        return _admin_ok(info=admin_core.decode_code(req.code))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/admin/extend", dependencies=[Depends(require_admin)])
async def admin_extend(req: AdminExtendRequest):
    try:
        rec = admin_core.extend_code(req.id, months=req.months, days=req.days,
                                     amount=req.amount, note=req.note)
        return _admin_ok(record=rec)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


class AdminReissueRequest(BaseModel):
    id: int
    note: str = ""


@app.get("/api/admin/pack/{rid}", dependencies=[Depends(require_admin)])
async def admin_pack(rid: int):
    """下载某条记录的资产包（连同激活码一起发给租户的那个 zip）。

    种子在账本里，所以租户弄丢了随时能重出一份。老码（没种子）得先「换发」。
    """
    try:
        info = admin_core.regen_pack(rid)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return FileResponse(info['pack'], filename=info['pack_name'], media_type='application/zip')


@app.post("/api/admin/reissue", dependencies=[Depends(require_admin)])
async def admin_reissue(req: AdminReissueRequest):
    """换发（老码升级）：同一台机器、同一个到期日，重签一张带资产种子的码（不加时长）。"""
    try:
        rec = admin_core.reissue_code(req.id, note=req.note)
        return _admin_ok(record=rec)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/admin/update", dependencies=[Depends(require_admin)])
async def admin_update(req: AdminUpdateRequest):
    try:
        rec = admin_core.update_record(req.id, note=req.note, amount=req.amount, status=req.status)
        return _admin_ok(record=rec)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/admin/delete", dependencies=[Depends(require_admin)])
async def admin_delete(req: AdminUpdateRequest):
    try:
        ok = admin_core.delete_record(req.id)
        return _admin_ok(deleted=ok)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


class DownloadImagesRequest(BaseModel):
    images: List[str] = []


@app.post("/api/download-images", dependencies=[Depends(require_license)])
async def download_images(req: DownloadImagesRequest):
    """把商品搬家里抓到的图片（主图/详情图）打包成 zip 下载。"""
    import zipfile
    import io

    buf = io.BytesIO()
    added = 0
    blocked = 0
    total_bytes = 0
    if len(req.images) > 150:
        raise HTTPException(422, '一次最多下载150张图片')
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for p in req.images:
            if not _safe_image_path(p):      # 只打包工具自己目录里的图片，别被当成读文件的通道
                blocked += 1
                continue
            fp = Path(p)
            if not fp.is_absolute():
                fp = PROJECT_DIR / p
            if not fp.is_file():
                continue
            total_bytes += fp.stat().st_size
            if total_bytes > 128 * 1024 * 1024:
                raise HTTPException(413, '打包总量超过128MB，请分批下载')
            try:
                rel = fp.relative_to(_data_dir)
            except Exception:
                rel = fp.name
            zf.write(fp, arcname=str(rel).replace('\\', '/'))
            added += 1
    if added == 0:
        if blocked:
            raise HTTPException(status_code=403, detail='这些图片不在工具目录里，拒绝打包（只允许下载工具自己抓下来的图片）')
        raise HTTPException(status_code=404, detail='没有可下载的图片')
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type='application/zip',
        headers={'Content-Disposition': 'attachment; filename="product_images.zip"'}
    )


class SingleImageRequest(BaseModel):
    path: str = ""  # 本地路径 / data 相对路径 / 远程URL


@app.post("/api/download-single-image", dependencies=[Depends(require_license)])
def download_single_image(req: SingleImageRequest):
    # 同样是同步 def：远程图要 urlopen 下载（最长 30 秒），跑在事件循环上会把整个服务冻住
    """下载单张图片（主图/详情图/规格预览图通用）：本地路径直接读，远程URL带Referer下载。"""
    import urllib.request as _urlreq

    p = (req.path or "").strip()
    if not p:
        raise HTTPException(status_code=400, detail="缺少图片路径")

    if p.startswith("http://") or p.startswith("https://"):
        # 远程图片：带 Referer/UA 绕过淘宝/阿里防盗链
        try:
            from safe_http import download_bytes
            data = download_bytes(p, timeout=30, headers={
                "Referer": "https://item.taobao.com/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            })
            filename = "product_image.jpg"
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"远程图片下载失败: {e}")
    else:
        # 只允许读工具自己目录里的文件：以前这里对路径不设限，
        # 「../keygen/private_key.pem」这种也能被下载走
        if not _safe_image_path(p):
            raise HTTPException(status_code=403,
                                detail="只允许下载工具自己目录里的图片")
        fp = Path(p)
        if not fp.is_absolute():
            s = p.replace("\\", "/")
            if s.startswith("/data/"):
                fp = _data_dir / s[len("/data/"):]
            elif s.startswith("data/"):
                fp = PROJECT_DIR / s
            else:
                fp = PROJECT_DIR / p
        if not fp.is_file():
            raise HTTPException(status_code=404, detail="图片文件不存在")
        data = fp.read_bytes()
        filename = fp.name

    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


class OneClickRequest(BaseModel):
    url: str
    price: float = 49.9


class ScrapeTaobaoRequest(BaseModel):
    url: str


class StartListingRequest(BaseModel):
    url: str
    title: str = ""
    taobao_url: str = ""  # 淘宝商品URL
    specs: dict = {}  # {规格类型名: [规格值列表]}，如 {"颜色分类": ["红色","蓝色"], "尺码": ["S","M"]}
    skus: list = []  # [{name, price, single_price, stock, enabled}]
    main_images: list = []  # 主图URL列表
    detail_images: list = []  # 详情图路径列表
    spec_images: dict = {}  # {spec_value_name: local_path}
    brand: str = ""  # 品牌名称
    attrs: dict = {}  # 商品属性 {属性名: 属性值}
    description: str = ""  # AI/人工整理的商品描述，供后续详情编辑使用
    category: str = ""  # AI 类目候选，仅作提示，不自动替用户确认类目
    # 目标店铺：CDP 调试端口列表（多店铺上架）。None / 空 = 沿用单店老行为（自动挑第一家
    # 停在发布页的店），这样前端没跟上、或者有人直接打接口，也不会突然什么都不做。
    shops: Optional[List[int]] = None
    shop_identities: dict = {}
    client_request_id: str = ""
    # 每家店用「最近使用的分类」里的第几个（前端勾选弹窗里的下拉）。{端口: 下标}。
    # 缺省 / 没这家店的端口 → 一律用 0（第一个），跟以前一样。
    # 只在**这家店正停在分类页**时才有意义；停在发布页时压根用不上。
    shop_cats: Optional[Dict[str, int]] = None


class OpenPageRequest(BaseModel):
    target: str  # "taobao" 或 "pdd"


# 后台任务存储
_bg_tasks = {}
# 同一时间只允许一轮上架（空 = 没有在跑）。两轮会抢同一个拼多多商品页，把规格和图片写乱。
_LISTING_GUARD = threading.Lock()
_LISTING_ACTIVE = {}

# 抓取同理：两轮会抢同一个淘宝标签页，还把图片写进同一批固定文件名
_SCRAPE_GUARD = threading.Lock()
_SCRAPE_ACTIVE = {}

# AI 生图也只允许一轮：同一目录和同一浏览器页不能被两份商品结果互相覆盖。
_AI_GUARD = threading.Lock()
_AI_ACTIVE = {}


from ai_api import install_ai_routes
_ai_runner = install_ai_routes(app, PROJECT_DIR, require_license, _bg_tasks)


@app.get('/api/health')
def workbench_health():
    from ai_workflow import skill_template_version
    try:
        skill = skill_template_version()
        database_ok = _ai_runner.store.health()
        manifest_path = PROJECT_DIR / 'release_manifest.json'
        release = json.loads(manifest_path.read_text('utf-8')) if manifest_path.exists() else {}
        return {'ok': database_ok, 'release_id': release.get('release_id', 'development'), 'schema_version': 2,
                'skill': skill, 'mode': 'local', 'active_task': _ai_runner.busy}
    except Exception:
        raise HTTPException(503, '本地运行文件或存储检查失败，请恢复同版本交付包')

@app.post("/api/scrape-taobao", dependencies=[Depends(require_license)])
async def scrape_taobao(req: ScrapeTaobaoRequest):
    """通过CDP抓取淘宝商品信息（标题、规格、详情图）"""
    import uuid
    task_id = str(uuid.uuid4())[:8]

    # 同时只允许一轮抓取：两轮会连同一个淘宝标签页、各自 Page.navigate，
    # 而且都往 data/main_images/main_1.jpg 这种固定文件名写 —— 先跑完那轮返回给前端的
    # 本地图片路径，内容已经是后一轮的图（上架时就把别的商品的图传上去了）。
    # 前端现在也会掐掉旧轮询，但连点两下仍会同时发出两个请求，闸得在后端。
    with _SCRAPE_GUARD:
        if _SCRAPE_ACTIVE:
            _st = _bg_tasks.get(_SCRAPE_ACTIVE.get("task_id") or "", {}) or {}
            _p = _st.get("progress") or "准备执行..."
            _s = time.time() - float(_SCRAPE_ACTIVE.get("started") or time.time())
            raise HTTPException(
                status_code=409,
                detail=f"上一轮抓取还在跑（已经 {_s:.0f} 秒，正在「{_p}」）。"
                       "两轮同时抓会把图片写串（上架时可能传成另一个商品的图），等它跑完再点")
        _SCRAPE_ACTIVE.update({"task_id": task_id, "started": time.time()})

    def run_scrape():
        import asyncio
        import traceback
        _bg_tasks[task_id] = {"status": "running", "progress": "正在连接淘宝页面..."}
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(_do_scrape_taobao(req.url))
            loop.close()
            _bg_tasks[task_id] = {"status": "done", **result}
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[SCRAPE ERROR] {tb}")
            _bg_tasks[task_id] = {"status": "error", "message": str(e)}
        finally:
            with _SCRAPE_GUARD:
                _SCRAPE_ACTIVE.clear()

    thread = threading.Thread(target=run_scrape, daemon=True)
    thread.start()
    return {"task_id": task_id}


async def _ensure_browser() -> bool:
    """确保内置浏览器在运行：若 9222 不通则自动重启浏览器。返回 True 表示浏览器已就绪。"""
    import urllib.request

    def _alive():
        try:
            urllib.request.urlopen('http://127.0.0.1:9222/json/version', timeout=2)
            return True
        except Exception:
            return False

    if _alive():
        return True

    # 浏览器已退出 → 自动重启（放线程里避免阻塞事件循环）
    def _start():
        import launcher
        launcher.start_browser()

    try:
        await asyncio.to_thread(_start)
    except Exception:
        return False

    for _ in range(30):
        await asyncio.sleep(0.5)
        if await asyncio.to_thread(_alive):
            return True
    return False


@app.post("/api/open-page", dependencies=[Depends(require_license)])
async def open_page(req: OpenPageRequest):
    """打开内置浏览器里的淘宝 / 拼多多上架界面，并把窗口置顶"""
    import websockets
    import urllib.request
    from urllib.parse import quote

    if req.target == 'taobao':
        url = 'https://www.taobao.com'

        def _match(u):
            return 'taobao.com' in u or 'tmall.com' in u
    elif req.target == 'pdd':
        url = 'https://mms.pinduoduo.com/goods/category'

        def _match(u):
            return 'pinduoduo.com' in u or 'yangkeduo.com' in u
    elif req.target == 'pdd-login':
        # 打开新店铺：拉起一个独立浏览器窗口（独立 profile + 独立调试端口），
        # 导航到商家后台登录页，用于登录另一家店铺（彻底隔离，互不影响）
        import launcher
        try:
            # open_new_shop 是同步的：拉起 Chromium 后要轮询等端口，最长阻塞 20 秒，
            # 里面还有一把 threading.Lock（连点两下，第二个请求会卡在锁上）。
            # 直接调用会把事件循环冻住 —— 正在跑的上架任务、前端 2 秒一次的进度轮询全停。
            port = await asyncio.to_thread(launcher.open_new_shop)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f'打开新店铺窗口失败：{e}')
        return {
            "ok": True,
            "port": port,
            "message": f"已打开新店铺窗口（端口 {port}），请在新窗口里登录这家店。想登录更多店就再点一次本按钮。",
        }
    else:
        raise HTTPException(status_code=400, detail='未知目标')

    # 浏览器可能已退出 → 先确保浏览器在运行
    if not await _ensure_browser():
        raise HTTPException(status_code=503, detail='内置浏览器未启动，请关闭程序重新打开')

    # 找已有标签页
    try:
        tabs = json.loads(urllib.request.urlopen('http://127.0.0.1:9222/json', timeout=3).read())
    except Exception:
        raise HTTPException(status_code=503, detail='内置浏览器未启动')

    pages = [t for t in tabs if t.get('type') == 'page']
    tab = next((t for t in pages if _match(t.get('url', ''))), None)

    # 没有对应标签页 → 新开一个
    if tab is None:
        try:
            req2 = urllib.request.Request(
                f'http://127.0.0.1:9222/json/new?{quote(url)}', method='PUT'
            )
            tab = json.loads(urllib.request.urlopen(req2, timeout=5).read())
        except Exception:
            raise HTTPException(status_code=503, detail='打开标签页失败')

    from one_click_list import CDPClient
    try:
        ws = await websockets.connect(tab['webSocketDebuggerUrl'], max_size=50*1024*1024)
        cdp = CDPClient(ws)
        await cdp.start()
        await cdp.call('Page.bringToFront')   # 窗口置顶
        await cdp.call('Page.navigate', {'url': url})  # 导航到目标界面
        await asyncio.sleep(0.5)
        await ws.close()
    except Exception:
        # 标签页已打开即可，置顶/导航失败不报错
        pass

    return {"ok": True, "url": url}


def _sniff_image(data: bytes) -> str:
    """按字节头判断图片真实格式 —— 不看文件名。

    （阿里的 CDN 会随时改主意：同一个 URL，头里说要不要 webp、UA 是谁，回来的格式都可能不一样）
    """
    if data[:3] == b'\xff\xd8\xff':
        return 'jpeg'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if data[:2] == b'BM':
        return 'bmp'
    return 'other'


def _convert_image(data: bytes, fmt: str = 'JPEG') -> bytes:
    """把图片字节转成指定格式；转不动就原样返回（宁可留个格式不对的图，也别把图弄丢）。"""
    try:
        import io as _io

        from PIL import Image
        im = Image.open(_io.BytesIO(data))
        im.load()
        if fmt == 'JPEG' and im.mode not in ('RGB', 'L'):
            im = im.convert('RGB')
        out = _io.BytesIO()
        if fmt == 'JPEG':
            im.save(out, 'JPEG', quality=92, optimize=True)
        else:
            im.save(out, fmt)
        return out.getvalue()
    except Exception as e:
        print(f'  [图片] 转 {fmt} 失败（{e}），按原字节保存')
        return data


def _write_image_consistent(data: bytes, path) -> None:
    """把图片字节写进 path，并保证「后缀名和真实格式对得上」。

    后缀是 jpg/jpeg 而字节不是 JPEG → 转成 JPEG；后缀是 png 而字节是别的 → 转成 PNG；
    格式本来就对、或认不出来的（可能是张错误页）→ 原样写。
    """
    from ai_workflow import _open_image, MAX_UPLOAD_BYTES
    if len(data) > 24 * 1024 * 1024:
        raise ValueError('图片超过24MB限制')
    import io
    from PIL import Image
    with Image.open(io.BytesIO(data)) as checked_image:
        if checked_image.width * checked_image.height > 40_000_000:
            raise ValueError('图片像素过大')
        checked_image.verify()
    want = os.path.splitext(str(path))[1].lower()
    kind = _sniff_image(data)
    if want in ('.jpg', '.jpeg') and kind != 'jpeg':
        data = _convert_image(data, 'JPEG')
    elif want == '.png' and kind not in ('png', 'other'):
        data = _convert_image(data, 'PNG')
    with open(path, 'wb') as f:
        f.write(data)


# ==================== 抓取的「节奏」（2026-09-28 风控专项）====================
# 为什么有这几个数：抓一个商品，页面上只是「导航一次」，后台却要拉 ~90 张图
# （主图 5 + 详情图最多 80 + 规格图若干个）。原来 8 线程一口气打完 —— 对一个 CDN
# 来说，这比「导航一次」扎眼得多，是最容易触发风控的流量特征。
# 淘宝那边已经真的被限流过（详情页挂「访问异常提示」，要等几十分钟自己恢复）。
# 所以：并发降下来、每张之间随机停一下，把「一阵猛冲」变成「细水长流」。
# 间隔**必须随机** —— 固定间隔（比如正好 0.3 秒一张）本身就是很好认的机器特征。
_IMG_WORKERS = 3                 # 原来 8
_IMG_GAP = (0.2, 0.5)            # 每张图之间随机停 0.2~0.5 秒
MAX_DETAIL_IMAGES = 80           # 2026-09-28 用户先调到 60，又调回 80（详情图是商品信息最全的一段，
                                 # 砍了下限就丢内容）。注意：**下载多少张，上架时就要一张一张传多少张**，
                                 # 传图那步有总时间预算（DETAIL_TOTAL_BUDGET_SECS），张数一多容易到点收工。


def _img_pace() -> None:
    """下载每张图之前随机停一下（在下载线程里调用，不阻塞事件循环）。"""
    time.sleep(random.uniform(*_IMG_GAP))


def _download_image(url, path, referer: str = 'https://item.taobao.com/'):
    """带headers下载图片，绕过反爬 + 清理阿里CDN裁剪后缀。

    下载完必须把字节转成真 JPEG：我们一直把文件存成 .jpg，但过去请求头里写了
    `Accept: image/webp`，阿里 CDN 就真的把 webp 发回来 —— 于是 data/main_images
    里大半是「名叫 .jpg 的 webp」，还有 746 字节的 PNG 也顶着 .jpg 的名字。拼多多
    的上传框拿到这种表里不一的文件会拒收/传成一团（用户看到的就是「图传不上去」）。
    两头堵：请求头不再要 webp；拿回来凡是字节头不是 JPEG 的一律转一遍。

    referer：图片站会看它防盗链。淘宝图要带着商品页的 Referer；拼多多的图不一样，
    所以做成参数（写死淘宝的值去拉拼多多的图会被拒）。
    """
    import re
    from urllib.request import Request
    from urllib.request import urlopen as _urlopen
    # 清理阿里CDN各种裁剪后缀
    url = re.sub(r'\.(jpg|jpeg|png)_\.webp$', r'.\1', url)
    url = re.sub(r'\.(jpg|jpeg|png)_q\d+\.jpg_$', r'.\1', url)
    url = re.sub(r'\.(jpg|jpeg|png)_\d+x\d+q\d+\.jpg_$', r'.\1', url)
    url = re.sub(r'\.(jpg|jpeg|png)_\d+x\d+\.jpg_$', r'.\1', url)
    url = re.sub(r'\.(jpg|jpeg|png)_$', r'.\1', url)
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': referer,
        # 不要写 image/webp —— 写了它就给 webp，存下来还是 .jpg，传给拼多多就废了
        'Accept': 'image/jpeg,image/png;q=0.9,image/*;q=0.8'
    }
    from safe_http import download_bytes
    data = download_bytes(url, timeout=30, headers=headers)
    _write_image_consistent(data, path)


def _download_images_parallel(urls, target_dir: Path, prefix: str, min_size: int = 1024,
                              referer: str = 'https://item.taobao.com/') -> list:
    """低并发、带随机间隔地下载图片到 target_dir，返回按原始顺序的本地路径列表（失败跳过）。

    慢一点是故意的：这里是整轮抓取里请求量最大的一段（见上面 _IMG_WORKERS 的说明）。
    下限并发 2，上限 6 —— 别有人把这几个数调回去。
    """
    from concurrent.futures import ThreadPoolExecutor
    target_dir.mkdir(parents=True, exist_ok=True)
    workers = max(2, min(6, _IMG_WORKERS))

    def _one(pair):
        i, url = pair
        dst = target_dir / f"{prefix}_{i+1}.jpg"
        try:
            _img_pace()
            _download_image(url, str(dst), referer)
            if dst.exists() and dst.stat().st_size > min_size:
                return i, str(dst)
        except Exception:
            pass
        return i, None

    results = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, path in ex.map(_one, list(enumerate(urls))):
            if path:
                results[i] = path
    return [results[i] for i in sorted(results)]


async def _do_scrape_taobao(url: str) -> dict:
    """通过CDP抓取淘宝商品（输入链接后自动导航到该商品页）"""
    import websockets
    import urllib.request
    from urllib.parse import quote

    # 抓取这一段也计时：用户说「还是有点慢」，得知道是抓取慢还是上架慢（2026-09-27）
    ph = _Phase()

    # 浏览器可能已退出 → 先确保浏览器在运行
    if not await _ensure_browser():
        raise Exception("内置浏览器未启动，请关闭程序重新打开")
    ph.done('确保浏览器在跑')

    def _get_pages():
        tabs = json.loads(urllib.request.urlopen('http://127.0.0.1:9222/json', timeout=10).read())
        # 只取页面标签，排除 service worker 等后台目标
        return [t for t in tabs if t.get('type') == 'page']

    def _find_taobao(tabs):
        for t in tabs:
            tab_url = t.get('url', '')
            if 'taobao.com' in tab_url or 'tmall.com' in tab_url:
                return t
        return None

    def _item_id(u):
        """从商品链接里取商品 id，用来判断「这个商品页是不是已经开着」。"""
        m = re.search(r'[?&]id=(\d+)', u or '')
        return m.group(1) if m else None

    want_id = _item_id(url)
    pages = _get_pages()

    # 这个商品页已经开着了 → 一次导航都不发，直接读它。
    # 既最快（不用等加载），也最不像机器人：真人就是把正看着的那个页面复制给助手，
    # 不会为了抓取再把它刷新一遍。顺带还保住了用户在这个页面上的状态。
    taobao_tab = None
    if want_id:
        for t in pages:
            tu = t.get('url', '')
            # 必须是淘宝/天猫的页面才算数：光比 id 会撞上别的站点同名的参数，
            # 那就变成「拿一个不相干的页面当商品页读」，抓出来一片空
            if ('taobao.com' in tu or 'tmall.com' in tu) and _item_id(tu) == want_id:
                taobao_tab = t
                break
    reused_open = taobao_tab is not None
    if not taobao_tab:
        taobao_tab = _find_taobao(pages)

    # 一个淘宝页都没有 → 新开。
    # 关键：不要 /json/new?<商品链接>。那样标签页是「从 about:blank 直接变出商品页」，
    # 没有来源页、没有跳转链、标签页历史只有一条 —— 偏偏我们原来每次都这么开。
    # 改成先落在淘宝首页（真人也是从首页点进去的），再从首页跳到商品页，
    # Referer 和跳转链就都自然了。
    _new_on_home = False
    if not taobao_tab:
        try:
            # /json/new 需要 PUT 方法（新版 Chrome 已弃用 GET）
            req = urllib.request.Request(
                f'http://127.0.0.1:9222/json/new?{quote("https://www.taobao.com/")}', method='PUT'
            )
            taobao_tab = json.loads(urllib.request.urlopen(req, timeout=10).read())
            _new_on_home = True
        except Exception:
            taobao_tab = None
        if not taobao_tab:
            await asyncio.sleep(3)
            taobao_tab = _find_taobao(_get_pages())

    if not taobao_tab:
        raise Exception("未找到淘宝标签页，请确认内置浏览器已启动")

    from one_click_list import CDPClient
    ws = await websockets.connect(taobao_tab['webSocketDebuggerUrl'], max_size=50*1024*1024)
    cdp = CDPClient(ws)
    await cdp.start()

    try:
        if reused_open:
            # 已经开着这个商品页：不导航，只等它加载完（通常早就是 complete，不花时间）
            ph.done('复用已打开的商品页（未导航）')
        else:
            if _new_on_home:
                # 等首页真落地了再跳（最多 8 秒，超时也照样往下走，不能卡住用户）。
                # 条件里必须带 href：新标签页一上来是 about:blank，readyState 立刻就是
                # 'complete'，只看 readyState 会秒过、首页还没提交就跳走了 —— 跳转链白给。
                for _ in range(16):
                    try:
                        st = await cdp.js("document.readyState + '|' + location.href")
                    except Exception:
                        st = None
                    if st and 'taobao.com' in st and st.split('|')[0] in ('interactive', 'complete'):
                        break
                    await asyncio.sleep(0.5)
            # 自动打开输入的商品链接并等待页面加载。
            # referrer + transitionType='link'：声明「是从淘宝站内点进来的」。
            # 原来是裸跳（默认 referrer 为空、transitionType='typed'），
            # 加上「每次都是全新 about:blank」——这两条摞一起就是标准的脚本特征。
            await cdp.call('Page.navigate', {
                'url': url,
                'referrer': 'https://www.taobao.com/',
                'transitionType': 'link',
            })
        for _ in range(30):
            try:
                ready = await cdp.js("document.readyState")
            except Exception:
                ready = None
            if ready == 'complete':
                break
            await asyncio.sleep(0.5)
        await asyncio.sleep(2)
        if not reused_open:
            ph.done('打开淘宝页并等加载')
        # 抓取标题
        title = await cdp.js('''
        (() => {
            // 淘宝标题
            let el = document.querySelector(".tb-main-title") ||
                     document.querySelector("[class*=mainTitle]") ||
                     document.querySelector("h3.tb-main-title");
            if (el) return el.innerText.trim();
            // 备用：从meta获取
            let meta = document.querySelector('meta[name="keywords"]');
            if (meta) return meta.content.split(',')[0].trim();
            return document.title.split('-')[0].trim();
        })()
        ''')

        # 抓取规格选项（通用：找到所有规格组）
        specs = await cdp.js('''
        (() => {
            let result = {};

            // 方法1: 淘宝新版React组件 - 只找skuValueWrap（规格值容器）
            // skuValueWrap是规格选择区域的直接容器
            let valueWraps = document.querySelectorAll('[class*=skuValueWrap]');
            for (let vw of valueWraps) {
                let name = '';

                // 先找兄弟元素中的labelWrap（最常见）
                let prev = vw.previousElementSibling;
                if (prev) {
                    // 精确取规格名：找labelWrapTitle里的span
                    let titleSpan = prev.querySelector('[class*=labelWrapTitle] span, [class*=ItemLabel] span');
                    if (titleSpan) {
                        name = titleSpan.textContent.trim();
                    } else {
                        let firstDiv = prev.querySelector('div');
                        if (firstDiv) name = firstDiv.textContent.trim();
                        else name = prev.textContent.trim();
                    }
                }

                // 如果没找到，向上找
                if (!name) {
                    let parent = vw.parentElement;
                    for (let i = 0; i < 5 && parent; i++) {
                        let titleEl = parent.querySelector('[class*=skuItemTitle], [class*=skuName]');
                        if (titleEl) { name = titleEl.textContent.trim(); break; }
                        let labelEl = parent.querySelector('[class*=labelWrap], [class*=Label]');
                        if (labelEl) {
                            let titleSpan = labelEl.querySelector('[class*=labelWrapTitle] span, [class*=ItemLabel] span');
                            if (titleSpan) { name = titleSpan.textContent.trim(); break; }
                            let firstDiv = labelEl.querySelector('div');
                            if (firstDiv) { name = firstDiv.textContent.trim(); break; }
                        }
                        parent = parent.parentElement;
                    }
                }

                if (!name || name.length > 10) continue;
                if (name === '商品规格') name = '规格';

                // 收集规格值
                let values = [];
                // 淘宝新版React用valueItemText，旧版用skuValue
                let valueEls = vw.querySelectorAll('[class*=valueItemText], [class*=skuValue], [class*=tb-prop-val]');
                for (let v of valueEls) {
                    let text = v.textContent.trim();
                    // 过滤掉空值、图片alt、太长的文本
                    if (text && text.length > 0 && text.length < 40 && !values.includes(text)) {
                        values.push(text);
                    }
                }
                if (values.length > 0) result[name] = values;
            }

            // 方法2: 传统淘宝选择器
            if (Object.keys(result).length === 0) {
                let groups = document.querySelectorAll('.tb-sku .tb-prop, .tb-prop, [class*=sku-prop]');
                for (let g of groups) {
                    let nameEl = g.querySelector('.tb-prop-name, dt, h3, label');
                    let name = nameEl ? nameEl.innerText.trim() : '';
                    if (!name) continue;
                    let values = [];
                    let items = g.querySelectorAll('.tb-prop-val a, dd a, li a');
                    for (let item of items) {
                        let v = item.innerText.trim();
                        if (v && !values.includes(v)) values.push(v);
                    }
                    if (values.length > 0) result[name] = values;
                }
            }

            // 方法3: 从页面文本提取（仅在方法1和2都失败时使用）
            if (Object.keys(result).length === 0) {
                let text = document.body.innerText;
                let lines = text.split('\\n');
                let currentSpec = null;
                // 排除词：不是规格值的文本
                let excludeWords = ['切换大图模式', '多人加购', '数量', '有货', '保障服务', '选购更多',
                    '加入购物车', '立即购买', '收藏', '规则协议', '淘宝规则', '平台服务协议',
                    '新手上路', '0元开店', '天猫开店', '商家服务', '付款方式', '快捷支付',
                    '余额宝', '蚂蚁花呗', '淘宝特色', '阿里旺旺', '官方客服', '问问', 'AI',
                    '商品规格', '图文详情', '店铺优惠', '领券购买', '有什么想了解'];
                // 只匹配已知的规格名模式
                let specPatterns = ['颜色分类', '尺寸', '尺码', '款式', '规格', '组合形式', '外框类型',
                    '容量', '型号', '版本', '套餐', '尺码', '颜色', '款式', '类型', '大小'];
                for (let line of lines) {
                    line = line.trim();
                    if (specPatterns.some(s => line === s)) {
                        currentSpec = line;
                        result[currentSpec] = [];
                    } else if (currentSpec && result[currentSpec] && line.length > 0 && line.length < 30) {
                        if (!result[currentSpec].includes(line) && !excludeWords.includes(line) && !line.match(/^[\\d.]+$/)) {
                            result[currentSpec].push(line);
                        }
                    }
                }
            }

            return JSON.stringify(result);
        })()
        ''')

        spec_dict = json.loads(specs) if specs else {}

        # ========== 提取规格对应的预览图 ==========
        # 预览图只对应第一个规格维度（颜色分类/主要颜色/款式等）；尺码等后续维度无图，
        # 同一颜色下不同尺码共用同一张预览图。
        # 规格缩略图是懒加载：页面刚加载时 <img>.src 还是 2x2 占位图(-tps-2-2.png)，
        # 真实地址要等滚动/交互后才替换，直接读 DOM 会拿到占位图导致下载出 67 字节假图。
        # 因此优先读服务端渲染数据 __ICE_APP_CONTEXT__.skuBase.props：每个颜色值自带 image 字段，
        # 不依赖 DOM 懒加载，最可靠。
        spec_images = {}  # {spec_value_name: image_url}
        spec_img_js = await cdp.js('''(() => {
            var result = [];
            function push(src, text) {
                if (!text || !src || src.indexOf("alicdn") < 0) return;
                if (src.indexOf("//") === 0) src = "https:" + src;
                src = src.split("?")[0];
                result.push({text: text, img: src});
            }
            // 1. 服务端渲染数据（可靠，不受懒加载影响）
            try {
                var ctx = window.__ICE_APP_CONTEXT__;
                var props = ctx && ctx.loaderData && ctx.loaderData.home &&
                            ctx.loaderData.home.data && ctx.loaderData.home.data.res &&
                            ctx.loaderData.home.data.res.skuBase &&
                            ctx.loaderData.home.data.res.skuBase.props;
                if (props && Array.isArray(props)) {
                    for (var i = 0; i < props.length; i++) {
                        var vals = props[i] && props[i].values;
                        if (!vals || !Array.isArray(vals)) continue;
                        for (var j = 0; j < vals.length; j++) {
                            var v = vals[j];
                            if (!v) continue;
                            push(v.image || v.imageUrl || "", v.name);
                        }
                    }
                }
            } catch (e) {}
            // 2. 兜底：读 DOM 已加载好的缩略图（仅在服务端数据缺失时用）
            if (result.length === 0) {
                var items = document.querySelectorAll("[class*=valueItem]");
                for (var k = 0; k < items.length; k++) {
                    var item = items[k];
                    var textEl = item.querySelector("[class*=valueItemText]");
                    var imgEl = item.querySelector("img");
                    if (textEl && imgEl && imgEl.src) {
                        push(imgEl.src, textEl.textContent.trim());
                    }
                }
            }
            return JSON.stringify(result);
        })()''')
        if spec_img_js:
            try:
                spec_img_list = json.loads(spec_img_js)
                for item in spec_img_list:
                    if item.get("img"):
                        spec_images[item["text"]] = item["img"]
                print(f"[SPEC_IMGS] 提取到 {len(spec_images)} 个规格预览图")
            except Exception as e:
                print(f"[WARN] 解析规格预览图失败: {e}")

        # 预览图只对应「颜色/款式」那一维（尺码等维度无图，混进来会导致上架时按序上传错位）。
        # 是哪一维要看数据（哪个维度的值带图），不能看顺序 —— 见 _pick_image_dim 的说明。
        if spec_images and spec_dict:
            _dim = _pick_image_dim(spec_dict, spec_images)
            if _dim:
                _keep = {_norm_val(v) for v in (spec_dict.get(_dim) or [])}
                spec_images = {k: v for k, v in spec_images.items() if _norm_val(k) in _keep}
                print(f"[SPEC_IMGS] 预览图归「{_dim}」这一维，保留 {len(spec_images)} 个")
            else:
                # 认不出属于哪一维（拿到的是尺码占位图、或图名跟规格值对不上）：这些图按颜色名
                # 一个也匹配不上，上架时用不了。清掉让下面「用主图代替」兜底 —— 预览图那列是必填的，
                # 有主图顶上总比整列空着强。
                print(f"[SPEC_IMGS] 这 {len(spec_images)} 个预览图认不出属于哪个规格维度（可能只是尺码占位图），"
                      f"改用主图兜底")
                spec_images = {}

        # 先滚动页面触发懒加载（淘宝新版React页面详情图需要滚动才加载）
        await cdp.js('''
        (async () => {
            let h = document.body.scrollHeight || 8000;
            for (let i = 0; i <= h; i += 800) {
                window.scrollTo(0, i);
                await new Promise(r => setTimeout(r, 150));
            }
            window.scrollTo(0, h);
            await new Promise(r => setTimeout(r, 1500));
            // 再滚一次确保
            window.scrollTo(0, h);
            await new Promise(r => setTimeout(r, 800));
            window.scrollTo(0, 0);
            return 'scrolled';
        })()
        ''')
        await asyncio.sleep(0.5)

        # 抓取主图和详情图URL
        all_imgs = await cdp.js('''
        (() => {
            let mainUrls = [];
            let detailUrls = [];
            let seen = new Set();

            // 清理URL的通用函数
            function cleanUrl(src) {
                if (!src) return '';
                if (src.startsWith('//')) src = 'https:' + src;
                src = src.split('?')[0];
                src = src.replace(/\\.webp$/, '');
                src = src.replace(/_-~~.*$/, '');
                src = src.replace(/_\\d+x\\d+q\\d+/, '');
                src = src.replace(/_q50$/, '');
                src = src.replace(/_[A-Za-z0-9_-]{20,}$/, '');
                src = src.replace(/_$/, '');
                return src;
            }

            function isProductImg(url) {
                if (!url.includes('alicdn.com')) return false;
                if (url.includes('passport') || url.includes('favicon') ||
                    url.includes('icon') || url.includes('logo') ||
                    url.includes('-tps-') || url.includes('tfs//') ||
                    url.includes('tfs/TB') || url.includes('.gif') ||
                    url.includes('.woff') || url.includes('.svg') ||
                    url.includes('gg_dtc')) return false;
                return url.includes('.jpg') || url.includes('.jpeg') || url.includes('.png');
            }

            function isMainImg(url) {
                return url.includes('/i4/73065033/') || url.includes('/i3/73065033/') ||
                       url.includes('/i2/73065033/') || url.includes('/i1/73065033/');
            }

            // 1. 从DOM收集
            document.querySelectorAll('img').forEach(img => {
                let src = img.dataset.src || img.dataset.lazyload || img.src || '';
                src = cleanUrl(src);
                if (!isProductImg(src) || seen.has(src)) return;
                seen.add(src);

                let cls = (img.className || '');
                if (cls.includes('thumbnailPic') || cls.includes('mainPic')) {
                    if (mainUrls.length < 5) mainUrls.push(src);
                } else {
                    detailUrls.push(src);
                }
            });

            // 2. 从HTML源码提取（重要！DOM可能没有渲染所有图片）
            let html = document.documentElement.outerHTML;
            let imgRegex = /https?:\\/\\/[^\"\\s<>]+alicdn[^\"\\s<>]+\\.(jpg|jpeg|png)/gi;
            let match;
            while ((match = imgRegex.exec(html)) !== null) {
                let url = cleanUrl(match[0]);
                if (!isProductImg(url) || seen.has(url)) continue;
                seen.add(url);
                // bao/uploaded的是商品图片
                if (url.includes('bao/uploaded') || url.includes('/73065033/')) {
                    detailUrls.push(url);
                }
            }

            return JSON.stringify({main: mainUrls, detail: detailUrls});
        })()
        ''')
        import json as _json
        imgs_data = _json.loads(all_imgs) if all_imgs else {"main": [], "detail": []}
        main_img_urls = imgs_data.get("main", [])
        detail_img_urls = imgs_data.get("detail", [])

        # 下载图片（带 Referer 绕过防盗链）。并发数和张数上限见文件上方 _IMG_WORKERS /
        # MAX_DETAIL_IMAGES 的说明 —— 这里是整轮抓取里请求量最大的一段，也是风控主要盯的地方。
        ph.done('读页面（标题/规格/图片地址）')
        main_dir = DATA_DIR / "main_images"
        detail_dir = DATA_DIR / "detail_images"
        local_main_images = _download_images_parallel(main_img_urls[:5], main_dir, "main")
        local_detail_images = _download_images_parallel(
            detail_img_urls[:MAX_DETAIL_IMAGES], detail_dir, "detail")
        if len(detail_img_urls) > MAX_DETAIL_IMAGES:
            print(f"[IMGS] 详情图原页有 {len(detail_img_urls)} 张，只取前 {MAX_DETAIL_IMAGES} 张"
                  f"（上限 MAX_DETAIL_IMAGES，抓多了既慢又容易触发风控）")

        # 下载规格预览图到本地（并发：颜色/款式值通常 3~8 个，串行逐张太慢）
        spec_dir = DATA_DIR / "spec_images"
        spec_dir.mkdir(parents=True, exist_ok=True)
        local_spec_images = {}  # {spec_value_name: local_path}
        if spec_images:
            import hashlib
            from concurrent.futures import ThreadPoolExecutor
            spec_items = list(spec_images.items())

            def _dl_spec(pair):
                name, img_url = pair
                safe_name = hashlib.md5(name.encode()).hexdigest()[:12]
                img_path = spec_dir / f"spec_{safe_name}.jpg"
                try:
                    _img_pace()      # 和主图/详情图一样：张数虽少，也别一口气全打出去
                    _download_image(img_url, str(img_path))
                    if img_path.exists() and img_path.stat().st_size > 512:
                        return name, str(img_path)
                except Exception:
                    pass
                return name, None

            with ThreadPoolExecutor(max_workers=max(2, min(6, _IMG_WORKERS))) as ex:
                for name, path in ex.map(_dl_spec, spec_items):
                    if path:
                        local_spec_images[name] = path
        print(f"[SPEC_IMGS] 下载了 {len(local_spec_images)} 个规格预览图")

        # 只丢掉「一眼假」的懒加载占位图（2×2）；几个颜色共用一张照片是卖家的事，照传不误
        local_spec_images, _ph_note = _drop_placeholder_spec_images(local_spec_images)
        if _ph_note:
            print(f"[SPEC_IMGS] {_ph_note}")
        # 几个颜色拿到同一张照片时说清楚（用户看到的现象是「预览图传的还是主图」，会以为工具拿错了）
        _same_note = _describe_same_pictures(local_spec_images, local_main_images)
        if _same_note:
            print(f"[SPEC_IMGS] {_same_note}")
        if not local_spec_images and local_main_images:
            # 只用主图给「颜色/款式」那一维兜底，不给尺码值塞图，否则按序上传会错位。
            # 这一维按名字认（颜色/款式 → 其次非尺码维度 → 再不行第一个），不能拿第一个键，
            # 页面上尺码排在颜色前面时第一个键就是尺码（见 _pick_image_dim 的说明）。
            _dim = _name_dim(spec_dict)
            for i, sv in enumerate(spec_dict.get(_dim) or []):
                img_path = local_main_images[i % len(local_main_images)]
                local_spec_images[sv] = img_path
            print(f"[SPEC_IMGS] 用主图代替: {len(local_spec_images)} 个规格值（{_dim or '认不出维度'}）")

        ph.done(f'下载图片（主{len(local_main_images)}/详{len(local_detail_images)}）')
        ph.summary()
        return {
            "title": title or "",
            "specs": spec_dict,
            "spec_images": local_spec_images,
            "main_images": local_main_images,
            "main_image_urls": main_img_urls,
            "detail_images": local_detail_images,
            "detail_image_urls": detail_img_urls
        }
    finally:
        await ws.close()


class _Phase:
    """给一轮上架分阶段计时 —— 「还是有点慢」得先量出来慢在哪，别再靠猜（2026-09-27）。

    每段结束就打一行 `[时间] 名称 12.3s`（flush，因为启动器把 stdout 重定向进日志文件，
    不 flush 会块缓冲，日志要滞后好几 KB 才看得到）。最后再打一行总计。
    """

    def __init__(self):
        self.t0 = time.time()
        self.marks = []

    def done(self, name):
        dt = time.time() - self.t0
        self.marks.append((name, dt))
        print(f'[时间] {name} {dt:.1f}s', flush=True)
        self.t0 = time.time()

    def summary(self):
        total = sum(d for _, d in self.marks)
        top = '，'.join(f'{n} {d:.1f}s' for n, d in sorted(self.marks, key=lambda x: -x[1])[:4])
        print(f'[时间] 本轮合计 {total:.1f}s —— 最花时间的：{top}', flush=True)


@app.get("/api/pdd-shops", dependencies=[Depends(require_license)])
async def pdd_shops():
    """列出「可以把商品上架到哪几家店」，给前端的店铺勾选弹窗用。

    只读：连上去问页面自己「你是哪家店、停在发布页没有」——不导航、不点击、不改任何东西。
    （每个店铺窗口一个独立调试端口，登录态按窗口隔离，所以端口 = 一家店。）
    """
    from one_click_list import list_shops
    try:
        shops = await list_shops()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"检测店铺失败：{e}")
    return {"shops": shops}


@app.post('/api/listing/preflight', dependencies=[Depends(require_license)])
def listing_preflight(payload: dict):
    from product_package import preflight_listing
    return preflight_listing(payload, project_dir=PROJECT_DIR)


@app.post("/api/start-listing", dependencies=[Depends(require_license)])
async def start_listing(req: StartListingRequest):
    """启动一键上架CDP自动化任务（一家店，或同时多家店）"""
    from product_package import preflight_listing
    checked = preflight_listing(req.model_dump(), project_dir=PROJECT_DIR)
    if not checked['ok']:
        raise HTTPException(422, detail=checked)
    import launcher

    # 目标店铺：勾了哪几家就上哪几家。勾选必须落在真实存在的调试端口上 —— 放任何别的
    # 端口进来，等于把商品往一个来路不明的窗口里填。
    known = {s['port'] for s in launcher.shop_slots()}
    shops = [p for p in (req.shops or []) if p]
    unknown = [p for p in shops if p not in known]
    if unknown:
        raise HTTPException(status_code=400,
                            detail=f"这些店铺已经不在了：{unknown}。"
                                   f"请重新点「执行上架」，让工具重新检测店铺")
    from one_click_list import list_shops
    from listing_readback import identity_matches
    current = {str(row['port']): row for row in await list_shops()}
    for port in shops:
        if not identity_matches(req.shop_identities.get(str(port)), current.get(str(port))):
            raise HTTPException(409, 'Target shop identity unavailable or changed; detect shops again')
    req.shops = list(dict.fromkeys(shops))
    n_shops = len(req.shops)

    # 额度按店铺算：选 3 家就要有 3 次额度。不够就整单拒绝，不做「能上几家上几家」——
    # 那样用户会以为全上了（2026-09-28 用户定的口径）。
    _ok, _reason, _left = license.can_list_n(n_shops)
    if not _ok:
        if _left is not None and _left > 0:
            raise HTTPException(
                status_code=403,
                detail=f"选了 {n_shops} 家店，但本月只剩 {_left} 次额度。"
                       f"少勾几家，或联系出租方提升限额")
        raise HTTPException(status_code=403, detail="本月上架额度已用完，请联系出租方续费或提升限额")
    import uuid
    task_id = str(uuid.uuid4())[:8]

    # 同一时间只准跑一轮上架。2026-09-27 的日志里出现了两轮交错跑在**同一个**商品页上
    # （同一段日志里「选中的页面」出现两次、每个阶段都跑了两遍，尺码还被两边各敲一次成了
    # 「MM」「LL」）—— 用户感觉「慢」，一半就是这么来的：两边抢同一个页面，谁也快不了。
    with _LISTING_GUARD:
        if _LISTING_ACTIVE:
            _st = _bg_tasks.get(_LISTING_ACTIVE.get("task_id") or "", {}) or {}
            _p = _st.get("progress") or "准备执行..."
            _s = time.time() - float(_LISTING_ACTIVE.get("started") or time.time())
            raise HTTPException(
                status_code=409,
                detail=f"上一轮上架还在跑（已经 {_s:.0f} 秒，正在「{_p}」）。"
                       "两轮同时操作同一个商品页会把规格和图片写乱，等它跑完再点执行上架")
        from task_store import request_digest, StoreError
        raw = req.model_dump()
        digest = request_digest({key:value for key,value in raw.items() if key != 'client_request_id'})
        existing_task, created = _ai_runner.store.create_task('local', 'listing', {'digest': digest, 'product': raw}, req.client_request_id)
        if not created:
            if existing_task['payload'].get('digest') != digest:
                raise HTTPException(409, 'Submission identifier conflicts with another product')
            return {'task_id': existing_task['id'], 'status': existing_task['status'], 'duplicate': True}
        task_id = existing_task['id']
        try:
            _ai_runner.store.reserve('local', task_id, 'listing', n_shops)
        except StoreError as exc:
            raise HTTPException(403, str(exc)) from exc
        _LISTING_ACTIVE.update({"task_id": task_id, "started": time.time()})

    def run_task():
        import asyncio
        import traceback
        _bg_tasks[task_id] = {"status": "running", "progress": "Preparing form entry"}
        _ai_runner.store.update_task('local', task_id, status='running', progress='Preparing form entry')
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_do_listing(req, task_id))
            loop.close()
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[LISTING ERROR] {tb}")
            _bg_tasks[task_id] = {"status": "error", "message": str(e), "traceback": tb}
        finally:
            outcome = _bg_tasks.get(task_id) or {'status': 'error', 'message': 'Interrupted'}
            _ai_runner.store.update_task('local', task_id, status=outcome.get('status','error'), result=outcome, progress=outcome.get('message',''))
            verified = sum(1 for row in outcome.get('results', []) if row.get('verified'))
            _ai_runner.store.settle('local', task_id, 'listing', verified)
            with _LISTING_GUARD:
                _LISTING_ACTIVE.clear()

    thread = threading.Thread(target=run_task, daemon=True)
    thread.start()
    return {"task_id": task_id}


def _spec_display_name(name):
    """规格类型名用于 Excel 表头的显示名。

    - 颜色类统一显示为「颜色」
    - 尺寸类统一显示为「尺码」：拼多多的规格类型就叫「尺码」，
      淘宝那边叫「尺寸」，表头直接跟拼多多保持一致，
      商家看着顺、和拼多多模板也对得上（映射规则与 one_click_list._map_spec_type 一致）
    """
    name = name or ''
    if any(k in name for k in ['颜色', '色号', '花色', '花型']):
        return '颜色'
    if any(k in name for k in ['尺码', '尺寸', '大小', '码数']):
        return '尺码'
    return name


def _generate_spec_excel(specs, spec_path, color_header="颜色"):
    """生成规格 Excel（spec_input.xlsx）。颜色类表头统一用 color_header（「颜色」或「颜色分类」）。"""
    import openpyxl
    import re as _re
    from itertools import product as iter_product

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "规格"

    size_keywords = ['尺码', '尺寸', '大小', '型号', '规格']
    color_keywords = ['颜色', '颜色分类', '款式', '风格']
    size_cols = [k for k in specs.keys() if any(s in k for s in size_keywords)]
    color_cols = [k for k in specs.keys() if any(c in k for c in color_keywords)]
    other_cols = [k for k in specs.keys() if k not in size_cols and k not in color_cols]
    col_names = color_cols + size_cols + other_cols

    # 表头：颜色类统一显示为 color_header，尺寸类显示为「尺码」，其余用原始名
    header_names = [color_header if n in color_cols else _spec_display_name(n) for n in col_names]
    ws.append(header_names)

    def _clean_spec_value(val, col_name):
        if not val:
            return val
        if any(s in col_name for s in size_keywords):
            m = _re.match(r'^([0-9]*[SMLXLsmlxl]+)', val.strip())
            if m:
                return m.group(1)
        return val

    col_values = []
    for name in col_names:
        col_values.append([_clean_spec_value(v, name) for v in specs[name]])
    for combo in iter_product(*col_values):
        ws.append(list(combo))

    wb.save(str(spec_path))
    print(f"[EXCEL] 生成规格Excel: 列头={header_names}, 各列值数={[len(specs[n]) for n in col_names]}")
    return col_names


# 多店铺并行的两个开关（2026-09-28 用户选了「多家同时开跑」）。
# 做成常量而不写死，是因为并行意味着 N 家店的上传流量同时压向拼多多 —— 万一被限流，
# 改一行就能退回串行，不必翻代码。
_LISTING_PARALLEL = True
_SHOP_STAGGER = (2.0, 5.0)      # 并行时每家店起步错开 2~5 秒，别像机器齐射


def _shop_progress(task_id, key, text, status=None, name=None, page=None):
    """把「这家店现在干到哪一步了」写进任务状态。key 是端口号，每店只写自己那把。

    多店并行时不能再往顶层 progress 里塞：三家店会互相覆盖，用户看到的就是一句话乱跳，
    分不清哪家跑到哪了。顶层的 progress 只由父任务在开头和结尾写。
    """
    task = _bg_tasks.get(task_id)
    if task is None:
        return
    row = task.setdefault("shops", {}).setdefault(str(key), {})
    row["port"] = key
    if name is not None:
        row["name"] = name
    if page is not None:
        row["page"] = page
    if text is not None:
        row["progress"] = text
    if status is not None:
        row["status"] = status


def _spec_excel_path(port) -> Path:
    """这一家店自己那份规格 Excel 的落盘路径。

    **必须按店铺分开**：表头名取决于该店页面的规格界面类型（手动界面是「颜色」、
    新版界面是「颜色分类」，见 _list_one_shop 里的 color_header），两家店的表内容
    可能不同；共用一个 data/spec_input.xlsx 会互相覆盖 —— 前一家刚生成就被后一家
    盖掉，规格直接填错。
    """
    return DATA_DIR / f"spec_input_{port}.xlsx"


async def _do_listing(req: StartListingRequest, task_id: str):
    """执行一键上架流程：准备一次，然后给每一家目标店铺各跑一遍（默认并行）。

    分工：
    - **父**（本函数）只做一次性准备 —— 规格推导与过滤、价格映射、主图存在性检查，
      全是纯计算和读文件、跟连哪家店无关；然后按店铺扇出、汇总、按成功的店数计费。
    - **子**（_list_one_shop）跑一家店的完整流程：连它自己那个窗口 → 生成自己那份
      规格 Excel → 填规格/主图/预览图/价格/品牌属性/详情图。
    """
    # 1. 规格 Excel 目录（表头名与规格界面类型有关，连上该店之后再生成）
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 优先用传入的 specs 字典（从淘宝抓取的规格类型→规格值映射）
    specs = req.specs if req.specs else {}

    # 如果没有 specs，从 SKU 名称中推断
    if not specs:
        spec_columns = {}  # {col_index: [unique_values]}
        for sku in req.skus:
            name = sku.get("name", "")
            parts = name.split(" ", 1) if " " in name else [name]
            for i, part in enumerate(parts):
                part = part.strip()
                if i not in spec_columns:
                    spec_columns[i] = []
                if part and part not in spec_columns[i]:
                    spec_columns[i].append(part)
        # 尝试从spec_images的key推断列名
        spec_names = list(req.spec_images.keys()) if req.spec_images else []
        for i in sorted(spec_columns.keys()):
            found_name = None
            if spec_names:
                for sn in spec_names:
                    if sn in spec_columns[i]:
                        found_name = sn
                        break
            col_name = found_name if found_name else f"规格{i+1}"
            specs[col_name] = spec_columns[i]

    # 只保留「启用」的 SKU 里出现过的规格值与预览图：未勾选的规格不生成、不上传
    # 颜色值可能含空格（如「绿色 马甲」），不能按空格拆词匹配，要按 SKU 名的前缀/后缀整段匹配
    # 注意 req.skus 里包含未勾选的 SKU（它们的价格/停用状态仍要写进 Excel），所以这里要按 enabled 过滤
    enabled_names = [' '.join((s.get('name') or '').split())
                     for s in req.skus if s.get('enabled', True)]
    enabled_names = [n for n in enabled_names if n]

    if specs and req.skus:
        dims = list(specs.keys())
        filtered_specs = {}
        for i, dim in enumerate(dims):
            kept = []
            for v in specs[dim]:
                v = (v or '').strip()
                if not v:
                    continue
                hit = False
                for name in enabled_names:
                    if len(dims) == 1:
                        hit = (name == v)
                    elif i == len(dims) - 1:
                        # 最后一个维度（尺码）：SKU 名以「值」结尾
                        hit = (name == v) or name.endswith(' ' + v)
                    else:
                        # 前面的维度（颜色等，值可能含空格）：SKU 名以「值 」开头
                        hit = (name == v) or name.startswith(v + ' ')
                    if hit:
                        break
                if hit:
                    kept.append(v)
            if kept:
                filtered_specs[dim] = kept
        specs = filtered_specs

    # 预览图只对应第一个规格维度（颜色/款式等）：尺码值旁的 <img> 是占位副本，
    # 混进来会让 upload_spec_preview_images 按「颜色」匹配时错位（同一颜色下多尺码要共图）
    if req.spec_images and req.skus:
        first_dim_values = set()
        for _dim_name, _dim_vals in specs.items():
            first_dim_values.update((v or '').strip() for v in _dim_vals)
            break  # 只取第一个维度
        req.spec_images = {k: v for k, v in req.spec_images.items() if (k or '').strip() in first_dim_values}

    # 规格 Excel 的生成已移到连接 PDD 之后（表头名取决于手动/新版两种规格界面）

    # 2. 构建价格映射
    price_map = {}
    sku_by_name = {}          # 完整 SKU 名 → SKU（价格/库存/启用状态都按整名匹配）
    for sku in req.skus:
        name = " ".join(str(sku.get("name", "") or "").split())
        if not name:
            continue
        # 键用完整 SKU 名：颜色值可能含空格，之前按空格截第一段会把价格串到别的规格上
        price_map[name] = (sku.get("price", 0), sku.get("single_price", 0))
        sku_by_name[name] = sku

    # 库存默认值：取第一个「勾选了、而且填了库存」的 SKU 的库存；一个都没有就用 100。
    # 逐行填的时候每行用自己 SKU 的库存，这个值只是「这个 SKU 没填库存」时的兜底
    # （见 one_click_list._price_row）。口径跟改动前一字不变。
    stock = next((s.get("stock") for s in req.skus if s.get("enabled", True) and s.get("stock") not in (None, "")), 0)

    def local_asset(value):
        if str(value).startswith(('https://','http://')):
            import hashlib
            target = DATA_DIR / 'listing_assets' / (hashlib.sha256(str(value).encode()).hexdigest() + '.jpg')
            target.parent.mkdir(parents=True, exist_ok=True)
            _download_image(str(value), target)
            return str(target)
        return _upload_path(value)
    req.main_images = [local_asset(value) for value in req.main_images]
    req.detail_images = [local_asset(value) for value in req.detail_images]
    req.spec_images = {name: local_asset(value) for name,value in req.spec_images.items()}

    # 3. 准备工作（只做一次，跟连哪家店无关）
    _bg_tasks[task_id]["progress"] = "准备执行上架..."

    # 下载主图到本地（如果传入的是远程URL则用CDP下载，本地路径则直接用）
    main_dir = DATA_DIR / "main_images"
    main_dir.mkdir(parents=True, exist_ok=True)
    local_main = []
    # PDD 轮播图通常最多 10 张；AI 流程会返回不超过 10 张候选，旧抓取流程也可以复用这个上限。
    for i, img_path in enumerate(req.main_images[:10]):
        upload_path = _upload_path(img_path)
        if str(upload_path).startswith("http"):
            local_main.append(upload_path)
        elif os.path.exists(upload_path):
            local_main.append(upload_path)
        else:
            local_main.append(upload_path)

    # 缺图必须吵出来。2026-09-15 就是在这里静默跳过了一整轮上架：前端刷新过页面，
    # 主图/详情图列表是空的（那四个数组当时只存在内存里），下面 `if local_main:` 和
    # `if req.detail_images:` 两个分支一起不成立 —— 日志里一个字都没有，
    # 用户在商品页上看到的就是「主图、商品详情图啥的都没上传」，也不知道该回去补哪一步。
    img_warn = []
    missing_main = [p for p in local_main
                    if not str(p).startswith('http') and not os.path.exists(p)]
    if missing_main:
        print(f'[IMG] 这些主图文件已经不在了，跳过：{missing_main}')
        img_warn.append(f'{len(missing_main)} 张主图的文件已经不在了')
        local_main = [p for p in local_main if p not in missing_main]
    if not local_main:
        print('[IMG] ⚠ 主图一张都没有 —— 这次上架不会往轮播图里传任何图'
              f'（前端传过来 {len(req.main_images)} 个、去掉失效的剩 0 个）')
        img_warn.append('主图一张都没传（回上一步重新抓取淘宝）')
    else:
        print(f'[IMG] 主图 {len(local_main)} 张待上传到轮播图')

    # 4. 扇出到目标店铺
    import launcher
    ports = launcher.list_shop_ports()
    # 勾了哪几家就上哪几家；没指定（老前端、或有人直接打接口）→ 沿用单店老行为：
    # 在所有店铺窗口里自动挑第一个停在发布页的。targets 里的 None 就是「自动挑」。
    if not req.shops:
        raise ValueError('未指定目标店铺，已停止填表')
    targets = list(dict.fromkeys(req.shops))

    local_detail = [_upload_path(p) for p in (req.detail_images or [])]
    local_spec_images = {
        str(k): _upload_path(v) for k, v in (req.spec_images or {}).items()
        if v
    }

    prepared = {
        "specs": specs,
        "price_map": price_map,
        "sku_by_name": sku_by_name,
        "local_main": local_main,
        "local_detail": local_detail,
        "local_spec_images": local_spec_images,
        "stock": stock,
        "ports": ports,
        # {端口: 用第几个最近分类}，前端下拉选的。全走参数往下传，**不用模块级全局** ——
        # 多店是 asyncio.gather 在同一个线程里跑，全局变量会互相串店。
        "shop_cats": req.shop_cats or {},
    }

    if req.shops:
        # 明细行先建出来：用户一点执行就能看到「有哪几家、谁在排队」，而不是干等
        for _p in targets:
            _shop_progress(task_id, _p, "排队等待...", status="pending")
        _bg_tasks[task_id]["progress"] = f"正在给 {len(targets)} 家店上架..."
    else:
        _bg_tasks[task_id]["progress"] = "连接PDD页面..."

    if _LISTING_PARALLEL and len(targets) > 1:
        # 并行：各家店自己在自己的浏览器窗口里跑，互不共享任何东西（连接、页面、
        # 规格 Excel、行数记录、进度 key 都按端口分开）。起步错开 2~5 秒。
        results = await asyncio.gather(*[
            _list_one_shop(req, task_id, p, prepared,
                           delay=i * random.uniform(*_SHOP_STAGGER))
            for i, p in enumerate(targets)
        ], return_exceptions=True)
    else:
        results = []
        for p in targets:
            results.append(await _list_one_shop(req, task_id, p, prepared))

    # gather(return_exceptions=True) 正常不会抛，但真抛了也要变成「这一家失败」，
    # 不能把整轮炸掉、让另外几家已经填好的结果连说都说不出来。
    # key 要照 targets 补齐，否则这一家的明细行会永远停在「正在跑」上。
    results = [r if isinstance(r, dict) else
               {"port": targets[i], "key": None if targets[i] is None else str(targets[i]),
                "ok": False, "message": f"这家店的流程内部出错了：{r!r}", "warnings": []}
               for i, r in enumerate(results)]

    # 5. 计费：**按店铺算**（上 3 家扣 3 次，2026-09-28 用户定的口径），只算成功的店。
    #    放在父里、所有店跑完之后串行调用 —— 顺手避开了 license.increment_usage()
    #    那个「读-改-写」在并行下丢计数的问题（usage.json 的读改写没有锁）。
    ok_results = [r for r in results if r.get("verified")]
    for _ in ok_results:
        license.increment_usage()
    usage = license.get_usage()

    # 6. 汇总
    def _label(r):
        return r.get("name") or (f"端口 {r['port']}" if r.get("port") else "店铺")

    shops_detail = _bg_tasks[task_id].get("shops") or {}
    notes = list(img_warn)        # 缺主图/缺详情图是共用的，不属于某一家店
    for r in results:
        if r.get("key"):
            _shop_progress(task_id, r["key"],
                           "完成" if r.get("ok") else (r.get("message") or "失败"),
                           status="done" if r.get("verified") else "needs_review" if r.get("ok") else "error",
                           name=r.get("name"))
        for w in (r.get("warnings") or []):
            # 只有一家店时不加「端口 922x：」前缀 —— 单店的消息文案跟以前保持一致
            notes.append(f'{_label(r)}：{w}' if len(results) > 1 else w)

    if ok_results and len(ok_results) == len(results):
        status = "filled_verified"
    elif ok_results:
        status = "partial"
    elif any(r.get("ok") for r in results):
        status = "needs_review"
    else:
        status = "error"

    if len(results) == 1:
        only = results[0]
        msg = only.get("message") or ("上架完成" if only.get("ok") else "上架失败")
    elif ok_results:
        good = '、'.join(_label(r) for r in ok_results)
        bad = '、'.join(f'{_label(r)}（{r.get("message") or "失败"}）'
                        for r in results if not r.get("ok"))
        msg = f"{len(results)} 家店里 {len(ok_results)} 家上架完成：{good}"
        if bad:
            msg += f"。没上去的：{bad}"
    else:
        msg = "所有店铺都没上架成功：" + '；'.join(
            f'{_label(r)}（{r.get("message") or "失败"}）' for r in results)

    if notes:
        # 提醒全都拼进 message（前端直接把 message 显示给用户），别只在控制台里躺着
        msg += ('' if msg.endswith('。') else '。') + '注意：' + '；'.join(notes)

    _bg_tasks[task_id] = {
        "status": status,
        "verified": status == "filled_verified",
        "results": results,
        "message": msg,
        "warning": '；'.join(notes) or None,
        "shops": shops_detail,
        "sku_count": len([s for s in req.skus if s.get("enabled", True)]),
        "used": usage["used"],
        "quota": usage["quota"],
    }


async def _list_one_shop(req: StartListingRequest, task_id: str, port, prepared,
                         delay: float = 0.0) -> dict:
    """把商品上架到**一家**店，返回 {port, key, name, ok, message, warnings}。

    port=None 表示「自动挑一家」（老的单店路径，没勾店铺时用）。
    **指定了端口却连不上时直接判这家失败，绝不回退到别的店** —— 把 A 店的商品
    上到 B 店是最严重的事故，宁可这家不成功。
    """
    from one_click_list import (connect_pdd_tab, connect_pdd_tab_at_port, detect_spec_interface,
                                fill_specs_direct, fill_specs_new, batch_fill_prices,
                                probe_shop_identity, set_shop_tag)

    specs = prepared["specs"]
    price_map = prepared["price_map"]
    sku_by_name = prepared["sku_by_name"]
    local_main = prepared["local_main"]
    local_detail = prepared.get("local_detail") or []
    local_spec_images = prepared.get("local_spec_images") or {}
    stock = prepared["stock"]

    if delay:
        await asyncio.sleep(delay)

    key = None if port is None else str(port)
    name = None

    def say(text):
        """单店（自动挑）时写顶层 progress，跟以前完全一样；多店时写自己那把 key。"""
        if key is None:
            _bg_tasks[task_id]["progress"] = text
        else:
            _shop_progress(task_id, key, text, status="running", name=name)

    if key is not None:
        _shop_progress(task_id, key, "连接店铺窗口...", status="running")

    ws = None
    pdd_port = port          # 连不上时 except 里要用，不能等到 try 里才第一次赋值
    # 这家店用「最近使用的分类」里的第几个（前端下拉选的，没选就是第一个）。
    # 停在发布页的店压根用不到它；停在分类页的店靠它决定点哪一条。
    cat_index = (prepared.get("shop_cats") or {}).get(str(port), 0)

    def _need_manual(msg):
        """停在等人手选分类这一步了 —— 用 waiting 状态，前端要显眼（这不是在跑，是在等人）。"""
        print(f'  [店铺] {msg}')
        if key is None:
            _bg_tasks[task_id]["progress"] = msg
        else:
            _shop_progress(task_id, key, msg, status="waiting", name=name)

    try:
        if port is None:
            cdp, ws, pdd_port = await connect_pdd_tab(
                title=req.title,
                main_images=local_main if local_main else None,
                ports=prepared["ports"],
                cat_index=cat_index,
                on_wait=_need_manual,
            )
        else:
            # 勾了但没停在发布页的店：在**它自己**的窗口里开一个发布页（用户选的方案）
            cdp, ws, pdd_port = await connect_pdd_tab_at_port(
                port, title=req.title,
                main_images=local_main if local_main else None,
                cat_index=cat_index,
                on_wait=_need_manual,
                expected_identity=req.shop_identities.get(str(port)),
            )

        # 截图/调试日志按店铺岔开文件名（多店并行时互相覆盖等于没留证据）。
        # 必须在连上之后立刻设 —— 用**真实连上的端口**，不是用户勾的那个。
        set_shop_tag(pdd_port)
        # 店名从页面上读（读不到就留空，调用方回退成「端口 922x」—— 绝不编一个出来）。
        # 光有端口号的话，用户看到「端口 9224 没上去」还得自己回忆那是哪家店。
        name = (await probe_shop_identity(cdp)).get('name') or None

        ph = _Phase()
        ph.done('连接/挑页')
        say(f"已连接店铺窗口(端口 {pdd_port})，开始填表...")

        warnings = []
        if req.category:
            # AI 类目只是候选，不能凭自然语言直接替用户确认平台叶子类目；
            # 把它写入进度和结果，提醒用户在发布页核对。
            say(f"AI 类目候选：{req.category}（请在页面核对）")
            warnings.append(f"AI 类目候选为「{req.category}」，请在拼多多页面确认")

        # 检测规格界面类型：新版（颜色分类/尺码固定大类）/ 手动（有「添加规格类型」）；AI添加规格已停用
        interface = await detect_spec_interface(cdp)
        ph.done('识别规格界面')
        color_header = "颜色" if interface == "manual" else "颜色分类"
        print(f'[SPEC] 规格界面类型: {interface}，颜色表头: {color_header}')

        # 生成一份规格 Excel：给商家下载/回传用。**每家店一份自己的文件名**。
        # 注意：**填价格不用它** —— batch_fill_prices 是直接在页面的规格表里填的
        # （fill_prices_in_page），一个文件都不传。2026-09-15 定案：Excel 导入这条路
        # 在真机上过不去，挂上文件那一刻拼多多的渲染进程就冻住 —— 连它自己导出的、
        # 只换了几个数字的表都不行（08:13 那次更是 V8 堆耗尽直接崩，转储里
        # JavaScript heap out of memory，死在它自己的 add.*.js）。这份表只留给商家看/填。
        spec_path = _spec_excel_path(pdd_port)
        say("生成规格Excel...")
        _generate_spec_excel(specs, spec_path, color_header=color_header)
        ph.done('生成规格Excel')

        # 这家店自己那份「上一次数到的页面行数」。多店并行时绝不能共用模块级那份：
        # A 填完 B 又填，B 的行数就成了 A 的对照基准，A 会误报「规格表掉了」。
        spec_rows = {}

        if interface == "new":
            say("填充颜色分类和尺码...")
            # 返回一句话 = 规格没全加载到页面上（用户 2026-09-27：「商品过多时规格并没有
            # 全部添加」）。以前这里把返回值丢了，日志说填完了、页面上却少一半。
            spec_fill_warn = await fill_specs_new(cdp, specs, spec_rows=spec_rows)
            if spec_fill_warn:
                print(f'[SPEC] ⚠ {spec_fill_warn}')
                warnings.append(spec_fill_warn)
        else:
            say("填充规格类型和规格值...")
            await fill_specs_direct(cdp, specs)
        ph.done('填规格')

        # 上传主图到轮播图位置。返回一句话就说明没传成 —— 要带进最终结果里给用户看，
        # 不能像 2026-09-15 那次一样主图没传却一路安静。
        if local_main:
            say("上传主图到轮播图...")
            from one_click_list import upload_main_images_to_carousel
            main_warn = await upload_main_images_to_carousel(cdp, local_main)
            if main_warn:
                print(f'[IMG] ⚠ {main_warn}')
                warnings.append(main_warn)
        ph.done(f'主图上传（{len(local_main)}张）')

        # 上传规格预览图
        if local_spec_images:
            # 前端刷新过页面时，这些路径是从浏览器缓存里恢复出来的，可能已经不在了。
            # 不存在的文件要在这里挑掉，不然上传时找不到文件、白白重试和误判失败。
            spec_imgs = {}
            for k, v in local_spec_images.items():
                if not v:
                    continue
                if str(v).startswith('http') or os.path.exists(_resolve_abs(v)):
                    spec_imgs[k] = v
            gone = [k for k in local_spec_images if k not in spec_imgs]
            if gone:
                print(f'[SPEC] 这些规格的预览图文件已经不在了，跳过：{gone}')
            if spec_imgs:
                say("上传规格预览图...")
                from one_click_list import upload_spec_preview_images
                # 返回一句话就说明有行没传上（以前丢了返回值，日志说「完成」但图少几张）
                spec_warn = await upload_spec_preview_images(cdp, spec_imgs)
                if spec_warn:
                    print(f'[IMG] ⚠ {spec_warn}')
                    warnings.append(spec_warn)
        ph.done('规格预览图')

        say("填写价格库存...")
        # 返回 None 表示填完并回读核对通过；返回文字表示「值填进去了但页面上没留住」或
        # 「有几行规格在工具里找不到对应 SKU」，都要如实带到最终结果里，不能只说「完成」
        fill_warn = await batch_fill_prices(cdp, price_map, stock,
                                            spec_excel_path=str(spec_path),
                                            sku_by_name=sku_by_name, spec_rows=spec_rows)
        ph.done('填价格库存')

        # 填写品牌和商品属性
        if req.brand or req.attrs:
            say("填写品牌和属性...")
            from one_click_list import fill_brand_and_attrs
            await fill_brand_and_attrs(cdp, brand=req.brand, attrs=req.attrs)
        ph.done('品牌和属性')

        # 上传详情图
        if local_detail:
            say("上传详情图...")
            from one_click_list import upload_detail_images
            # 同上：返回一句话就是「没全部传上」，要带进最终提示，不能只在控制台里躺着
            # stats：拿回「真上去几张」。时间轴上写「详情图（47张）」是提交的张数，
            # 用户 2026-09-27 就是被这个数骗了 —— 「没有说少传几张」。
            _dst = {}
            detail_warn = await upload_detail_images(cdp, local_detail, stats=_dst)
            if detail_warn:
                print(f'[IMG] ⚠ {detail_warn}')
                warnings.append(detail_warn)
            if _dst.get('good') is None:
                ph.done(f'详情图（{len(local_detail)}张）')
            else:
                ph.done(f'详情图（上去 {_dst["good"]}/{_dst.get("total") or len(local_detail)} 张）')
        else:
            print('[IMG] ⚠ 详情图一张都没有 —— 这次上架不会传详情图（回上一步重新抓取淘宝）')
            warnings.append('详情图一张都没传（回上一步重新抓取淘宝）')

        # AI 流程同时生成了商品描述时，尽量填入当前页面的文本框/富文本编辑器；
        # 页面改版导致找不到字段时保留告警，不能伪报“已填写”。
        if req.description:
            say("填写商品描述...")
            from one_click_list import fill_product_description
            desc_result = await fill_product_description(cdp, req.description)
            if desc_result == 'NOT_FOUND':
                warnings.append('商品描述输入框未找到，已保留文字供人工复制')

        ph.summary()

        notes = []
        if fill_warn:
            notes.append(f'{fill_warn}，请到商品页核对一下价格')
        notes.extend(warnings)
        from listing_readback import verify_form
        readback = await verify_form(cdp, req.model_dump(), warnings=notes)
        return {
            'port': pdd_port, 'key': key, 'name': name, 'ok': True,
            'verified': readback['verified'], 'status': readback['status'],
            'message': readback['message'], 'readback': readback, 'warnings': notes,
        }
    except Exception as e:
        print(f'[LISTING] 店铺端口 {port} 上架失败：{e!r}')
        return {
            "port": port or pdd_port, "key": key, "name": name, "ok": False,
            "message": str(e) or repr(e),
            "warnings": [],
        }
    finally:
        if ws is not None:
            await ws.close()


@app.get("/api/one-click/{task_id}", dependencies=[Depends(require_license)])
async def one_click_status(task_id: str):
    """查询一键上架任务状态"""
    if task_id in _bg_tasks:
        return _bg_tasks[task_id]
    task = _ai_runner.store.get_task('local', task_id)
    if not task:
        raise HTTPException(404, '任务不存在')
    return {**(task.get('result') or {}), 'status': task['status'], 'progress': task['progress'], 'task_id': task_id}


class SkuListRequest(BaseModel):
    specs: dict = {}  # {规格类型名: [规格值列表]}
    skus: list = []  # [{name, price, single_price, stock, enabled}]


@app.post("/api/generate-sku-excel", dependencies=[Depends(require_license)])
async def generate_sku_excel(req: SkuListRequest):
    """生成SKU Excel文件供下载（拼多多规格批量编辑格式）"""
    import openpyxl
    import uuid
    from itertools import product as iter_product

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet0"

    specs = req.specs if req.specs else {}
    print(f"[SKU_EXCEL] 收到specs: {list(specs.keys()) if specs else 'EMPTY'}, skus: {len(req.skus)}")

    # 如果没有specs，从SKU名称推断
    if not specs:
        spec_columns = {}
        for sku in req.skus:
            name = sku.get("name", "")
            parts = name.split(" ", 1) if " " in name else [name]
            for i, part in enumerate(parts):
                part = part.strip()
                if i not in spec_columns:
                    spec_columns[i] = []
                if part and part not in spec_columns[i]:
                    spec_columns[i].append(part)
        for i in sorted(spec_columns.keys()):
            specs[f"规格{i+1}"] = spec_columns[i]

    # 排序：颜色分类在前，尺码在后
    size_keywords = ['尺码', '尺寸', '大小', '型号', '规格']
    color_keywords = ['颜色', '颜色分类', '款式', '风格']
    size_cols = [k for k in specs.keys() if any(s in k for s in size_keywords)]
    color_cols = [k for k in specs.keys() if any(c in k for c in color_keywords)]
    other_cols = [k for k in specs.keys() if k not in size_cols and k not in color_cols]
    spec_col_names = color_cols + size_cols + other_cols

    # 拼多多格式：前2行说明，第3行空，第4行表头
    ws.append([None] * (len(spec_col_names) + 5))
    ws.append(["a. 状态为\"0\"（未启用）的行数更新的库存数据无法生效！请商家设置状态为\"1\"或者在商品编辑页单独编辑！\r\nb. \"库存\"前几列为您的商品的规格，规格值仅供对照，不可编辑！"] + [None] * (len(spec_col_names) + 4))
    ws.append([None] * (len(spec_col_names) + 5))

    # 表头：规格列 + 库存 + 拼单价 + 单买价 + 规格编码 + 启用状态
    header_names = [_spec_display_name(n) for n in spec_col_names]
    header = header_names + ["库存", "拼单价(元)", "单买价(元)", "规格编码", "启用状态"]
    ws.append(header)

    # 构建价格映射：从SKU中提取 {规格值组合 → price}
    price_map = {}
    for sku in req.skus:
        sku_name = sku.get("name", "")
        price_map[sku_name] = {
            "price": sku.get("price", 0),
            "single_price": sku.get("single_price", 0),
            "stock": sku.get("stock"),
            "enabled": sku.get("enabled", True)
        }

    # 笛卡尔积展开：每个规格组合一行
    import re as _re
    size_keywords = ['尺码', '尺寸', '大小', '型号', '规格']

    def _clean_spec_value(val, col_name):
        """清洗规格值：尺码只保留S/M/L/XL等核心词"""
        if not val:
            return val
        if any(s in col_name for s in size_keywords):
            m = _re.match(r'^([0-9]*[SMLXLsmlxl]+)', val.strip())
            if m:
                return m.group(1)
        return val

    if spec_col_names:
        # 按「词」匹配 SKU，不能用子串：'L' in '黑色 XL' 会命中，某个颜色缺某个尺码时
        # 那一行就会静默套上别的尺码的价格/库存（尺码 S/M/L/XL 里 L 是 XL 的子串）
        from one_click_list import _sku_words, _words_contain
        _norm = lambda x: ' '.join(str(x or '').split())   # noqa: E731
        col_values = [[_clean_spec_value(v, name) for v in specs[name]] for name in spec_col_names]
        row_seq = 1
        for combo in iter_product(*col_values):
            combo_vals = [str(v) for v in combo if v]
            # 先按完整 SKU 名精确匹配，再退化为「每个规格值都是独立的词」
            matched_sku = next((s for s in req.skus
                                if _norm(s.get("name", "")) == _norm(' '.join(combo_vals))), None)
            if matched_sku is None:
                for sku in req.skus:
                    words = _sku_words(sku.get("name", ""))
                    if all(_words_contain(words, _sku_words(v)) for v in combo_vals):
                        matched_sku = sku
                        break

            price = matched_sku.get("price", 0) if matched_sku else ""
            single_price = matched_sku.get("single_price", 0) if matched_sku else ""
            stock = matched_sku.get("stock") if matched_sku else None
            enabled = "1" if (matched_sku.get("enabled", True) if matched_sku else True) else "0"
            # 规格编码：直接按行号自动生成 1,2,3,4...
            spec_code = str(matched_sku.get("spec_code")) if matched_sku and matched_sku.get("spec_code") not in (None, "") else str(row_seq)

            row = list(combo) + [stock, price or "", single_price or "", spec_code, enabled]
            ws.append(row)
            row_seq += 1

    # 设置列宽
    for i in range(len(spec_col_names)):
        ws.column_dimensions[chr(65 + i)].width = 20
    ws.column_dimensions[chr(65 + len(spec_col_names))].width = 10     # 库存
    ws.column_dimensions[chr(65 + len(spec_col_names) + 1)].width = 12  # 拼单价
    ws.column_dimensions[chr(65 + len(spec_col_names) + 2)].width = 12  # 单买价
    ws.column_dimensions[chr(65 + len(spec_col_names) + 3)].width = 15  # 规格编码
    ws.column_dimensions[chr(65 + len(spec_col_names) + 4)].width = 10  # 启用状态

    # 保存
    filename = f"sku_table_{uuid.uuid4().hex[:6]}.xlsx"
    filepath = DATA_DIR / filename
    wb.save(str(filepath))

    return {"success": True, "filename": filename, "url": f"/api/download-sku-excel/{filename}"}


@app.get("/api/download-sku-excel/{filename}", dependencies=[Depends(require_license)])
async def download_sku_excel(filename: str):
    """下载SKU Excel文件。

    只认 data 目录下的文件名：路径里带分隔符或 .. 的一律拒掉
    （Windows 上 %5C 解出来就是反斜杠，`..\\..\\keygen\\private_key.pem` 能被读走）。
    """
    if filename != Path(filename).name or not filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=404, detail="文件不存在")
    filepath = DATA_DIR / filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(
        str(filepath),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename
    )


@app.post("/api/download-pdd-excel", dependencies=[Depends(require_license)])
async def download_pdd_excel():
    """从拼多多页面下载规格Excel（通过CDP）

    这里原来是 new_event_loop + run_until_complete —— 但本函数自己就跑在 uvicorn 的事件
    循环里，CPython 会直接抛「Cannot run the event loop while another loop is running」，
    所以这个接口每次调用都是 500（前端还把这个错吞掉了，看起来像「点了没反应」）。
    它本来就是 async，直接 await 就行。
    """
    try:
        return await _download_from_pdd()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def _download_from_pdd():
    """通过CDP从拼多多Excel批量编辑下载表格"""
    import websockets
    import urllib.request

    tabs = json.loads(urllib.request.urlopen('http://127.0.0.1:9222/json', timeout=10).read())
    pdd_tab = None
    # 优先找goods/category（新发布页），其次找goods_add
    for t in tabs:
        url = t.get('url', '')
        if 'goods/category' in url and 'goods_id' not in url:
            pdd_tab = t
            break
    if not pdd_tab:
        for t in tabs:
            if 'goods/category' in t.get('url', '') or 'goods_add' in t.get('url', ''):
                pdd_tab = t
                break
    if not pdd_tab:
        raise Exception('PDD商品编辑页未找到')

    from one_click_list import CDPClient
    ws = await websockets.connect(pdd_tab['webSocketDebuggerUrl'], max_size=50*1024*1024)
    cdp = CDPClient(ws)
    await cdp.start()

    try:
        # 点击"Excel批量编辑规格"
        await cdp.js('''
        (() => {
            for (let el of document.querySelectorAll("a")) {
                if ((el.innerText||"").trim().includes("Excel批量编辑规格")) {
                    el.click();
                    return "CLICKED";
                }
            }
        })()
        ''')
        await asyncio.sleep(3)

        # 启用网络监听（事件统一由 CDPClient 的接收循环收集到 cdp.events，
        # 不再另起一个 reader 和它争抢同一条 WebSocket，否则响应与事件会互相丢失）
        await cdp.call('Network.enable')
        events_start = len(cdp.events)

        # 点击"导出当前规格数据"
        await cdp.js('''
        (() => {
            for (let el of document.querySelectorAll("button")) {
                if ((el.innerText||"").trim().includes("导出当前规格数据")) {
                    el.click();
                    return "CLICKED";
                }
            }
        })()
        ''')

        await asyncio.sleep(5)

        # 获取下载URL（只扫描点击导出之后新到达的事件，避免误用旧事件）
        download_url = None
        for ev in cdp.events[events_start:]:
            params = ev.get('params', {})
            req = params.get('request', {})
            if 'downloadExcel' in req.get('url', ''):
                request_id = params.get('requestId')
                result = await cdp.call('Network.getResponseBody', {'requestId': request_id})
                if result and 'result' in result:
                    body = result['result'].get('body', '')
                    data = json.loads(body)
                    if data.get('success') and data.get('result', {}).get('url'):
                        download_url = data['result']['url']
                break

        if not download_url:
            raise Exception("未能获取下载URL")

        # 下载Excel到data目录。
        # 必须带超时 + 丢到线程里：这里是 async 函数，同步下载会把整个 web 服务卡住
        # （前端所有请求一起转圈），而 urlretrieve 连超时参数都没有。
        filename = "pdd_spec_export.xlsx"
        filepath = DATA_DIR / filename

        def _fetch():
            from safe_http import download_bytes
            return download_bytes(download_url, timeout=30, max_bytes=8 * 1024 * 1024,
                                  headers={'Referer': 'https://mms.pinduoduo.com/'})

        filepath.write_bytes(await asyncio.to_thread(_fetch))

        return {
            "success": True,
            "filename": filename,
            "url": f"/api/download-sku-excel/{filename}"
        }
    finally:
        await ws.close()


@app.post("/api/upload-sku-excel", dependencies=[Depends(require_license)])
async def upload_sku_excel(file: UploadFile = File(...)):
    from safe_http import read_upload
    from sku_workbook import parse_workbook
    try:
        content=await read_upload(file,8*1024*1024)
        skus=parse_workbook(content)
        return {'success':True,'skus':skus,'count':len(skus)}
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
    except Exception as exc:
        raise HTTPException(422,'Invalid workbook; use an exported SKU workbook') from exc


if __name__ == "__main__":
    # 只监听本机（跟 launcher.py 一致）。原来写的是 0.0.0.0：那三个 .bat
    # （启动.bat / 一键启动.bat / start.bat）都是 `python web/app.py` 起的，
    # 一开就是全网卡 —— 同局域网任何人都能直接读 /data 下的图、日志，配合接口漏洞
    # 甚至能读走私钥。这工具只给本机用，不需要对外监听。
    uvicorn.run(app, host="127.0.0.1", port=3000)
