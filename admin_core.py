"""
授权管理核心逻辑（发码 / 查码 / 记账 / 统计 / 导出）。

「管理端」（总控台）和独立的「发码工具」共用这一份逻辑，避免两处代码不一致。

私钥 keygen/private_key.pem 是总钥匙：只在这份代码里读取，绝不外传。
"""
import re
import time
import base64
import sqlite3
import calendar
import csv
import io
import sys
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from asset_store import (AssetKeyMissing, b32_seed, new_seed, read_admin_seed,
                         seed_from_b32, write_pack)

_PREFIX = "PDD-"
_SIG_LEN = 64


def _base_dir() -> Path:
    """程序根目录：打包后为 exe 所在目录，开发态为本文件所在目录（项目根）。"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = _base_dir()
KEY_PATH = BASE_DIR / 'keygen' / 'private_key.pem'
DB_PATH = BASE_DIR / 'keygen' / 'licenses.db'


def _load_key() -> Ed25519PrivateKey:
    if not KEY_PATH.exists():
        raise RuntimeError(f'未找到私钥文件 {KEY_PATH}，请先备份/放置私钥')
    return serialization.load_pem_private_key(KEY_PATH.read_bytes(), password=None)


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return closing(conn)


def init_db():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS issued (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                machine_id TEXT NOT NULL,
                code TEXT NOT NULL,
                expiry TEXT NOT NULL,
                months INTEGER NOT NULL,
                days INTEGER NOT NULL DEFAULT 0,
                quota INTEGER NOT NULL DEFAULT 1000,
                amount REAL NOT NULL DEFAULT 0,
                note TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                seed TEXT,
                created_at TEXT NOT NULL
            )
        """)
        cols = [r['name'] for r in conn.execute('PRAGMA table_info(issued)').fetchall()]
        if 'quota' not in cols:
            conn.execute('ALTER TABLE issued ADD COLUMN quota INTEGER NOT NULL DEFAULT 1000')
        if 'days' not in cols:
            conn.execute('ALTER TABLE issued ADD COLUMN days INTEGER NOT NULL DEFAULT 0')
        if 'amount' not in cols:
            conn.execute('ALTER TABLE issued ADD COLUMN amount REAL NOT NULL DEFAULT 0')
        if 'status' not in cols:
            conn.execute("ALTER TABLE issued ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        # 2026-09-28 下午：一码一份的资产种子。**必须记在本机账本里** —— 续期换了到期日
        # 要重签同一张码，种子得原样带上，否则租户手上那个资产包会突然失效（白跑一趟）。
        if 'seed' not in cols:
            conn.execute('ALTER TABLE issued ADD COLUMN seed TEXT')
        conn.commit()


# 只有「管理端」环境（私钥存在）才建库；用户端没有私钥，导入本模块时不会在用户目录里
# 留下 keygen/licenses.db 空文件，也不会因为缺 keygen/ 目录而报错。
if KEY_PATH.exists():
    init_db()


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def _add_duration(d: date, months: int, days: int) -> date:
    """先按日历加月，再往后加零头天数（与 keygen/keygen.py 保持一致）。

    半个月 = 0月15天；一个半月 = 1月15天。先加月再加天，整月行为与以前完全相同。
    """
    return _add_months(d, months) + timedelta(days=days)


def fmt_duration(months: int, days: int) -> str:
    """时长显示：0月15天 → 「15天」；1月15天 → 「1月15天」。"""
    m, d = int(months or 0), int(days or 0)
    if m <= 0 and d <= 0:
        return '-'
    return (f'{m}月' if m else '') + (f'{d}天' if d else '')


def format_mid(mid: str) -> str:
    """16 位 hex 机器码 → 带横线的显示格式。"""
    mid = (mid or '').replace('-', '').upper()
    if len(mid) != 16:
        return mid
    return '-'.join(mid[i:i + 4] for i in range(0, 16, 4))


def _sign_code(mid: str, exp_date: date, quota: int, expire_at: str = '',
               seed: bytes | None = None) -> str:
    """用私钥签一个激活码。mid 为 16 位 hex（无横线）。

    载荷字段位置固定（空也留位）：机器码|到期日|月限额|分钟级到期|资产种子。
    第5段是这张码**专属**的资产种子（2026-09-28 下午改的，见 asset_store.py 开头）：
    一码一份，租户拿它解**自己那个**资产包。不传就现随机一段 —— 续期/换发时要把
    老种子原样传进来，否则租户手上的资产包会突然解不开。
    """
    key = _load_key()
    seed = seed if seed else new_seed()
    payload = f"{mid}|{exp_date.strftime('%Y%m%d')}|{quota:06d}|{expire_at}|{b32_seed(seed)}"
    sig = key.sign(payload.encode('ascii'))
    data = payload.encode('ascii') + sig
    return _PREFIX + base64.b32encode(data).decode().rstrip('=')


def row_seed(row) -> bytes | None:
    """账本里那条记录的种子（老记录没有 → None）。"""
    try:
        return seed_from_b32(row['seed'])
    except Exception:
        return None


def make_pack_for(mid: str, seed: bytes) -> tuple[str, str]:
    """给这台机器出资产包，返回 (文件路径, 出错说明)。发码后就要发给租户。

    宁可不发码也不能发一张装不了资产的码，所以这里出错要一路报上去给界面看。
    """
    try:
        fp = write_pack(mid, seed)
        return str(fp), ''
    except Exception as e:
        return '', f'{e}'


def _decode_code(code: str) -> dict:
    """解析激活码（只读，不造码），返回机器码/到期日/月限额/签名是否有效。"""
    raw = (code or '').strip()
    if raw.upper().startswith(_PREFIX):
        raw = raw[len(_PREFIX):]
    try:
        pad = '=' * ((8 - len(raw) % 8) % 8)
        data = base64.b32decode(raw.upper() + pad)
    except Exception:
        raise ValueError('激活码格式不对')
    if len(data) <= _SIG_LEN:
        raise ValueError('激活码格式不对')
    payload, sig = data[:-_SIG_LEN], data[-_SIG_LEN:]

    valid = True
    try:
        _load_key().public_key().verify(sig, payload)
    except Exception:
        valid = False

    try:
        parts = payload.decode('ascii').split('|')
        mid = parts[0]
        expiry_raw = parts[1]
        quota = int(parts[2]) if len(parts) >= 3 and parts[2] else 0
        expire_at_raw = parts[3] if len(parts) >= 4 and parts[3] else ''
    except Exception:
        raise ValueError('激活码内容解析失败')

    if len(expiry_raw) == 8 and expiry_raw.isdigit():
        expiry = f"{expiry_raw[:4]}-{expiry_raw[4:6]}-{expiry_raw[6:8]}"
    else:
        expiry = expiry_raw

    expire_at = None
    if expire_at_raw:
        try:
            expire_at = datetime.strptime(expire_at_raw, '%Y%m%d%H%M%S').isoformat()
        except Exception:
            expire_at = None

    return {
        'machine_id': format_mid(mid),
        'expiry': expiry,
        'expire_at': expire_at,
        'quota': quota,
        'valid': valid,
    }


def gen_code(machine_id: str, months: int = 1, days: int = 0, quota: int = 1000, minutes: int = 0,
             note: str = '', amount: float = 0.0) -> dict:
    """生成激活码并记入 DB，返回记录 dict。minutes>0 生成分钟级测试码（忽略时长）。

    时长按「月 + 天」：半个月 = months=0/days=15，一个半月 = months=1/days=15。
    """
    mid = (machine_id or '').strip().replace('-', '').upper()
    if not re.fullmatch(r'[0-9A-F]{16}', mid):
        raise ValueError('验证码格式不对，应为 16 位字符，例如 D8D0-F6A8-625C-3D1C')
    months = max(0, min(int(months or 0), 60))
    days = max(0, min(int(days or 0), 366))
    quota = max(1, min(int(quota), 100000))
    minutes = max(0, min(int(minutes), 60 * 24 * 7))
    amount = max(0.0, float(amount or 0))
    if minutes <= 0 and months <= 0 and days <= 0:
        raise ValueError('时长不能为 0，至少填 1 天（半个月填 0 月 15 天）')

    seed = new_seed()   # 一码一份：这张码专属的资产种子
    if minutes > 0:
        exp_at = datetime.now() + timedelta(minutes=minutes)
        exp_date = exp_at.date()
        expire_at = exp_at.strftime('%Y%m%d%H%M%S')
        code = _sign_code(mid, exp_date, quota, expire_at, seed)
        expiry = exp_date.isoformat()
    else:
        exp_date = _add_duration(date.today(), months, days)
        code = _sign_code(mid, exp_date, quota, seed=seed)
        expiry = exp_date.isoformat()
        expire_at = ''

    note = (note or '').strip()
    if minutes > 0:
        note = f"【测试码 {minutes}分钟】{note}".strip()

    pack, pack_err = make_pack_for(mid, seed)
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    with _conn() as conn:
        cur = conn.execute(
            'INSERT INTO issued (machine_id, code, expiry, months, days, quota, amount, note, status, seed, created_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (mid, code, expiry, months, days, quota, amount, note, 'active', b32_seed(seed), now),
        )
        conn.commit()
        rid = cur.lastrowid
    return {
        'id': rid, 'machine_id': format_mid(mid), 'code': code, 'expiry': expiry,
        'months': months, 'days': days, 'quota': quota, 'amount': amount, 'note': note,
        'status': 'active', 'created_at': now, 'expire_at': expire_at,
        'duration': fmt_duration(months, days),
        'pack': pack, 'pack_name': Path(pack).name if pack else '', 'pack_error': pack_err,
    }


def decode_code(code: str) -> dict:
    return _decode_code(code)


def _code_has_seed(code: str) -> bool:
    """这张码里带没带资产种子（= 装了新版用户端能不能干活）。老码返回 False。"""
    raw = (code or '').strip()
    if raw.upper().startswith(_PREFIX):
        raw = raw[len(_PREFIX):]
    try:
        data = base64.b32decode(raw.upper() + '=' * ((8 - len(raw) % 8) % 8))
        parts = data[:-_SIG_LEN].decode('ascii').split('|')
        return len(parts) >= 5 and bool(parts[4])
    except Exception:
        return False


def list_codes(status: str = None, q: str = None) -> list:
    """列表，可按状态过滤、按机器码/备注/激活码搜索。"""
    sql = 'SELECT * FROM issued'
    where, args = [], []
    if status:
        where.append('status=?')
        args.append(status)
    if q:
        like = f'%{q}%'
        where.append('(machine_id LIKE ? OR note LIKE ? OR code LIKE ?)')
        args += [like, like, like]
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY id DESC'
    with _conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d['machine_id'] = format_mid(d['machine_id'])
        d['has_seed'] = _code_has_seed(d['code'])
        # 种子不出这个函数：它是解资产包的那把钥匙，界面用不上（要出包走 regen_pack）。
        d.pop('seed', None)
        out.append(d)
    return out


def extend_code(record_id: int, months: int = 1, days: int = 0, amount: float = 0.0,
                note: str = '') -> dict:
    """续期：从原到期日（已过期则从今天）往后加「月 + 天」，生成新码并记入 DB。"""
    months = max(0, min(int(months or 0), 60))
    days = max(0, min(int(days or 0), 366))
    amount = max(0.0, float(amount or 0))
    if months <= 0 and days <= 0:
        raise ValueError('续期时长不能为 0，至少填 1 天（半个月填 0 月 15 天）')
    with _conn() as conn:
        row = conn.execute('SELECT * FROM issued WHERE id=?', (int(record_id),)).fetchone()
        if not row:
            raise ValueError('记录不存在')
        mid = row['machine_id']
        quota = row['quota']
        try:
            old = date.fromisoformat(row['expiry'])
        except Exception:
            old = date.today()
        new_expiry = _add_duration(max(old, date.today()), months, days)
        seed = row_seed(row) or new_seed()   # 续期沿用老种子 → 租户手上的资产包继续有效
        code = _sign_code(mid, new_expiry, quota, seed=seed)
        note = (note or '').strip() or f"续期自 #{record_id}"
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        cur = conn.execute(
            'INSERT INTO issued (machine_id, code, expiry, months, days, quota, amount, note, status, seed, created_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (mid, code, new_expiry.isoformat(), months, days, quota, amount, note, 'active',
             b32_seed(seed), now),
        )
        conn.commit()
        rid = cur.lastrowid
    pack, pack_err = make_pack_for(mid, seed)
    return {
        'id': rid, 'machine_id': format_mid(mid), 'code': code, 'expiry': new_expiry.isoformat(),
        'months': months, 'days': days, 'quota': quota, 'amount': amount, 'note': note,
        'status': 'active', 'created_at': now, 'expire_at': '',
        'duration': fmt_duration(months, days),
        'pack': pack, 'pack_name': Path(pack).name if pack else '', 'pack_error': pack_err,
    }


