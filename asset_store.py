# -*- coding: utf-8 -*-
"""核心资产解密：注入 JS、规则片段（干活要用的那部分）。

为什么要多这一层（2026-09-28 加）：
    以前「怎么驱动拼多多页面」的 JS 全是明文常量，直接写在 one_click_list.py 里。
    PyInstaller 打的包能用 pyinstxtractor 解出来、反编译回源码 —— 谁拿到包，谁就有
    全套 JS：把授权判断删掉就能白用，抄去做竞品也照抄。把布尔值改成 True 这种改动，
    原来一个字节就能换全套功能。

    现在这些 JS 不在代码里，而在密文里。钥匙（32 字节）由一段 16 字节种子派生。
    于是「把授权判断改成 True」这条老路断了：判断能改，种子变不出来 —— AES-GCM 的
    密文解不开，JS 拿不到，上架流程跑不起来。想干活就得有码，想有码就得跟出租方要。

**一码一份（2026-09-28 下午改的，这条是整套防护的命门）**：
    第一版是「所有租户共用一份密文 + 一份全局种子」，结果就是个洞：**任何一张有效
    码都能解开随包的那份密文**（种子在每张码里都长一样）。有人把码发到群里，或者
    花一个月租金买一张，整套 JS 就全出来了。tools/crack_demo.py 把这条路演过一遍。

    现在改成一码一份：
      · 每发一张码，现随机生成 16 字节种子，只装在这张码里，同时记进账本（续期要复用）
      · 出租方用**这段种子**单独给这台机器打一个「资产包」packs/assets_<验证码>.zip
      · 用户端包里**不带** assets/ —— 分发出去的东西里根本没有可攻击的密文
    于是：码泄漏 → 没有对应密文，解不开；包泄漏 → 没有那张码里的种子，解不开。
    只有「码 + 包」同时到手才解得开，而那就等于租户自己的整套安装 —— 离线方案
    到这儿是尽头，再往下只能把最值钱的那步搬到服务器（业主已否掉）。

    出租方/管理端自己那台机器仍走「共用密文 + keygen/assets_seed.bin」这条路，
    而且 `_admin_seed()` 要求本机真的持有配套私钥，所以 assets/ 就算漏出去，
    没有私钥的人（= 所有租户）也解不开。

钥匙怎么取（都在内存里，不落盘）：
    · 出租方/开发机：本机持有配套私钥（license 的凭据校验通过）→ 读 keygen/assets_seed.bin
    · 租户：激活码里带的那段种子（license._verify 验签通过后取出来）→ 解自己的资产包

密文格式：nonce(12 字节) || AES-256-GCM 密文；附加认证数据 = 资产名。所以把 A 文件
换成 B 文件、或者改一个字节，都会解密失败并抛出可读错误，而不是悄悄崩。
资产包（packs/*.zip）里装的就是这样一份份 <资产名>.bin，整包解不开就整包拒绝。
"""
import base64
import io
import os
import sys
import zipfile
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def _base_dir() -> Path:
    """程序根目录：打包后是 exe 所在目录（跟 keygen/、data/、browsers/ 同一层）。

    不能用 __file__ 的父目录：打包后本模块在 PyInstaller 的归档里，__file__ 指向
    内部的 _internal/，而资产是构建脚本复制到 exe 旁边那一层（和 data/ 一样）。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = _base_dir()
ASSETS_DIR = BASE_DIR / "assets"          # 共用密文：只在出租方/管理端存在
PACKS_DIR = BASE_DIR / "packs"            # 一码一份的资产包（租户导入后落在这里）
ADMIN_SEED_FILE = BASE_DIR / "keygen" / "assets_seed.bin"   # 只在出租方/管理端存在
ASSETS_SRC_DIR = BASE_DIR / "assets_src"  # 明文资产：只在出租方源码目录存在
SEED_LEN = 16
ZIP_NAME = "assets_{mid}.zip"             # 发给租户的资产包长这样


def seed_paths() -> list:
    """种子可能在的位置（打包后 exe 旁边；开发态项目根）。第一个存在的生效。"""
    cands = [ADMIN_SEED_FILE]
    if getattr(sys, "frozen", False):
        cands.append(Path(__file__).resolve().parent / "keygen" / "assets_seed.bin")
    return cands


_KDF_SALT = b"pdd-listing-assets"
_KDF_INFO = b"assets-v1"

_CACHE: dict[str, str] = {}


class AssetKeyMissing(RuntimeError):
    """拿不到钥匙：没激活，激活码是旧版，或者资产包还没导入。"""


class AssetBroken(RuntimeError):
    """密文坏了 / 被改过 / 不属于这套钥匙 / 资产文件缺失。"""


class PackInvalid(RuntimeError):
    """导入的资产包不是给这台机器（这张码）的，或者文件本身坏了。"""


def key_from_seed(seed: bytes) -> bytes:
    """16 字节种子 → 32 字节 AES 钥匙。发码端和这里必须用同一套派生参数。"""
    if not seed:
        # 老码没有第5段。宁可在这一层报个明白错，也别让 HKDF 抛 TypeError 那种看不懂的
        raise AssetKeyMissing("这张激活码是旧版（不带钥匙），请让出租方换发一张新版码")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=_KDF_SALT,
                info=_KDF_INFO).derive(bytes(seed))


def new_seed() -> bytes:
    """发码时给这张码现随机一段种子（一码一份的起点）。"""
    return os.urandom(SEED_LEN)


def b32_seed(seed: bytes) -> str:
    """种子在激活码里的写法（base32，无填充，16 字节 → 26 个字符）。"""
    return base64.b32encode(bytes(seed)).decode().rstrip("=")


def seed_from_b32(text: str) -> bytes | None:
    """激活码里的种子字段 → 字节；长度不对就当没有。"""
    t = (text or "").strip().upper()
    if not t:
        return None
    try:
        raw = base64.b32decode(t + "=" * ((8 - len(t) % 8) % 8))
    except Exception:
        return None
    return raw if len(raw) == SEED_LEN else None


def compact_mid(machine_id: str) -> str:
    """验证码 → 16 位 hex（无横线）。资产包目录名用它。"""
    import re
    value = (machine_id or "").strip().replace("-", "").upper()
    if value and not re.fullmatch(r'[A-Z0-9]{8,64}', value):
        raise PackInvalid('设备编号格式无效')
    return value


def pack_dir(machine_id: str) -> Path:
    """这台机器导入的资产包解在哪。"""
    return PACKS_DIR / compact_mid(machine_id)


def _seed_file() -> Path | None:
    for fp in seed_paths():
        if fp.exists():
            return fp
    return None


def read_admin_seed() -> bytes:
    """发码时要用的种子（出租方本机）。"""
    fp = _seed_file()
    if fp is None:
        raise AssetKeyMissing(
            f"缺少 {ADMIN_SEED_FILE} —— 先在源码目录跑一次 python tools/enc_assets.py 生成")
    raw = fp.read_bytes()
    if len(raw) != SEED_LEN:
        raise AssetKeyMissing(f"{fp} 不是 {SEED_LEN} 字节，可能损坏")
    return raw


def _admin_seed() -> bytes | None:
    """出租方/开发机这条路：本机真的持有配套私钥才认（和授权那套是同一个凭据）。"""
    try:
        fp = _seed_file()
        if fp is None:
            return None
        import license
        if not license._admin_credential_ok():
            return None
        raw = fp.read_bytes()
        return raw if len(raw) == SEED_LEN else None
    except Exception:
        return None


def _license_seed() -> bytes | None:
    """租户这条路：激活码里带的那段种子（一码一份）。"""
    try:
        import license
        return license.asset_seed()
    except Exception:
        return None


def seed_source() -> str:
    """种子是从哪儿来的（排查用）：'admin' / 'license' / ''。"""
    if _admin_seed():
        return "admin"
    if _license_seed():
        return "license"
    return ""


def _license_mid() -> str:
    """当前激活码绑的那台机器（用来找它自己的资产包目录）。"""
    try:
        import license
        return license.LICENSE_STATE.get("machine_id") or ""
    except Exception:
        return ""


def _encrypt(text: str, key: bytes, name: str) -> bytes:
    """一段文本 → nonce(12) || 密文；AAD = 资产名（换文件、改字节都解不开）。"""
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, text.encode("utf-8"), name.encode("utf-8"))


def _decrypt(blob: bytes, key: bytes, name: str) -> str:
    if len(blob) <= 12:
        raise AssetBroken(f"资产文件损坏：{name}")
    try:
        return AESGCM(key).decrypt(blob[:12], blob[12:], name.encode("utf-8")).decode("utf-8")
    except InvalidTag:
        raise AssetBroken(f"资产解密失败（文件被改过，或者不属于这套钥匙）：{name}")


# ---------- 一码一份的资产包 ----------

def src_texts() -> dict:
    """明文资产 {名字: 文本}：只在出租方源码目录有 assets_src/。"""
    if not ASSETS_SRC_DIR.exists():
        return {}
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(ASSETS_SRC_DIR.glob("*.txt"))}


def shared_texts() -> dict:
    """从随包的共用密文 assets/ 解出全部资产（要求本机持有私钥 = 出租方/管理端）。"""
    seed = _admin_seed()
    if not seed:
        raise AssetKeyMissing("本机没有出租方凭据，读不了 assets/（这是正常的：租户走资产包）")
    key = key_from_seed(seed)
    out = {}
    for fp in sorted(ASSETS_DIR.glob("*.bin")):
        out[fp.stem] = _decrypt(fp.read_bytes(), key, fp.stem)
    return out


def make_pack_bytes(seed: bytes, texts: dict | None = None) -> bytes:
    """按这段种子打一个资产包（zip：里面是一份份 <资产名>.bin）。返回 zip 字节。"""
    texts = texts if texts is not None else (src_texts() or shared_texts())
    if not texts:
        raise AssetKeyMissing("找不到明文资产（assets_src/ 和 assets/ 都没有）—— 先跑 tools/enc_assets.py")
    key = key_from_seed(seed)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(texts):
            z.writestr(f"{name}.bin", _encrypt(texts[name], key, name))
    return buf.getvalue()


def write_pack(mid: str, seed: bytes, out_dir: Path | None = None,
               texts: dict | None = None) -> Path:
    """给这台机器出资产包，落到 out_dir/（默认 packs/）。返回文件路径。"""
    mid = compact_mid(mid)
    out_dir = Path(out_dir) if out_dir else PACKS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    fp = out_dir / ZIP_NAME.format(mid=mid)
    fp.write_bytes(make_pack_bytes(seed, texts))
    return fp


def pack_root(seed: bytes, mid: str) -> Path:
    """租户这台机器的资产包目录（先看有没有导入过）。"""
    return pack_dir(mid)


def pack_ready(mid: str | None = None) -> bool:
    """这台机器导入资产包了吗（界面提示用）。"""
    mid = compact_mid(mid or _license_mid())
    if not mid:
        return False
    d = pack_dir(mid)
    return d.exists() and any(d.glob("*.bin"))


def import_pack(data: bytes, mid: str) -> int:
    """租户导入出租方发来的资产包：**先整包验签解密，全对才落盘**。

    只要有一份解不开，就说明这包不是给这台机器的（或者文件坏了）—— 整包拒绝，
    绝不留下半份能用的东西。返回导入的资产份数。
    """
    if len(data) > 8 * 1024 * 1024:
        raise PackInvalid('资产包超过8MB限制')
    mid = compact_mid(mid)
    seed = _license_seed()
    if not seed:
        raise PackInvalid("还没激活（或者激活码是旧版），先激活再导入资产包")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise PackInvalid("这不是一个资产包（应该是一个 zip 文件）")

    key = key_from_seed(seed)
    known = set(names()) or set(src_names())
    staged: dict[str, bytes] = {}
    with zf:
        infos = zf.infolist()
        if len(infos) > 100 or sum(info.file_size for info in infos) > 16 * 1024 * 1024:
            raise PackInvalid('资产包条目或解压总量超过限制')
        seen = set()
        for info in infos:
            if info.is_dir():
                continue
            entry = os.path.basename(info.filename.replace("\\", "/"))
            if entry != info.filename or info.file_size > 1024 * 1024 or info.file_size > max(info.compress_size, 1) * 100:
                raise PackInvalid('资产包路径、单文件大小或压缩比例无效')
            if not entry.endswith(".bin"):
                raise PackInvalid('资产包包含无效文件')
            name = entry[:-4]
            if not name or name in seen:
                raise PackInvalid('资产包包含重复文件')
            seen.add(name)
            if known and name not in known:
                raise PackInvalid(f"资产包里有一份不认识的资产：{entry}")
            blob = zf.read(info)
            try:
                _decrypt(blob, key, name)
            except AssetBroken:
                raise PackInvalid(
                    "这个资产包解不开 —— 它不是发给这台机器的（或者下载时损坏了）。"
                    "请让出租方重新发一份。")
            staged[name] = blob
    if not staged:
        raise PackInvalid("资产包里一份资产都没有")

    d = pack_dir(mid)
    d.mkdir(parents=True, exist_ok=True)
    for name, blob in staged.items():
        (d / f"{name}.bin").write_bytes(blob)
    clear_cache()
    return len(staged)


# ---------- 取用 ----------

def asset_path(name: str) -> Path:
    """这台机器该用的那份密文：优先自己的资产包，其次出租方的共用密文。"""
    mid = _license_mid()
    if mid:
        fp = pack_dir(mid) / f"{name}.bin"
        if fp.exists():
            return fp
    return ASSETS_DIR / f"{name}.bin"


def _key_for(path: Path) -> bytes:
    """按密文所在的位置决定用哪把钥匙（两把钥匙走两条完全不同的来路）。"""
    if PACKS_DIR in path.parents:
        seed = _license_seed()
        if not seed:
            raise AssetKeyMissing("请先激活（输入出租方发回的激活码），再用上架功能。")
        return key_from_seed(seed)
    seed = _admin_seed()
    if not seed:
        raise AssetKeyMissing("请先激活（输入出租方发回的激活码），再用上架功能。")
    return key_from_seed(seed)


def load(name: str) -> str:
    """取一段资产（解出来是文本，UTF-8）。同一段只解一次，之后走内存缓存。

    租户这条路**只认自己的资产包**：手里有码的种子却没有包，就直接报「去导入资产包」，
    绝不悄悄退回随包的共用密文 —— 那份密文本来就不该发给租户（见文件开头）。
    """
    if name in _CACHE:
        return _CACHE[name]
    mid = compact_mid(_license_mid())
    seed = _license_seed()
    fp = (pack_dir(mid) / f"{name}.bin") if mid else None
    if fp is not None and fp.exists():
        if seed is None:
            # 机器上留着旧资产包，但这张码是旧版（或者刚被换成没钥匙的码）——
            # 这时候手上那把钥匙已经对不上了，得重新换发 + 重发包。
            raise AssetKeyMissing(
                "当前这张激活码是旧版（不带钥匙），解不开机器上已导入的资产包。"
                "请让出租方「换发」一张新版激活码，并把新的资产包一起发过来。")
        text = _decrypt(fp.read_bytes(), key_from_seed(seed), name)
    elif seed is not None:
        raise AssetKeyMissing(
            "这台机器还没导入「资产包」。请把验证码发给出租方，"
            "拿到 assets_%s.zip 后在工具页「📦 资产包」那里导入，再重开工具。" % (mid or "验证码"))
    else:
        fp = ASSETS_DIR / f"{name}.bin"
        if not fp.exists():
            raise AssetBroken(f"资产文件缺失：{fp}（打包前要先跑 tools/enc_assets.py）")
        text = _decrypt(fp.read_bytes(), _key_for(fp), name)
    _CACHE[name] = text
    return text


def names() -> list:
    """可用的资产名单：自己的资产包 + 随包的共用密文，两处合起来。"""
    got = set()
    mid = compact_mid(_license_mid())
    for d in (pack_dir(mid) if mid else None, ASSETS_DIR):
        if d and d.exists():
            got.update(p.stem for p in d.glob("*.bin"))
    return sorted(got)


def src_names() -> list:
    """明文资产名单（只在源码/出租方机器上有 assets_src/）。"""
    if not ASSETS_SRC_DIR.exists():
        return []
    return sorted(p.stem for p in ASSETS_SRC_DIR.glob("*.txt"))


def clear_cache() -> None:
    """换钥匙/换码/导入资产包之后要重解一遍。"""
    _CACHE.clear()
