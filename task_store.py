"""Durable owner-scoped work, drafts and usage accounting for local/rental apps."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def public_data(value):
    blocked = {"apikey", "imageapikey", "promptapikey", "authorization", "provideroverrides", "password", "traceback", "accesstoken", "sessiontoken", 'csrftoken', 'secret', 'privatekey'}
    if isinstance(value, dict):
        return {str(key): public_data(item) for key, item in value.items() if str(key).lower().replace('_','').replace('-','') not in blocked and not str(key).lower().replace('_','').replace('-','').endswith('apikey')}
    if isinstance(value, (tuple, list)):
        return [public_data(item) for item in value]
    return value


def request_digest(value) -> str:
    return hashlib.sha256(json.dumps(public_data(value), ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


class StoreError(ValueError):
    pass


class TaskStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / "data" / "workbench.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_key TEXT NOT NULL,
                    kind TEXT NOT NULL, status TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
                    checkpoint TEXT NOT NULL DEFAULT '{}', cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(owner,request_key)
                );
                CREATE INDEX IF NOT EXISTS task_owner ON tasks(owner,updated);
                CREATE TABLE IF NOT EXISTS drafts (
                    id TEXT NOT NULL, owner TEXT NOT NULL, data TEXT NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY(owner,id)
                );
                CREATE TABLE IF NOT EXISTS entitlements (
                    owner TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
                    image_limit INTEGER NOT NULL DEFAULT 1000000, listing_limit INTEGER NOT NULL DEFAULT 1000000,
                    image_used INTEGER NOT NULL DEFAULT 0, listing_used INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS reservations (
                    task_id TEXT NOT NULL, owner TEXT NOT NULL, kind TEXT NOT NULL,
                    reserved INTEGER NOT NULL, charged INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'reserved', updated REAL NOT NULL,
                    PRIMARY KEY(owner,task_id,kind)
                );
                CREATE TABLE IF NOT EXISTS usage_events (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, task_id TEXT NOT NULL,
                    module_id TEXT NOT NULL DEFAULT '', state TEXT NOT NULL, amount INTEGER NOT NULL DEFAULT 0,
                    data TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL
                );
            """)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _task(self, row):
        if not row:
            return None
        item = dict(row)
        for field in ("payload", "result", "checkpoint"):
            item[field] = json.loads(item[field])
        item["task_id"] = item["id"]
        return item

    def create_task(self, owner: str, kind: str, payload: dict, request_key: str = "") -> tuple[dict, bool]:
        request_key = str(request_key or secrets.token_hex(24))[:160]
        now = time.time()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            found = conn.execute("SELECT * FROM tasks WHERE owner=? AND request_key=?", (owner, request_key)).fetchone()
            if found:
                return self._task(found), False
            task_id = secrets.token_hex(16)
            conn.execute("INSERT INTO tasks(id,owner,request_key,kind,status,payload,created,updated) VALUES(?,?,?,?,?,?,?,?)", (task_id, owner, request_key, kind, "queued", json.dumps(public_data(payload), ensure_ascii=False), now, now))
            return self._task(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()), True

    def get_task(self, owner: str, task_id: str):
        with self.connection() as conn:
            return self._task(conn.execute("SELECT * FROM tasks WHERE id=? AND owner=?", (task_id, owner)).fetchone())

    def update_task(self, owner: str, task_id: str, *, status=None, progress=None, result=None, checkpoint=None):
        fields, values = ["updated=?"], [time.time()]
        for name, value in (("status", status), ("progress", progress), ("result", result), ("checkpoint", checkpoint)):
            if value is not None:
                fields.append(name + "=?")
                values.append(json.dumps(public_data(value), ensure_ascii=False) if name in {"result", "checkpoint"} else value)
        with self.connection() as conn:
            conn.execute("UPDATE tasks SET " + ",".join(fields) + " WHERE id=? AND owner=?", values + [task_id, owner])

    def active_tasks(self, owner: str):
        with self.connection() as conn:
            return [self._task(row) for row in conn.execute("SELECT * FROM tasks WHERE owner=? AND status IN ('queued','running','interrupted','cancel_requested','needs_reconciliation') ORDER BY updated DESC", (owner,))]

    def interrupt_startup(self):
        with self.connection() as conn:
            conn.execute("UPDATE tasks SET status='interrupted',progress='服务已重启；可重新提供API配置后继续，成功批次会保留',updated=? WHERE status IN ('queued','running','cancel_requested')", (time.time(),))

    def cancel(self, owner: str, task_id: str) -> bool:
        with self.connection() as conn:
            row = conn.execute("SELECT status FROM tasks WHERE id=? AND owner=?", (task_id, owner)).fetchone()
            if not row:
                return False
            if row['status']=='interrupted':
                conn.execute("UPDATE tasks SET cancel_requested=1,status='cancelled',progress='任务已停止，已完成批次保留',updated=? WHERE id=? AND owner=?",(time.time(),task_id,owner))
            elif row["status"] not in {"done", "failed", "error", "cancelled", 'needs_reconciliation', 'needs_review', 'filled_verified', 'partial'}:
                conn.execute("UPDATE tasks SET cancel_requested=1,status='cancel_requested',progress='正在等待当前请求结束，后续批次将停止',updated=? WHERE id=? AND owner=?", (time.time(), task_id, owner))
            return True

    def cancelled(self, owner: str, task_id: str) -> bool:
        with self.connection() as conn:
            row = conn.execute("SELECT cancel_requested FROM tasks WHERE id=? AND owner=?", (task_id, owner)).fetchone()
            return bool(row and row[0])

    def reset_cancel(self, owner: str, task_id: str):
        with self.connection() as conn:
            conn.execute("UPDATE tasks SET cancel_requested=0 WHERE id=? AND owner=?", (task_id, owner))

    def save_draft(self, owner: str, data: dict):
        clean = public_data(data)
        draft_id = str(clean.get("id") or "p" + secrets.token_hex(12))
        if len(draft_id) > 80 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in draft_id):
            raise StoreError("商品编号无效")
        clean.update({"id": draft_id, "schema_version": 2, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        with self.connection() as conn:
            conn.execute("INSERT INTO drafts(owner,id,data,updated) VALUES(?,?,?,?) ON CONFLICT(owner,id) DO UPDATE SET data=excluded.data,updated=excluded.updated", (owner, draft_id, json.dumps(clean, ensure_ascii=False), time.time()))
        return clean

    def get_draft(self, owner: str, draft_id: str):
        with self.connection() as conn:
            row = conn.execute("SELECT data FROM drafts WHERE owner=? AND id=?", (owner, draft_id)).fetchone()
            return json.loads(row[0]) if row else None

    def list_drafts(self, owner: str):
        with self.connection() as conn:
            return [json.loads(row[0]) for row in conn.execute("SELECT data FROM drafts WHERE owner=? ORDER BY updated DESC", (owner,))]

    def delete_draft(self, owner: str, draft_id: str):
        with self.connection() as conn:
            return bool(conn.execute("DELETE FROM drafts WHERE owner=? AND id=?", (owner, draft_id)).rowcount)

    def entitlement(self, owner: str):
        with self.connection() as conn:
            conn.execute("INSERT OR IGNORE INTO entitlements(owner) VALUES(?)", (owner,))
            result = dict(conn.execute("SELECT * FROM entitlements WHERE owner=?", (owner,)).fetchone())
            for kind in ("image", "listing"):
                pending = conn.execute("SELECT COALESCE(SUM(reserved-charged),0) FROM reservations WHERE owner=? AND kind=? AND state IN ('reserved','unknown')", (owner, kind)).fetchone()[0]
                result[kind + "_reserved"] = pending
                result[kind + "_remaining"] = max(0, result[kind + "_limit"] - result[kind + "_used"] - pending)
            return result

    def set_entitlement(self, owner: str, *, enabled=True, image_limit=0, listing_limit=0):
        with self.connection() as conn:
            conn.execute("INSERT INTO entitlements(owner,enabled,image_limit,listing_limit) VALUES(?,?,?,?) ON CONFLICT(owner) DO UPDATE SET enabled=excluded.enabled,image_limit=excluded.image_limit,listing_limit=excluded.listing_limit", (owner, int(enabled), max(0, int(image_limit)), max(0, int(listing_limit))))

    def reserve(self, owner: str, task_id: str, kind: str, amount: int):
        if kind not in {"image", "listing"}:
            raise StoreError("额度类型无效")
        amount = max(0, int(amount))
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO entitlements(owner) VALUES(?)", (owner,))
            quota = conn.execute("SELECT * FROM entitlements WHERE owner=?", (owner,)).fetchone()
            if not quota["enabled"]:
                raise StoreError("当前账号已停用")
            if conn.execute("SELECT 1 FROM reservations WHERE owner=? AND task_id=? AND kind=?", (owner, task_id, kind)).fetchone():
                return
            pending = conn.execute("SELECT COALESCE(SUM(reserved-charged),0) FROM reservations WHERE owner=? AND kind=? AND state IN ('reserved','unknown')", (owner, kind)).fetchone()[0]
            if quota[kind + "_used"] + pending + amount > quota[kind + "_limit"]:
                raise StoreError("当前额度不足，请减少请求数量或联系管理员")
            conn.execute("INSERT INTO reservations(owner,task_id,kind,reserved,updated) VALUES(?,?,?,?,?)", (owner, task_id, kind, amount, time.time()))

    def settle(self, owner: str, task_id: str, kind: str, charged: int, *, unknown=False):
        if kind not in {"image", "listing"}:
            raise StoreError("额度类型无效")
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM reservations WHERE owner=? AND task_id=? AND kind=?", (owner, task_id, kind)).fetchone()
            if not row or row["state"] == "settled":
                return
            total = max(row["charged"], min(row["reserved"], int(charged)))
            delta = total - row["charged"]
            conn.execute("UPDATE entitlements SET " + kind + "_used=" + kind + "_used+? WHERE owner=?", (delta, owner))
            conn.execute("UPDATE reservations SET charged=?,state=?,updated=? WHERE owner=? AND task_id=? AND kind=?", (total, "unknown" if unknown else "settled", time.time(), owner, task_id, kind))

    def reopen_reservation(self, owner: str, task_id: str, kind: str):
        """Reacquire the unused budget before an explicitly requested resume."""
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM reservations WHERE owner=? AND task_id=? AND kind=?', (owner, task_id, kind)).fetchone()
            quota = conn.execute('SELECT * FROM entitlements WHERE owner=?', (owner,)).fetchone()
            if not row or not quota or not quota['enabled']:
                raise StoreError('当前任务没有可用额度或账号已停用')
            if row['state'] == 'unknown':
                raise StoreError('上一请求扣费状态未知，请核对后再开始新任务')
            pending = conn.execute("SELECT COALESCE(SUM(reserved-charged),0) FROM reservations WHERE owner=? AND kind=? AND state IN ('reserved','unknown') AND task_id<>?", (owner, kind, task_id)).fetchone()[0]
            if quota[kind + '_used'] + pending + row['reserved'] - row['charged'] > quota[kind + '_limit']:
                raise StoreError('剩余额度不足以继续原方案')
            conn.execute("UPDATE reservations SET state='reserved',updated=? WHERE owner=? AND task_id=? AND kind=?", (time.time(), owner, task_id, kind))

    def event(self, owner: str, task_id: str, state: str, *, module_id="", amount=0, data=None):
        with self.connection() as conn:
            conn.execute("INSERT INTO usage_events VALUES(?,?,?,?,?,?,?,?)", (secrets.token_hex(16), owner, task_id, module_id, state, int(amount), json.dumps(public_data(data or {}), ensure_ascii=False), time.time()))

    def health(self):
        with self.connection() as conn:
            return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
