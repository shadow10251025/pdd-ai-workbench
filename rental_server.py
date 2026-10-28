"""Isolated rental API. It deliberately does not import the desktop admin app."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.datastructures import Headers

from task_store import StoreError, TaskStore, public_data

COOKIE = "pdd_rental_session"
ROLES = {"owner", "operator", "reviewer"}
WRITE_ROLES = {"owner", "operator"}
PASSWORD_ROUNDS = 600_000
SESSION_SECONDS = 12 * 60 * 60
MAX_REQUEST_BYTES = 13 * 1024 * 1024
ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


def _digest(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PASSWORD_ROUNDS).hex()


class AuthStore:
    def __init__(self, root):
        self.path = Path(root) / "data" / "rental_auth.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tenants(id TEXT PRIMARY KEY,name TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,tenant_id TEXT,username TEXT NOT NULL UNIQUE,salt TEXT NOT NULL,password_hash TEXT NOT NULL,role TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,token_hash TEXT NOT NULL UNIQUE,csrf_hash TEXT NOT NULL,device_name TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS login_attempts(bucket TEXT NOT NULL,created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS attempt_bucket ON login_attempts(bucket,created);
            """)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_user(self, tenant_id, username, password, role):
        username = username.strip()
        if not 3 <= len(username) <= 100 or any(ord(c) < 32 for c in username):
            raise HTTPException(422, "账号须为3到100个可见字符")
        if not 12 <= len(password) <= 256:
            raise HTTPException(422, "密码须为12到256个字符")
        if role not in ROLES | {"platform_admin"} or (role == "platform_admin") != (tenant_id is None):
            raise HTTPException(422, "账号角色无效")
        uid, salt = secrets.token_hex(16), secrets.token_hex(32)
        password_hash = _password(password, salt)
        try:
            with self.connection() as conn:
                conn.execute("INSERT INTO users(id,tenant_id,username,salt,password_hash,role,created) VALUES(?,?,?,?,?,?,?)", (uid, tenant_id, username, salt, password_hash, role, time.time()))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "账号已存在") from exc
        return {"id": uid, "tenant_id": tenant_id, "username": username, "role": role, "enabled": True}

    def bootstrap(self):
        username = os.getenv("PDD_RENTAL_BOOTSTRAP_USER", "").strip()
        password = os.getenv("PDD_RENTAL_BOOTSTRAP_PASSWORD", "")
        with self.connection() as conn:
            exists = conn.execute("SELECT 1 FROM users WHERE role='platform_admin' LIMIT 1").fetchone()
        if not exists and username and password:
            self.create_user(None, username, password, "platform_admin")

    def authenticate(self, token):
        if not token or len(token) > 200:
            return None
        with self.connection() as conn:
            row = conn.execute("SELECT s.id AS session_id,s.csrf_hash,s.expires,u.id AS user_id,u.tenant_id,u.username,u.role,u.enabled AS user_enabled,t.enabled AS tenant_enabled FROM sessions s JOIN users u ON s.user_id=u.id LEFT JOIN tenants t ON u.tenant_id=t.id WHERE s.token_hash=? AND s.revoked=0", (_digest(token),)).fetchone()
        if not row or row["expires"] <= time.time() or not row["user_enabled"]:
            return None
        if row["tenant_id"] and not row["tenant_enabled"]:
            return None
        return dict(row)

    def login(self, username, password, ip, device_name):
        # Both per-account and per-address limits apply; neither password nor
        # a reusable authentication credential enters this table.
        buckets = [_digest("user:" + username.casefold()), _digest("ip:" + ip)]
        now = time.time()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM login_attempts WHERE created<?", (now - 900,))
            for bucket, limit in zip(buckets, (8, 30)):
                count = conn.execute("SELECT COUNT(*) FROM login_attempts WHERE bucket=? AND created>?", (bucket, now - 300)).fetchone()[0]
                if count >= limit:
                    raise HTTPException(429, "登录尝试过多，请5分钟后再试", headers={"Retry-After": "300"})
            for bucket in buckets:
                conn.execute("INSERT INTO login_attempts VALUES(?,?)", (bucket, now))
            row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        # Do equivalent work for an unknown username to limit enumeration.
        salt = row["salt"] if row else "00" * 32
        valid = hmac.compare_digest(_password(password, salt), row["password_hash"] if row else "0" * 64)
        if not valid or not row or not row["enabled"]:
            raise HTTPException(401, "账号或密码不正确，或账号已停用")
        if row["tenant_id"]:
            with self.connection() as conn:
                tenant = conn.execute("SELECT enabled FROM tenants WHERE id=?", (row["tenant_id"],)).fetchone()
            if not tenant or not tenant[0]:
                raise HTTPException(401, "账号或密码不正确，或账号已停用")
        sid, token, csrf = secrets.token_hex(16), secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        with self.connection() as conn:
            conn.execute("INSERT INTO sessions(id,user_id,token_hash,csrf_hash,device_name,created,expires) VALUES(?,?,?,?,?,?,?)", (sid, row["id"], _digest(token), _digest(csrf), device_name[:80], now, now + SESSION_SECONDS))
        return {"session_id": sid, "access_token": token, "csrf_token": csrf, "expires_in": SESSION_SECONDS, "user": {key: row[key] for key in ("id", "tenant_id", "username", "role")}}