def reissue_code(record_id: int, note: str = '') -> dict:
    """换发（老码升级）：同一台机器、同一个到期日，重签一张带资产种子的码。

    为什么要这个：2026-09-28 之前的码没有第5段种子，装了新版用户端之后授权有效，
    但解不开随包的密文资产，上架功能用不了。换发**不追加时长**，只换钥匙 ——
    老租户的到期日一天不多、一天不少。
    """
    with _conn() as conn:
        row = conn.execute('SELECT * FROM issued WHERE id=?', (int(record_id),)).fetchone()
        if not row:
            raise ValueError('记录不存在')
        mid = row['machine_id']
        try:
            exp = date.fromisoformat(row['expiry'])
        except Exception:
            raise ValueError(f"这条记录的到期日读不出来：{row['expiry']!r}")
        months = row['months'] or 0
        days = row['days'] if 'days' in row.keys() else 0
        seed = row_seed(row) or new_seed()   # 老码没种子 → 这次给它一段新的（并重出资产包）
        code = _sign_code(mid, exp, row['quota'], seed=seed)
        note = (note or '').strip() or f'换发升级自 #{record_id}（到期日不变）'
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        cur = conn.execute(
            'INSERT INTO issued (machine_id, code, expiry, months, days, quota, amount, note, status, seed, created_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (mid, code, exp.isoformat(), months, days, row['quota'], 0.0, note, 'active',
             b32_seed(seed), now),
        )
        conn.commit()
        rid = cur.lastrowid
    pack, pack_err = make_pack_for(mid, seed)
    return {
        'id': rid, 'machine_id': format_mid(mid), 'code': code, 'expiry': exp.isoformat(),
        'months': months, 'days': days, 'quota': row['quota'], 'amount': 0.0, 'note': note,
        'status': 'active', 'created_at': now, 'expire_at': '', 'duration': '换发',
        'pack': pack, 'pack_name': Path(pack).name if pack else '', 'pack_error': pack_err,
    }


