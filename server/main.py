"""
拼多多一键上架 — 授权验证服务器

只做验码 / 存码 / 绑机器 / 吊销，SQLite 存储，无状态业务，极轻量。

启动（在 server/ 目录下）：
    uvicorn main:app --host 127.0.0.1 --port 8000

环境变量：
    ADMIN_PASSWORD   管理员密码（至少12位，未设置时禁止后台登录）
    DB_PATH          数据库文件路径（默认 server/licenses.db）
"""
import os
import sqlite3
import hashlib
import secrets
import time
import calendar
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ---------- 基础配置 ----------
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "licenses.db"))

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
_ADMIN_SALT = secrets.token_hex(16)
_ADMIN_HASH = hashlib.pbkdf2_hmac("sha256", ADMIN_PASSWORD.encode(), _ADMIN_SALT.encode(), 100000).hex()

SESSION_TTL = 24 * 3600  # 会话 token 有效期：24 小时
_sessions = {}  # token -> 过期时间戳(epoch)
_login_attempts = {}
import threading
_auth_lock = threading.Lock()

app = FastAPI(title="授权验证服务")


# ---------- 工具函数 ----------
def _conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return closing(conn)


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _add_months(d: date, months: int) -> date:
    """按自然月加 months 个月（自动处理月底日期）。"""
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


# 去掉易混字符 0/O、1/I
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _gen_code() -> str:
    chars = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(12))
    return "PDD-" + "-".join(chars[i:i + 4] for i in range(0, 12, 4))


def _result(ok: bool, reason: str, expiry: str | None = None) -> dict:
    return {"ok": ok, "reason": reason, "expiry": expiry}


def _norm(s: str) -> str:
    return (s or "").strip().upper()


