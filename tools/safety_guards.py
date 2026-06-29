"""速率限制 (SQLite 持久化) + 审计日志 (进程内存).

v2.12 升级:
- 速率限制从内存 deque 迁到 SQLite (data/aiops.db rate_limit_records 表)
  目的: 重启不丢状态; 多副本部署可共享同一 db (NFS / 共享卷)
- 审计日志保持内存 deque 不变 (本期不迁, 学习/单机场景够用)
"""
import os
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path

# === 速率限制: SQLite ===
DB_PATH = Path(os.getenv("AIOPS_DB_PATH", "data/aiops.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_db_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=10, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""
        CREATE TABLE IF NOT EXISTS rate_limit_records (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            target TEXT NOT NULL,
            action TEXT NOT NULL,
            ts     INTEGER NOT NULL
        )
    """)
    c.execute("""CREATE INDEX IF NOT EXISTS idx_rate_target_action_ts
                 ON rate_limit_records(target, action, ts)""")
    return c


def allow(target: str, action: str, max_per_hour: int = 3) -> tuple:
    """检查是否在速率限制内. 返回 (allowed, reason).

    SQLite 实现:
    1. 删 1h 前的过期记录 (顺手 GC)
    2. 统计当前窗口内同 target+action 的记录数
    3. 满 → 拒绝; 未满 → 插入新记录并放行
    """
    now = int(time.time())
    cutoff = now - 3600
    with _db_lock:
        c = _conn()
        try:
            # GC 过期记录 (顺手清理整张表, 不只是当前 key)
            c.execute("DELETE FROM rate_limit_records WHERE ts < ?", (cutoff,))
            # 统计当前窗口
            cur = c.execute(
                "SELECT COUNT(*), MIN(ts) FROM rate_limit_records "
                "WHERE target=? AND action=? AND ts >= ?",
                (target, action, cutoff),
            )
            count, oldest = cur.fetchone()
            count = count or 0
            if count >= max_per_hour:
                oldest_str = time.strftime("%H:%M:%S", time.localtime(oldest)) \
                    if oldest else "?"
                return False, (
                    f"rate limit: {target} action={action} "
                    f"hit {count}/{max_per_hour} in last hour "
                    f"(oldest at {oldest_str})"
                )
            c.execute(
                "INSERT INTO rate_limit_records (target, action, ts) VALUES (?, ?, ?)",
                (target, action, now),
            )
            return True, "ok"
        finally:
            c.close()


def get_rate_status(target: str = None) -> dict:
    """看当前速率状态 (调试用). 返回 {f"{target}::{action}": count}."""
    now = int(time.time())
    cutoff = now - 3600
    with _db_lock:
        c = _conn()
        try:
            if target:
                cur = c.execute(
                    "SELECT target, action, COUNT(*) FROM rate_limit_records "
                    "WHERE target=? AND ts >= ? GROUP BY target, action",
                    (target, cutoff),
                )
            else:
                cur = c.execute(
                    "SELECT target, action, COUNT(*) FROM rate_limit_records "
                    "WHERE ts >= ? GROUP BY target, action",
                    (cutoff,),
                )
            return {f"{t}::{a}": n for t, a, n in cur.fetchall()}
        finally:
            c.close()


# === 审计日志: 内存 deque (本期不迁) ===
_audit_log = deque(maxlen=500)
_audit_lock = threading.Lock()


def record_audit(entry: dict):
    """写一条审计日志 (内存)."""
    with _audit_lock:
        entry_with_ts = {
            **entry,
            "audit_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        _audit_log.append(entry_with_ts)


def get_audit_log(limit: int = 50) -> list:
    """读最近 N 条审计 (内存)."""
    with _audit_lock:
        return list(_audit_log)[-limit:]