def regen_pack(record_id: int) -> dict:
    """重新给某条记录出一份资产包（租户把包弄丢了、或者当初没生成出来时用）。

    种子在账本里，所以不用重新发码；但**老记录（没种子）出不了包** —— 那种要先用「换发」。
    """
    with _conn() as conn:
        row = conn.execute('SELECT * FROM issued WHERE id=?', (int(record_id),)).fetchone()
    if not row:
        raise ValueError('记录不存在')
    seed = row_seed(row)
    if not seed:
        raise ValueError('这条是 2026-09-28 之前的老码（码里没有钥匙），请先点「换发」再出资产包')
    pack, pack_err = make_pack_for(row['machine_id'], seed)
    if pack_err:
        raise ValueError(f'资产包生成失败：{pack_err}')
    return {'id': int(record_id), 'machine_id': format_mid(row['machine_id']),
            'pack': pack, 'pack_name': Path(pack).name, 'pack_error': ''}


def delete_record(record_id: int) -> bool:
    with _conn() as conn:
        cur = conn.execute('DELETE FROM issued WHERE id=?', (int(record_id),))
        conn.commit()
        return cur.rowcount > 0


def update_record(record_id: int, note: str = None, amount: float = None, status: str = None) -> dict:
    fields, args = [], []
    if note is not None:
        fields.append('note=?')
        args.append(note)
    if amount is not None:
        fields.append('amount=?')
        args.append(max(0.0, float(amount)))
    if status is not None:
        status = 'active' if status not in ('active', 'void') else status
        fields.append('status=?')
        args.append(status)
    if not fields:
        raise ValueError('没有要更新的字段')
    args.append(int(record_id))
    with _conn() as conn:
        conn.execute(f'UPDATE issued SET {", ".join(fields)} WHERE id=?', args)
        conn.commit()
        row = conn.execute('SELECT * FROM issued WHERE id=?', (int(record_id),)).fetchone()
    d = dict(row) if row else {}
    if d:
        d['machine_id'] = format_mid(d['machine_id'])
    return d