def init_db():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS licenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                machine_id TEXT,
                expiry TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                activated_at TEXT,
                last_check_at TEXT
            )
        """)
        conn.commit()


init_db()


# ---------- 公开接口（客户端调用） ----------
class ActivateReq(BaseModel):
    code: str
    machine_id: str


class CheckReq(BaseModel):
    code: str
    machine_id: str


@app.post("/api/activate")
def activate(req: ActivateReq):
    """首次激活：码有效且未绑定则绑定本机。"""
    code = _norm(req.code)
    mid = (req.machine_id or "").strip()
    today = _today()
    now = _now()

    with _conn() as conn:
        row = conn.execute("SELECT * FROM licenses WHERE code=?", (code,)).fetchone()
        if row is None:
            return _result(False, "invalid")
        if row["status"] == "revoked":
            return _result(False, "revoked")
        if row["expiry"] < today:
            return _result(False, "expired")
        if row["machine_id"] is None:
            conn.execute(
                "UPDATE licenses SET machine_id=?, activated_at=?, last_check_at=? WHERE code=?",
                (mid, now, now, code),
            )
            conn.commit()
            return _result(True, "ok", row["expiry"])
        if row["machine_id"] == mid:
            conn.execute("UPDATE licenses SET last_check_at=? WHERE code=?", (now, code))
            conn.commit()
            return _result(True, "ok", row["expiry"])
        return _result(False, "bound_other")


@app.post("/api/check")
def check(req: CheckReq):
    """每次启动验证。"""
    code = _norm(req.code)
    mid = (req.machine_id or "").strip()
    today = _today()
    now = _now()

    with _conn() as conn:
        row = conn.execute("SELECT * FROM licenses WHERE code=?", (code,)).fetchone()
        if row is None:
            return _result(False, "invalid")
        if row["status"] == "revoked":
            return _result(False, "revoked")
        if row["expiry"] < today:
            return _result(False, "expired")
        if row["machine_id"] is None:
            return _result(False, "not_bound")
        if row["machine_id"] != mid:
            return _result(False, "machine_mismatch")
        conn.execute("UPDATE licenses SET last_check_at=? WHERE code=?", (now, code))
        conn.commit()
        return _result(True, "ok", row["expiry"])


# ---------- 后台鉴权 ----------
def _require_admin(authorization: str | None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = authorization[len("Bearer "):].strip()
    exp = _sessions.get(token)
    if exp is None or exp < time.time():
        raise HTTPException(status_code=401, detail="登录已过期")


class LoginReq(BaseModel):
    password: str


@app.post("/admin/login")
def login(req: LoginReq, request: Request):
    if not ADMIN_PASSWORD or len(ADMIN_PASSWORD) < 12:
        raise HTTPException(503, '管理员密码未配置或不足12位，登录暂不可用')
    address = request.client.host if request.client else 'unknown'
    with _auth_lock:
        now = time.time()
        attempts = [value for value in _login_attempts.get(address, []) if value > now - 300]
        if len(attempts) >= 8:
            raise HTTPException(429, '登录尝试过多，请5分钟后再试')
        _login_attempts[address] = attempts + [now]
    if len(req.password) > 256:
        raise HTTPException(401, '密码错误')
    h = hashlib.pbkdf2_hmac("sha256", req.password.encode(), _ADMIN_SALT.encode(), 100000).hex()
    if not secrets.compare_digest(h, _ADMIN_HASH):
        raise HTTPException(status_code=401, detail="密码错误")
    token = secrets.token_hex(32)
    _sessions[token] = time.time() + SESSION_TTL
    return {"ok": True, "token": token}


@app.post('/admin/logout-all')
def logout_all(authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    with _auth_lock:
        _sessions.clear()
    return {'ok': True}


@app.post("/admin/logout")
def logout(authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    token = authorization[len("Bearer "):].strip()
    _sessions.pop(token, None)
    return {"ok": True}


# ---------- 后台接口 ----------
class GenCodesReq(BaseModel):
    months: int = 1
    count: int = 1


@app.post("/admin/gen_codes")
def gen_codes(req: GenCodesReq, authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    months = max(1, min(req.months, 60))
    count = max(1, min(req.count, 100))
    expiry = _add_months(date.today(), months).isoformat()
    now = _now()
    codes = []
    with _conn() as conn:
        for _ in range(count):
            code = _gen_code()
            conn.execute(
                "INSERT INTO licenses (code, expiry, status, created_at) VALUES (?,?,?,?)",
                (code, expiry, "active", now),
            )
            codes.append({"code": code, "expiry": expiry})
        conn.commit()
    return {"ok": True, "codes": codes}


@app.get("/admin/licenses")
def list_licenses(authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM licenses ORDER BY id DESC").fetchall()
    return {"ok": True, "licenses": [dict(r) for r in rows]}


class CodeReq(BaseModel):
    code: str


@app.post("/admin/revoke")
def revoke(req: CodeReq, authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    code = _norm(req.code)
    with _conn() as conn:
        cur = conn.execute("UPDATE licenses SET status='revoked' WHERE code=?", (code,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="码不存在")
    return {"ok": True}


@app.post("/admin/unbind")
def unbind(req: CodeReq, authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    code = _norm(req.code)
    with _conn() as conn:
        cur = conn.execute("UPDATE licenses SET machine_id=NULL WHERE code=?", (code,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="码不存在")
    return {"ok": True}


class ExtendReq(BaseModel):
    code: str
    months: int = 1


@app.post("/admin/extend")
def extend(req: ExtendReq, authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    code = _norm(req.code)
    months = max(1, min(req.months, 60))
    with _conn() as conn:
        row = conn.execute("SELECT expiry FROM licenses WHERE code=?", (code,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="码不存在")
        base = max(date.today(), date.fromisoformat(row["expiry"]))
        new_expiry = _add_months(base, months).isoformat()
        conn.execute(
            "UPDATE licenses SET expiry=?, status='active' WHERE code=?",
            (new_expiry, code),
        )
        conn.commit()
    return {"ok": True, "expiry": new_expiry}


# ---------- 管理后台页面 ----------
@app.get("/admin")
def admin_page():
    return FileResponse(str(BASE_DIR / "admin.html"))


@app.get("/health")
def health():
    with _conn() as conn:
        ok = conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    if not ok:
        raise HTTPException(503, '数据库检查失败')
    return {"ok": True, 'admin_configured': bool(len(ADMIN_PASSWORD) >= 12), 'protocol': 'legacy-online-code-service'}