class LoginBody(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=256)
    device_name: str = Field(default="浏览器", max_length=80)


class TenantBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    owner_username: str = Field(min_length=3, max_length=100)
    owner_password: str = Field(min_length=12, max_length=256)
    image_limit: int = Field(0, ge=0, le=1_000_000)
    listing_limit: int = Field(0, ge=0, le=1_000_000)


class UserBody(BaseModel):
    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=12, max_length=256)
    role: str = "operator"


class QuotaBody(BaseModel):
    enabled: bool = True
    image_limit: int = Field(ge=0, le=1_000_000)
    listing_limit: int = Field(ge=0, le=1_000_000)


class SecurityGate:
    def __init__(self, app, *, store, production, trusted_proxies, allowed_hosts):
        self.app, self.store, self.production = app, store, production
        self.trusted_proxies, self.allowed_hosts = trusted_proxies, allowed_hosts

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        method, path = scope["method"], scope["path"]
        ip = (scope.get("client") or ("unknown",))[0]
        scheme = scope.get("scheme", "http")
        if ip in self.trusted_proxies and headers.get("x-forwarded-proto", "") == "https":
            scheme = "https"
        async def reject(status, message):
            await JSONResponse({"detail": message}, status_code=status)(scope, receive, send)
        if self.production and scheme != "https":
            await reject(426, "租赁站必须通过HTTPS访问")
            return
        if self.production and self.allowed_hosts and (urlsplit("//" + headers.get("host", "")).hostname or "").lower() not in self.allowed_hosts:
            await reject(400, "访问域名不在配置中")
            return
        try:
            declared = int(headers.get("content-length", "0"))
        except ValueError:
            declared = MAX_REQUEST_BYTES + 1
        if declared < 0 or declared > MAX_REQUEST_BYTES:
            await reject(413, "请求超过允许大小")
            return
        public = path in {"/health", "/api/auth/login"}
        from http.cookies import SimpleCookie
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("cookie", ""))
        except Exception:
            pass
        authorization = headers.get("authorization", "")
        bearer = authorization.startswith("Bearer ")
        token = authorization[7:] if bearer else cookie.get(COOKIE).value if cookie.get(COOKIE) else ""
        auth = self.store.authenticate(token)
        if not public and not auth:
            await reject(401, "请登录；会话可能已过期或被停用")
            return
        unsafe = method not in {"GET", "HEAD", "OPTIONS"}
        origin = headers.get("origin")
        if unsafe and origin and origin.rstrip("/") != f"{scheme}://{headers.get('host', '')}":
            await reject(403, "请求来源不匹配")
            return
        if unsafe and not public and not bearer and not hmac.compare_digest(_digest(headers.get("x-pdd-csrf", "")), auth["csrf_hash"]):
            await reject(403, "缺少有效的页面安全凭证")
            return
        scope.setdefault("state", {})["rental_auth"] = auth
        consumed, started = 0, False
        async def limited_receive():
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > MAX_REQUEST_BYTES:
                    raise HTTPException(413, "请求超过允许大小")
            return message
        async def secure_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                extra = [(b"x-content-type-options", b"nosniff"), (b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer")]
                if self.production:
                    extra.append((b"strict-transport-security", b"max-age=31536000"))
                message = {**message, "headers": list(message.get("headers", [])) + extra}
            await send(message)
        try:
            from safe_http import OUTBOUND_PRODUCTION
            context = OUTBOUND_PRODUCTION.set(self.production)
            try:
                await self.app(scope, limited_receive, secure_send)
            finally:
                OUTBOUND_PRODUCTION.reset(context)
        except HTTPException as exc:
            if started:
                raise
            await reject(exc.status_code, exc.detail)


def create_app(root, production=True):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    auth_store = AuthStore(root)
    auth_store.bootstrap()
    app = FastAPI(title="PDD rental service", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth_store, app.state.root = auth_store, root
    app.state.tenant_apps = {}
    tenant_lock = threading.Lock()

    def tenant_root(tenant_id):
        if not ID_PATTERN.fullmatch(str(tenant_id)):
            raise HTTPException(404, "租户不存在")
        path = root / "data" / "tenants" / tenant_id
        if path.is_symlink() or (root / "data").is_symlink() or (root / "data" / "tenants").is_symlink():
            raise HTTPException(503, "租户存储路径不安全")
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    def tenant_app(tenant_id):
        with tenant_lock:
            if tenant_id not in app.state.tenant_apps:
                from ai_api import install_ai_routes
                sub = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
                install_ai_routes(sub, tenant_root(tenant_id), lambda: None, owner=tenant_id, production=production)
                app.state.tenant_apps[tenant_id] = sub
            return app.state.tenant_apps[tenant_id]

    app.state.tenant_root, app.state.tenant_app = tenant_root, tenant_app

    def current(request, roles=None):
        auth = request.state.rental_auth
        if roles and auth["role"] not in roles:
            raise HTTPException(403, "当前角色没有此操作权限")
        return auth

    def tenant_context(request, roles=None):
        auth = current(request, roles)
        if not auth["tenant_id"]:
            raise HTTPException(403, "请使用所属租户账号操作商品")
        sub = tenant_app(auth["tenant_id"])
        return auth, sub.state.ai_runner.store, sub.state.ai_runner.project_dir

    @app.get("/health")
    def health():
        with auth_store.connection() as connection:
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise HTTPException(503, '存储检查失败')
        return {"status": "ok", "mode": "rental", "production": bool(production)}

    @app.post("/api/auth/login")
    def login(body: LoginBody, request: Request):
        result = auth_store.login(body.username.strip(), body.password, (request.client.host if request.client else "unknown"), body.device_name)
        response = JSONResponse(result)
        response.set_cookie(COOKIE, result["access_token"], max_age=SESSION_SECONDS, secure=production, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/api/auth/me")
    def me(request: Request):
        auth = current(request)
        return {key: auth[key] for key in ("user_id", "tenant_id", "username", "role", "session_id", "expires")}

    @app.post("/api/auth/logout")
    def logout(request: Request):
        auth = current(request)
        with auth_store.connection() as conn:
            conn.execute("UPDATE sessions SET revoked=1 WHERE id=?", (auth["session_id"],))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/", secure=production, httponly=True, samesite="strict")
        return response

    @app.get("/api/auth/devices")
    def devices(request: Request):
        auth = current(request)
        with auth_store.connection() as conn:
            rows = conn.execute("SELECT id,device_name,created,expires,revoked FROM sessions WHERE user_id=? ORDER BY created DESC LIMIT 100", (auth["user_id"],)).fetchall()
        return {"devices": [dict(row) for row in rows]}

    @app.delete("/api/auth/devices/{device_id}")
    def revoke_device(device_id: str, request: Request):
        auth = current(request)
        with auth_store.connection() as conn:
            changed = conn.execute("UPDATE sessions SET revoked=1 WHERE id=? AND user_id=?", (device_id, auth["user_id"])).rowcount
        if not changed:
            raise HTTPException(404, "设备会话不存在")
        return {"ok": True}

    @app.post("/api/platform/tenants")
    def create_tenant(body: TenantBody, request: Request):
        current(request, {"platform_admin"})
        tid = secrets.token_hex(16)
        with auth_store.connection() as conn:
            conn.execute("INSERT INTO tenants VALUES(?,?,1,?)", (tid, body.name.strip(), time.time()))
        try:
            owner = auth_store.create_user(tid, body.owner_username, body.owner_password, "owner")
        except BaseException:
            with auth_store.connection() as conn:
                conn.execute("DELETE FROM tenants WHERE id=?", (tid,))
            raise
        store = tenant_app(tid).state.ai_runner.store
        store.set_entitlement(tid, enabled=True, image_limit=body.image_limit, listing_limit=body.listing_limit)
        return {"id": tid, "name": body.name.strip(), "owner": owner}

    @app.get("/api/platform/tenants")
    def list_tenants(request: Request):
        current(request, {"platform_admin"})
        with auth_store.connection() as conn:
            return {"tenants": [dict(row) for row in conn.execute("SELECT * FROM tenants ORDER BY created DESC")]}

    @app.put("/api/platform/tenants/{tenant_id}/entitlement")
    def entitlement(tenant_id: str, body: QuotaBody, request: Request):
        current(request, {"platform_admin"})
        with auth_store.connection() as conn:
            found = conn.execute("UPDATE tenants SET enabled=? WHERE id=?", (int(body.enabled), tenant_id)).rowcount
            if found and not body.enabled:
                conn.execute("UPDATE sessions SET revoked=1 WHERE user_id IN (SELECT id FROM users WHERE tenant_id=?)", (tenant_id,))
        if not found:
            raise HTTPException(404, "租户不存在")
        store = tenant_app(tenant_id).state.ai_runner.store
        store.set_entitlement(tenant_id, **body.model_dump())
        if not body.enabled:
            for task in store.active_tasks(tenant_id):
                store.cancel(tenant_id, task["id"])
        return store.entitlement(tenant_id)

    @app.get("/api/tenant/usage")
    def usage(request: Request):
        auth, store, _ = tenant_context(request)
        return store.entitlement(auth["tenant_id"])

    @app.post("/api/tenant/users")
    def create_user(body: UserBody, request: Request):
        auth = current(request, {"owner"})
        if body.role not in {"operator", "reviewer"}:
            raise HTTPException(403, "店主只能添加操作员或审核员")
        return auth_store.create_user(auth["tenant_id"], body.username, body.password, body.role)

    @app.get("/api/tenant/users")
    def users(request: Request):
        auth = current(request, {"owner"})
        with auth_store.connection() as conn:
            return {"users": [dict(row) for row in conn.execute("SELECT id,username,role,enabled,created FROM users WHERE tenant_id=?", (auth["tenant_id"],))]}

    @app.post("/api/tenant/users/{user_id}/disable")
    def disable_user(user_id: str, request: Request):
        auth = current(request, {"owner"})
        if user_id == auth["user_id"]:
            raise HTTPException(422, "不能在此停用当前店主账号")
        with auth_store.connection() as conn:
            found = conn.execute("UPDATE users SET enabled=0 WHERE id=? AND tenant_id=? AND role<>'owner'", (user_id, auth["tenant_id"])).rowcount
            if found:
                conn.execute("UPDATE sessions SET revoked=1 WHERE user_id=?", (user_id,))
        if not found:
            raise HTTPException(404, "可停用账号不存在")
        return {"ok": True}

    @app.get("/api/drafts")
    def drafts(request: Request):
        auth, store, _ = tenant_context(request)
        return {"products": store.list_drafts(auth["tenant_id"])}

    @app.get("/api/drafts/{draft_id}")
    def get_draft(draft_id: str, request: Request):
        auth, store, _ = tenant_context(request)
        draft = store.get_draft(auth["tenant_id"], draft_id)
        if not draft:
            raise HTTPException(404, "商品草稿不存在")
        return draft

    @app.post("/api/drafts")
    def save_draft(body: dict, request: Request):
        auth, store, directory = tenant_context(request, WRITE_ROLES)
        from local_drafts import save_package
        try:
            package = save_package(directory, store, auth["tenant_id"], public_data(body))
        except (StoreError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"success": True, "id": package["id"], "product_package": package}

    @app.delete("/api/drafts/{draft_id}")
    def delete_draft(draft_id: str, request: Request):
        auth, store, _ = tenant_context(request, {"owner"})
        if not store.delete_draft(auth["tenant_id"], draft_id):
            raise HTTPException(404, "商品草稿不存在")
        return {"ok": True}

    @app.post("/api/drafts/{draft_id}/review")
    def review(draft_id: str, body: dict, request: Request):
        auth, store, _ = tenant_context(request)
        draft = store.get_draft(auth["tenant_id"], draft_id)
        if not draft:
            raise HTTPException(404, "商品草稿不存在")
        module = next((item for item in draft.get("module_outputs", []) if item.get("id") == body.get("module_id")), None)
        if not module:
            raise HTTPException(404, "图片模块不存在")
        if not isinstance(body.get("approved"), bool):
            raise HTTPException(422, "请明确选择审核是否通过")
        module["needs_review"] = not body["approved"]
        draft.setdefault("review_history", []).append({"module_id": module["id"], "approved": body["approved"], "user_id": auth["user_id"], "at": time.time()})
        draft["review_history"] = draft["review_history"][-500:]
        return store.save_draft(auth["tenant_id"], draft)

    @app.get("/data/{asset:path}")
    def asset_file(asset: str, request: Request):
        _, _, directory = tenant_context(request)
        base = (directory / "data").resolve()
        target = (base / asset).resolve()
        if base not in target.parents or not target.is_file() or target.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
            raise HTTPException(404, "图片不存在")
        from PIL import Image
        try:
            with Image.open(target) as image:
                if image.format not in {"JPEG", "PNG", "WEBP", "BMP"} or image.width * image.height > 40_000_000:
                    raise ValueError()
        except Exception as exc:
            raise HTTPException(404, "图片不存在") from exc
        return FileResponse(target)

    @app.get("/api/one-click/{task_id}")
    def task_alias(task_id: str, request: Request):
        auth, store, _ = tenant_context(request)
        task = store.get_task(auth["tenant_id"], task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        return {**task["result"], "status": task["status"], "progress": task["progress"], "task_id": task_id}

    allowed = re.compile(r"^/api/ai/(?:plan|prepare|confirm|capabilities|requests/[A-Za-z0-9_-]{1,160}|workflow|regenerate|recrop|usage|providers/test|tasks/active|tasks/[a-f0-9]{32}(?:/(?:cancel|resume))?)$")
    class TenantGateway:
        async def __call__(self, scope, receive, send):
            path = scope.get("path", "")
            if scope["type"] != "http" or not allowed.fullmatch(path):
                await JSONResponse({"detail": "接口不存在"}, status_code=404)(scope, receive, send)
                return
            auth = scope.get("state", {}).get("rental_auth")
            if not auth or not auth.get("tenant_id"):
                await JSONResponse({"detail": "请使用所属租户账号"}, status_code=403)(scope, receive, send)
                return
            if scope["method"] not in {"GET", "HEAD"} and auth["role"] not in WRITE_ROLES:
                await JSONResponse({"detail": "审核员不能启动或修改生图任务"}, status_code=403)(scope, receive, send)
                return
            sub = tenant_app(auth["tenant_id"])
            quota = sub.state.ai_runner.store.entitlement(auth["tenant_id"])
            if not quota["enabled"]:
                await JSONResponse({"detail": "租户权益已停用"}, status_code=403)(scope, receive, send)
                return
            await sub(scope, receive, send)
    app.mount("/", TenantGateway())
    app.add_middleware(SecurityGate, store=auth_store, production=bool(production), trusted_proxies={item.strip() for item in os.getenv("PDD_RENTAL_TRUSTED_PROXY_IPS", "").split(",") if item.strip()}, allowed_hosts={item.strip().lower() for item in os.getenv("PDD_RENTAL_HOSTS", "").split(",") if item.strip()})
    return app


def create_env_app():
    root = os.environ.get('PDD_RENTAL_ROOT', '').strip()
    if not root or not os.environ.get('PDD_RENTAL_HOSTS', '').strip():
        raise RuntimeError('Set PDD_RENTAL_ROOT and PDD_RENTAL_HOSTS before starting rental service')
    app = create_app(root, production=True)
    with app.state.auth_store.connection() as conn:
        configured = conn.execute("SELECT 1 FROM users WHERE role='platform_admin'").fetchone()
    if not configured:
        raise RuntimeError('Configure the initial admin with PDD_RENTAL_BOOTSTRAP_USER/PASSWORD')
    return app