def stats() -> dict:
    today = date.today()
    soon = today + timedelta(days=7)
    month = today.isoformat()[:7]
    with _conn() as conn:
        def one(sql, args=()):
            return conn.execute(sql, args).fetchone()[0]
        total = one('SELECT COUNT(*) FROM issued')
        active = one("SELECT COUNT(*) FROM issued WHERE status='active' AND expiry >= ?", (today.isoformat(),))
        expiring_soon = one(
            "SELECT COUNT(*) FROM issued WHERE status='active' AND expiry BETWEEN ? AND ?",
            (today.isoformat(), soon.isoformat()),
        )
        expired = one("SELECT COUNT(*) FROM issued WHERE status='active' AND expiry < ?", (today.isoformat(),))
        void = one("SELECT COUNT(*) FROM issued WHERE status='void'")
        total_revenue = one('SELECT COALESCE(SUM(amount),0) FROM issued')
        month_revenue = one('SELECT COALESCE(SUM(amount),0) FROM issued WHERE created_at LIKE ?', (month + '%',))
    return {
        'total': total,
        'active': active,
        'expiring_soon': expiring_soon,
        'expired': expired,
        'void': void,
        'total_revenue': total_revenue,
        'month_revenue': month_revenue,
    }


def export_csv() -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['ID', '验证码', '激活码', '到期日', '时长', '月限额', '金额(元)', '备注', '状态', '生成时间'])
    for r in list_codes():
        w.writerow([
            r['id'], format_mid(r['machine_id']), r['code'], r['expiry'],
            fmt_duration(r['months'], r.get('days')),
            r['quota'], r['amount'], r['note'] or '', r['status'], r['created_at'],
        ])
    return '﻿' + buf.getvalue()
