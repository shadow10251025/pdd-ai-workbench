"""
拼多多一键上架 — 客户端授权模块（离线验证版）

不再联网。激活码是「出租方」用私钥签名后发来的，本模块用内置公钥离线验证：

    激活码 = "PDD-" + base32( 机器码(16位hex,无横线) + "|" + 到期日(8位YYYYMMDD) + "|" + 月限额(6位补零) + Ed25519签名(64字节) )

私钥只存在于出租方电脑的发码工具（keygen/）里，软件里只有公钥——所以即使
有人把软件整个破解，也只能「验证」码，永远「造不出」新码。

「月限额」= 该码在自然月内最多可上架的商品数量，写进签名、改不了；
「已上架多少款」的计数器存在本机（usage.json），懂行的人能删文件清零——只能挡普通用户。
"""
import os
import sys
import json
import time
import base64
import hashlib
from pathlib import Path
from datetime import date, datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

# 公钥（只能验码，不能造码；公开无害）
_PUBLIC_KEY_HEX = "bd308d18ac4e023643a006537439888b907453c3d0d31482e793e324cd0d4604"
_PUBLIC_KEY = Ed25519PublicKey.from_public_bytes(bytes.fromhex(_PUBLIC_KEY_HEX))

_PREFIX = "PDD-"
_SIG_LEN = 64  # Ed25519 签名固定 64 字节

# 管理端（总控台）模式：出租方自用，免激活码。
# 只有 launcher_admin.py 会把它置为 True；用户端永远保持 False。
#
# ⚠️ 注意：这个开关**单独改 True 没用**（老版本是这样，等于反编译后改一个字就全解锁）。
# 现在必须配合 _admin_credential_ok()：本机真的要持有那把配套私钥才算数。
ADMIN_MODE = False


