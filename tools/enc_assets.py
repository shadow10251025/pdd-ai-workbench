# -*- coding: utf-8 -*-
"""把 assets_src/*.txt（明文 JS）加密成 assets/*.bin（随包分发）。

钥匙从哪来（见 asset_store.py 顶部的说明）：
    keygen/assets_seed.bin —— 16 字节随机种子。这个文件是**总钥匙的一半**：
      · 它 + 私钥 = 能给租户发「带钥匙的激活码」
      · 丢了它 = 所有已发的码和密文资产全废（只能重来一套）
      · 泄了它 = 拿到任意一张有效激活码的人本来就能解密，所以泄给外人等于送钥匙
    所以：本文件**永不覆盖已有的种子文件**，第一次生成后会打印两次提醒。

用法（每次打包前跑一次）：
    python tools/enc_assets.py            # 加密 + 自检（解一遍验证）
    python tools/enc_assets.py --check    # 只校验密文和明文是否对得上，不写文件
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import asset_store                                    # noqa: E402
from asset_store import ASSETS_DIR, ADMIN_SEED_FILE, SEED_LEN, key_from_seed  # noqa: E402

SRC_DIR = ROOT / "assets_src"
MANIFEST = SRC_DIR / "_manifest.json"


def ensure_seed() -> bytes:
    """读种子；第一次跑就生成一个。绝不覆盖 —— 覆盖等于把已发的码全废掉。"""
    if ADMIN_SEED_FILE.exists():
        raw = ADMIN_SEED_FILE.read_bytes()
        if len(raw) != SEED_LEN:
            raise SystemExit(f"{ADMIN_SEED_FILE} 是 {len(raw)} 字节，应该是 {SEED_LEN} 字节，已中止")
        return raw
    ADMIN_SEED_FILE.parent.mkdir(parents=True, exist_ok=True)
    raw = os.urandom(SEED_LEN)
    ADMIN_SEED_FILE.write_bytes(raw)
    print("=" * 72)
    print(f"已生成资产种子 → {ADMIN_SEED_FILE}")
    print("① 这个文件和私钥一样重要：丢了，所有已发的码都解不开资产；")
    print("   备份到你的 U 盘/网盘私人目录，别发给任何人、别放进用户端的包。")
    print("② 它只留在你这台机器上。用户端靠激活码里带的那段种子解资产。")
    print("=" * 72)
    return raw


def decrypt(blob: bytes, name: str, key: bytes) -> str:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce, ct = blob[:12], blob[12:]
    try:
        return AESGCM(key).decrypt(nonce, ct, name.encode("utf-8")).decode("utf-8")
    except InvalidTag:
        raise SystemExit(f"{name}: 解不开（密文和钥匙不匹配）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = ap.parse_args()

    if not SRC_DIR.exists():
        raise SystemExit(f"没有 {SRC_DIR} —— 先跑 python tools/extract_assets.py 把 JS 搬出来")
    srcs = sorted(p for p in SRC_DIR.glob("*.txt"))
    if not srcs:
        raise SystemExit(f"{SRC_DIR} 里没有 .txt")

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}
    if not manifest and not args.check:
        print(f"⚠️  没有 {MANIFEST.name}，跳过清单比对（照常加密）")

    key = key_from_seed(ensure_seed())

    # 明文变过就要重新生成 —— 顺手拿清单的 sha256 揪出来，别让旧密文混进包
    stale = []
    for fp in srcs:
        want = manifest.get(fp.stem, {}).get("sha256")
        got = hashlib.sha256(fp.read_bytes()).hexdigest()
        if want and want != got:
            stale.append(fp.stem)
    if stale:
        print(f"⚠️  {len(stale)} 份明文和清单对不上（明文被改过？）：{', '.join(stale)}")

    if args.check:
        bad = []
        for fp in srcs:
            target = ASSETS_DIR / f"{fp.stem}.bin"
            if not target.exists():
                bad.append(f"{fp.stem}: 密文不存在")
                continue
            if decrypt(target.read_bytes(), fp.stem, key) != fp.read_text(encoding="utf-8"):
                bad.append(f"{fp.stem}: 解出来和明文不一致")
        if bad:
            print("\n".join(bad))
            raise SystemExit(f"校验失败：{len(bad)} 份对不上")
        print(f"✅ 校验通过：{len(srcs)} 份密文都能解回原文")
        return 0

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ASSETS_DIR.mkdir(exist_ok=True)
    written, total_in, total_out = 0, 0, 0
    for fp in srcs:
        plain = fp.read_bytes()
        nonce = os.urandom(12)
        blob = nonce + AESGCM(key).encrypt(nonce, plain, fp.stem.encode("utf-8"))
        (ASSETS_DIR / f"{fp.stem}.bin").write_bytes(blob)
        written += 1
        total_in += len(plain)
        total_out += len(blob)

    # 明文没了、密文还在的（改名/删段之后留下的孤儿）要清掉，不然包里带着废文件
    src_names = {p.stem for p in srcs}
    orphans = [p for p in ASSETS_DIR.glob("*.bin") if p.stem not in src_names]
    for p in orphans:
        p.unlink()

    print(f"已加密 {written} 份：{total_in} → {total_out} 字节（AES-256-GCM，各带独立 nonce）→ {ASSETS_DIR}")
    if orphans:
        print(f"清掉 {len(orphans)} 份不再用的密文：{', '.join(p.stem for p in orphans)}")
    if src_names != set(asset_store.names()):
        raise SystemExit("密文名单和明文名单对不上，已中止")

    for fp in srcs:
        got = decrypt((ASSETS_DIR / f"{fp.stem}.bin").read_bytes(), fp.stem, key)
        if got != fp.read_text(encoding="utf-8"):
            raise SystemExit(f"{fp.stem}: 刚写出来的密文解回去不一致，已中止")
    print(f"✅ 自检通过：{written} 份密文当场解回原文，逐字一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
