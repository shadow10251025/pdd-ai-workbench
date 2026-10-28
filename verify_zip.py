# -*- coding: utf-8 -*-
"""校验打包出来的 zip 是不是完整的。

为什么需要：PowerShell 的 Compress-Archive 偶尔会写出「半个包」——
文件大小看着正常，实际缺结尾的中央目录（EOCD），解压时才报「压缩包损坏」。
2026-09-14 的 PDD_Listing_管理端.zip 就是这么来的：107.9 MB 却打不开。
构建脚本当时报「完成」，因为 Compress-Archive 自己返回了成功。

用法:
    python verify_zip.py <zip路径> [--forbid-secrets]

--forbid-secrets  用户端包用：包内出现私钥/发码库/登录态/激活记录 → 直接判失败
返回码 0 = 包是好的；非 0 = 包坏了或含敏感文件（构建脚本据此中止）。
"""
import os
import sys
import zipfile

# 用户端包里绝对不该有的东西（管理端包允许 keygen/，所以做成开关）
FORBIDDEN = (
    'private_key.pem',
    'licenses.db',
    'browser_data',
    'chrome_data',
    '.chrome_data',
    'license.json',
    'usage.json',
    'Cookies',
    'Login Data',
    # 2026-09-28 起：资产种子和明文资产是第二阶段防破解的命根子，漏一个出去，
    # 拿到包的人就能自己解出全部注入 JS（等于整套防破解白做）。
    'assets_seed',
    'assets_src',
    'bak_extract',
)


def verify(path, forbid_secrets=False):
    """返回 (是否通过, 说明文字列表)"""
    msgs = []
    if not os.path.exists(path):
        return False, [f'找不到 {path}']
    size = os.path.getsize(path)
    # zip 最小也就是一个 22 字节的 EOCD，比这还小肯定不是包
    if size < 22:
        return False, [f'只有 {size} 字节，连 zip 的结尾标记都放不下']

    # 1) 结尾必须有 EOCD（PK\x05\x06）。半个包就是缺这个。
    #    EOCD 在文件最后，前面可能带最多 64KB 的注释。
    with open(path, 'rb') as f:
        f.seek(max(0, size - 66000))
        tail = f.read()
    if b'PK\x05\x06' not in tail:
        return False, ['缺结尾标记（EOCD），是个没写完的坏包 —— 解压会报「压缩包损坏」']

    # 2) 中央目录能读出来，且每个文件的内容都完整（逐条 CRC 校验）
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            bad = z.testzip()
    except zipfile.BadZipFile as e:
        return False, [f'打不开：{e}']
    if bad:
        return False, [f'包内 {bad} 的内容已损坏']
    if not names:
        return False, ['包里一个文件都没有']

    msgs.append(f'包是完整的：{size / 1024 / 1024:.1f} MB，{len(names)} 个文件')

    # 3) 敏感文件自检（软性提示，不因为文件名里带关键词就误判整个包）
    hits = []
    for n in names:
        low = n.replace('\\', '/').lower()
        for pat in FORBIDDEN:
            if pat.lower() in low:
                hits.append(n)
                break
    if hits:
        if forbid_secrets:
            return False, msgs + [
                f'包内发现 {len(hits)} 个敏感文件，绝不能发出去：'] + [f'    {h}' for h in hits[:10]]
        msgs.append(f'提醒：包内有 {len(hits)} 个敏感文件（管理端包属于正常）：{hits[0]}'
                    + (f' 等 {len(hits)} 个' if len(hits) > 1 else ''))
    return True, msgs


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    path = argv[0]
    forbid = '--forbid-secrets' in argv
    ok, msgs = verify(path, forbid)
    for m in msgs:
        print(f'[校验] {m}')
    if not ok:
        print('[校验] ！！这个包不能用，已中止。请重新执行打包。')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