def _get_base_dir() -> Path:
    """程序根目录：打包后为 exe 所在目录，开发态为项目根目录。"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = _get_base_dir()


# ---------- 管理端凭据（出租方身份） ----------
# 管理端免激活码，但不是「布尔开关」说了算：必须本机真的持有那把**配套**私钥 ——
# 拿它对随机挑战值签名，再用内置公钥验回来。私钥只在出租方自己的电脑上（管理端包里
# 带一份；用户端包 verify_zip.py 明令禁止带），所以租户反编译把 ADMIN_MODE 改 True
# 也过不了这一关：他手里没有私钥，现场生成一对新的也对不上内置公钥。
_ADMIN_CRED_OK = None  # True = 已确认持钥；失败不缓存（方便事后放私钥再试）


def _admin_key_path() -> Path:
    """与 admin_core.py 取同一个位置（打包后 = exe 目录下的 keygen/）。"""
    return BASE_DIR / "keygen" / "private_key.pem"


def _admin_credential_ok() -> bool:
    """本机是否持有配套私钥（= 出租方自己的机器 / 开发调试机）。"""
    global _ADMIN_CRED_OK
    if _ADMIN_CRED_OK:
        return True
    try:
        fp = _admin_key_path()
        if not fp.exists():
            return False
        from cryptography.hazmat.primitives import serialization
        priv = serialization.load_pem_private_key(fp.read_bytes(), password=None)
        challenge = os.urandom(32)
        # 关键：用内置公钥验回来 —— 现场自己生成一对密钥糊弄不过去
        _PUBLIC_KEY.verify(priv.sign(challenge), challenge)
        _ADMIN_CRED_OK = True
        return True
    except Exception:
        return False


def _admin_ok() -> bool:
    """管理端模式是否真的成立：声明了 ADMIN_MODE **且**确实持有配套私钥。"""
    return bool(ADMIN_MODE) and _admin_credential_ok()


def get_machine_id() -> str:
    """机器码：Windows MachineGuid + 网卡 MAC + 主机名 的哈希，稳定且跨机不同。"""
    parts = []

    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
        guid, _ = winreg.QueryValueEx(key, "MachineGuid")
        winreg.CloseKey(key)
        parts.append(str(guid))
    except Exception:
        pass

    try:
        import uuid
        parts.append(str(uuid.getnode()))
    except Exception:
        pass

    try:
        import socket
        parts.append(socket.gethostname())
    except Exception:
        pass

    raw = "|".join(parts) if parts else str(BASE_DIR)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest().upper()
    short = digest[:16]
    return "-".join(short[i:i + 4] for i in range(0, 16, 4))


def _license_file() -> Path:
    data_dir = BASE_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "license.json"


def load_saved_code() -> str:
    try:
        fp = _license_file()
        if fp.exists():
            data = json.loads(fp.read_text(encoding="utf-8"))
            return str(data.get("code", "")).strip()
    except Exception:
        pass
    return ""


def save_code(code: str):
    try:
        _license_file().write_text(json.dumps({"code": code}), encoding="utf-8")
    except Exception:
        pass


def _usage_file() -> Path:
    data_dir = BASE_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "usage.json"


def _load_usage() -> dict:
    try:
        fp = _usage_file()
        if fp.exists():
            return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"month": "", "used": 0}


def _save_usage(usage: dict):
    try:
        _usage_file().write_text(json.dumps(usage), encoding="utf-8")
    except Exception:
        pass


# ---------- 时钟回拨防护（高水位） ----------
# 每次验码都把「本机见过的最晚时间」记成高水位；下次若系统时间早于水位，
# 说明用户把系统时钟往回拨了（到期后改时间续用的常见手法），直接拦截。
# 注意：这只是给普通用户抬门槛——懂逆向的人能找到并删掉这些记录。要彻底防，
# 要么给代码加壳（PyArmor，让他读不到这个规律），要么改成在线验码。
_GUARD_FILE = ".syscache"   # 不起眼的文件名，降低被发现概率
_CLOCK_TOLERANCE = 60.0     # 允许 60 秒以内的正常时钟抖动
# 水位副本：另存一份到 %LOCALAPPDATA%，这样「把 data/ 整个删掉」也清不掉水位记录。
# 目录名取得像个显卡缓存（不惹眼），但**不冒充系统目录** —— 往系统目录里塞文件容易
# 被杀软盯上，得不偿失。
_MIRROR_DIR = "d3d_cache"
_MIRROR_FILE = ".bl"


def _guard_file() -> Path:
    data_dir = BASE_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / _GUARD_FILE


def _mirror_file() -> Path | None:
    """%LOCALAPPDATA% 下的水位副本路径（拿不到就返回 None，不影响主流程）。"""
    try:
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if not root:
            return None
        d = Path(root) / _MIRROR_DIR
        d.mkdir(parents=True, exist_ok=True)
        return d / _MIRROR_FILE
    except Exception:
        return None


def _read_one(fp: Path | None) -> float:
    try:
        raw = fp.read_text(encoding="utf-8").strip() if fp else ""
        return float(int(raw, 16)) if raw else 0.0
    except Exception:
        return 0.0


def _read_guard() -> float:
    """读高水位（16 进制存储，避免一眼看懂）：两个水位点里取更高的。"""
    return max(_read_one(_guard_file()), _read_one(_mirror_file()))


def _write_guard(value: float):
    for fp in (_guard_file(), _mirror_file()):
        try:
            if fp is None:
                continue
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(hex(int(value)), encoding="utf-8")
        except Exception:
            pass


def _ref_mtime_max() -> float:
    """license.json / usage.json 的最大修改时间，作为辅助水位。

    这俩文件是程序自己写、时间就是真实系统时间；即使删了 .syscache，
    只要没把这两个文件的时间也改掉，时钟回拨照样露馅。
    """
    latest = 0.0
    for name in ("license.json", "usage.json", _GUARD_FILE):
        try:
            latest = max(latest, (BASE_DIR / "data" / name).stat().st_mtime)
        except Exception:
            pass
    return latest


def _clock_rolled_back() -> bool:
    """检测系统时钟是否被回拨。回拨时返回 True（保持高水位不回写，持续拦截）。"""
    now = time.time()
    high = max(_read_guard(), _ref_mtime_max())
    if high and now < high - _CLOCK_TOLERANCE:
        return True
    if now > high:
        _write_guard(now)
    return False


def _verify(code: str, machine_id: str) -> dict:
    """离线验证激活码，返回 {ok, reason, expiry}。"""
    mid_nodash = machine_id.replace("-", "").upper()

    if not code or not code.strip().upper().startswith(_PREFIX):
        return {"ok": False, "reason": "invalid"}
    raw = code.strip()[len(_PREFIX):]

    # base32 解码（补回填充）
    try:
        pad = "=" * ((8 - len(raw) % 8) % 8)
        data = base64.b32decode(raw.upper() + pad)
    except Exception:
        return {"ok": False, "reason": "invalid"}

    if len(data) <= _SIG_LEN:
        return {"ok": False, "reason": "invalid"}
    payload, sig = data[:-_SIG_LEN], data[-_SIG_LEN:]

    # 验签：签名不对一律视为无效码
    try:
        _PUBLIC_KEY.verify(sig, payload)
    except (InvalidSignature, Exception):
        return {"ok": False, "reason": "invalid"}

    # 解析 payload：机器码|到期日|月限额[|分钟级到期][|资产钥匙]
    # （新码定长：到期日8位YYYYMMDD、月限额6位补零；兼容旧码）
    try:
        parts = payload.decode("ascii").split("|")
        mid = parts[0]
        expiry_raw = parts[1]
        quota = int(parts[2]) if len(parts) >= 3 and parts[2] else 0
        expire_at_raw = parts[3] if len(parts) >= 4 and parts[3] else ""
        asset_seed_raw = parts[4] if len(parts) >= 5 and parts[4] else ""
    except Exception:
        return {"ok": False, "reason": "invalid"}

    # 第5段（可选）：派生资产解密钥匙的种子（base32，16 字节 → 26 个字符）。
    # 老码没有这一段 —— 授权照样有效（租户是付过钱的），但解不开随包的密文资产，
    # 上架流程用不了；这时要提示「换一张新版激活码」（见 upgrade_needed）。
    asset_seed_bytes = None
    if asset_seed_raw:
        try:
            _pad = "=" * ((8 - len(asset_seed_raw) % 8) % 8)
            _raw = base64.b32decode(asset_seed_raw.upper() + _pad)
            asset_seed_bytes = _raw if len(_raw) == 16 else None
        except Exception:
            asset_seed_bytes = None

    # 到期日归一化成 YYYY-MM-DD（新码 8 位，旧码本身带横线）
    try:
        if len(expiry_raw) == 8 and expiry_raw.isdigit():
            expiry = f"{expiry_raw[:4]}-{expiry_raw[4:6]}-{expiry_raw[6:8]}"
        else:
            expiry = expiry_raw
        exp_date = date.fromisoformat(expiry)
    except Exception:
        return {"ok": False, "reason": "invalid"}

    # 分钟级到期（可选第4段，14位 YYYYMMDDHHMMSS，用于测试码）
    expire_at = None
    if expire_at_raw:
        try:
            expire_at = datetime.strptime(expire_at_raw, "%Y%m%d%H%M%S")
        except Exception:
            expire_at = None

    if mid.upper() != mid_nodash:
        return {"ok": False, "reason": "machine_mismatch"}
    if expire_at is not None and datetime.now() > expire_at:
        return {"ok": False, "reason": "expired", "expiry": expiry, "quota": quota,
                "expire_at": expire_at.isoformat(), "asset_seed": asset_seed_bytes}
    if exp_date < date.today():
        return {"ok": False, "reason": "expired", "expiry": expiry, "quota": quota,
                "asset_seed": asset_seed_bytes}
    return {"ok": True, "reason": "ok", "expiry": expiry, "quota": quota,
            "expire_at": expire_at.isoformat() if expire_at else None,
            "asset_seed": asset_seed_bytes}


# 模块级授权状态：launcher 与 web/app 同进程共享
LICENSE_STATE = {
    "ok": False,
    "reason": "unlicensed",  # unlicensed / ok / invalid / expired / machine_mismatch
    "expiry": None,
    "expire_at": None,  # 分钟级到期时间（ISO，测试码用）
    "quota": 0,  # 月限额（0 = 不限额）
    "code": "",
    "machine_id": "",
}

# 资产解密种子（16 字节）。**故意不放进 LICENSE_STATE**：那个字典会整个被接口
# 当 JSON 返回给前端，bytes 既序列化不了、也没必要往外给。只留内存，不落盘。
_ASSET_SEED: bytes | None = None


def _apply_asset_seed(res: dict) -> None:
    """把这次验码带出来的种子记下来；换了种子就清掉已解密的缓存。"""
    global _ASSET_SEED
    seed = res.get("asset_seed") if isinstance(res, dict) else None
    if seed != _ASSET_SEED:
        _ASSET_SEED = seed
        try:
            import asset_store
            asset_store.clear_cache()
        except Exception:
            pass


def asset_seed() -> bytes | None:
    """当前可用的资产解密种子（没有就返回 None：没激活，或激活码是旧版）。"""
    return _ASSET_SEED


def upgrade_needed() -> bool:
    """授权有效、但激活码是旧版（不带钥匙）→ 要换一张新版码才能用上架功能。"""
    return bool(LICENSE_STATE.get("ok")) and not _ASSET_SEED


def check_and_update(code: str | None = None, machine_id: str | None = None) -> dict:
    """校验本地已存的激活码并把结果写入 LICENSE_STATE，返回 LICENSE_STATE。"""
    if _admin_ok():
        # 能走到这儿 = 声明了管理端模式**并且**本机真的持有配套私钥；
        # 光把 ADMIN_MODE 改成 True 到不了这里，会掉到下面按普通用户验码
        machine_id = machine_id or get_machine_id()
        LICENSE_STATE.update({"ok": True, "reason": "ok", "code": "", "expiry": None,
                              "expire_at": None, "quota": 0, "machine_id": machine_id})
        _apply_asset_seed({})  # 管理端不用码里的种子，走 keygen/assets_seed.bin（见 asset_store）
        return LICENSE_STATE
    if _clock_rolled_back():
        LICENSE_STATE.update({"ok": False, "reason": "clock_rollback"})
        return LICENSE_STATE
    machine_id = machine_id or get_machine_id()
    code = code or load_saved_code()

    LICENSE_STATE["code"] = code
    LICENSE_STATE["machine_id"] = machine_id

    if not code:
        LICENSE_STATE.update({"ok": False, "reason": "unlicensed", "expiry": None, "expire_at": None, "quota": 0})
        _apply_asset_seed({})
        return LICENSE_STATE

    res = _verify(code, machine_id)
    LICENSE_STATE["ok"] = bool(res.get("ok"))
    LICENSE_STATE["reason"] = res.get("reason", "unknown")
    LICENSE_STATE["expiry"] = res.get("expiry")
    LICENSE_STATE["expire_at"] = res.get("expire_at")
    LICENSE_STATE["quota"] = res.get("quota", 0)
    _apply_asset_seed(res)
    return LICENSE_STATE


def activate_and_update(code: str, machine_id: str | None = None) -> dict:
    """激活：离线验证通过后写回本地 + 更新状态。返回 {ok, reason, expiry}。"""
    machine_id = machine_id or get_machine_id()
    res = _verify(code, machine_id)
    if res.get("ok"):
        save_code(code)
        LICENSE_STATE.update({
            "ok": True,
            "reason": "ok",
            "expiry": res.get("expiry"),
            "expire_at": res.get("expire_at"),
            "quota": res.get("quota", 0),
            "code": code,
            "machine_id": machine_id,
        })
    _apply_asset_seed(res if res.get("ok") else {})
    res.pop("asset_seed", None)  # 钥匙留在进程里就行，不往接口/前端给
    return res


def days_left() -> int | None:
    """距到期剩余天数；无到期信息返回 None。"""
    expiry = LICENSE_STATE.get("expiry")
    if not expiry:
        return None
    try:
        return (date.fromisoformat(expiry) - date.today()).days
    except Exception:
        return None


def get_quota() -> int:
    """当前激活码的月限额（0 = 不限额）。"""
    return int(LICENSE_STATE.get("quota") or 0)


def get_usage() -> dict:
    """返回 {used, quota, month}：本月已上架数、月限额、当前月份。"""
    month = date.today().isoformat()[:7]
    usage = _load_usage()
    if usage.get("month") != month:
        usage = {"month": month, "used": 0}
    return {"used": int(usage.get("used", 0)), "quota": get_quota(), "month": month}


def can_list() -> tuple:
    """上架前检查：本月额度是否用完。返回 (ok, reason)。"""
    quota = get_quota()
    if quota <= 0:
        return True, "ok"
    if get_usage()["used"] >= quota:
        return False, "quota_exceeded"
    return True, "ok"


def can_list_n(n: int) -> tuple:
    """一次上架 N 家店之前的额度检查。返回 (ok, reason, 还剩几次)。

    计费口径是**按店铺算**（2026-09-28 用户定）：一个商品上到 3 家店扣 3 次。
    额度不够就整单拒绝，不做「能上几家上几家」—— 那样用户会以为全上了。
    quota<=0 = 不限量（还剩几次返回 None）。
    """
    quota = get_quota()
    if quota <= 0:
        return True, "ok", None
    left = quota - get_usage()["used"]
    if left < n:
        return False, "quota_exceeded", left
    return True, "ok", left


def increment_usage():
    """上架成功后 +1（按自然月统计，跨月自动清零）。"""
    month = date.today().isoformat()[:7]
    usage = _load_usage()
    if usage.get("month") != month:
        usage = {"month": month, "used": 0}
    usage["used"] = int(usage.get("used", 0)) + 1
    _save_usage(usage)


def check_now() -> dict:
    """请求时重新校验当前激活码是否仍有效（含分钟级到期），返回 {ok, reason, ...}。

    与 check_and_update 不同：这里针对「运行中到期」——比如 2 分钟测试码在程序
    运行期间到点后，下一次受保护请求就会被拒。
    """
    if _admin_ok():
        return {"ok": True, "reason": "ok"}
    if _clock_rolled_back():
        LICENSE_STATE.update({"ok": False, "reason": "clock_rollback"})
        return {"ok": False, "reason": "clock_rollback"}
    code = LICENSE_STATE.get("code")
    if not code:
        LICENSE_STATE.update({"ok": False, "reason": "unlicensed"})
        return {"ok": False, "reason": "unlicensed"}
    res = _verify(code, LICENSE_STATE.get("machine_id") or get_machine_id())
    LICENSE_STATE["ok"] = bool(res.get("ok"))
    LICENSE_STATE["reason"] = res.get("reason", "unknown")
    LICENSE_STATE["expiry"] = res.get("expiry")
    LICENSE_STATE["expire_at"] = res.get("expire_at")
    LICENSE_STATE["quota"] = res.get("quota", 0)
    _apply_asset_seed(res if res.get("ok") else {})
    res.pop("asset_seed", None)
    return res


class NotLicensedError(RuntimeError):
    """核心功能入口的授权校验没通过（给调用方一个能识别的类型）。"""


_REASON_TEXT = {
    "unlicensed": "还没输入激活码",
    "invalid": "激活码无效",
    "expired": "激活码已到期，请续费",
    "machine_mismatch": "这个激活码不是本机的（一码一机）",
    "clock_rollback": "系统时间被往回拨过，请把时间调准再试",
    "quota_exceeded": "本月上架额度已用完",
    "unknown": "授权校验没通过",
}


def license_error_text(reason: str | None = None) -> str:
    """把 reason 翻成能给用户看的中文。"""
    reason = reason or LICENSE_STATE.get("reason") or "unknown"
    return _REASON_TEXT.get(reason, _REASON_TEXT["unknown"])


def guard(feature: str = "") -> None:
    """核心功能入口的运行时校验（纵深防御）。

    为什么不能只在 API 层拦：`Depends(require_license)` 是**一处 patch 通杀** ——
    把那个函数改成直接 return，所有接口一起失守。所以校验要下沉到真正干活的函数里
    （抓规格、上传预览图、填 Excel、填价格、传详情图），破解者得逐个找到并改掉。

    ⚠️ 这里**每次现算**，不读 LICENSE_STATE['ok'] 那个缓存值 —— 所以「把 ok 改成
    True」这类改法对下沉的检查完全无效，必须去动 _verify / 公钥那一层才行。

    本机持有配套私钥（出租方自己的机器、开发调试机）时直接放行。
    """
    if check_now().get("ok") or _admin_credential_ok():
        return
    raise NotLicensedError(license_error_text())


def minutes_left() -> int | None:
    """测试码剩余分钟数（有分钟级到期时返回，否则 None）。"""
    expire_at = LICENSE_STATE.get("expire_at")
    if not expire_at:
        return None
    try:
        dt = datetime.fromisoformat(expire_at)
        return int((dt - datetime.now()).total_seconds() // 60)
    except Exception:
        return None


def reset_usage():
    """清零本月已上架计数（测试额度用）。"""
    month = date.today().isoformat()[:7]
    _save_usage({"month": month, "used": 0})
