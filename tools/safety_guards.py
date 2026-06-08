"""速率限制 + 审计日志 (放内存, 进程级).

生产可换成 Redis / SQLite, 这里先 in-memory 够用.
"""
import time
import threading
from collections import defaultdict, deque

# (target, action) → [timestamps]
_records = defaultdict(deque)
_lock = threading.Lock()

# 审计日志 (最近 N 条, 内存)
_audit_log = deque(maxlen=500)


def allow(target: str, action: str, max_per_hour: int = 3) -> tuple:
    """检查是否在速率限制内. 返回 (allowed, reason)."""
    with _lock:
        now = time.time()
        key = (target, action)
        recs = _records[key]
        # 清理 1h 前的
        cutoff = now - 3600
        while recs and recs[0] < cutoff:
            recs.popleft()
        if len(recs) >= max_per_hour:
            oldest = recs[0]
            return False, (
                f"rate limit: {target} action={action} "
                f"hit {len(recs)}/{max_per_hour} in last hour "
                f"(oldest at {time.strftime('%H:%M:%S', time.localtime(oldest))})"
            )
        recs.append(now)
        return True, "ok"


def record_audit(entry: dict):
    """写一条审计日志"""
    with _lock:
        entry_with_ts = {
            **entry,
            "audit_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        _audit_log.append(entry_with_ts)


def get_audit_log(limit: int = 50) -> list:
    """读最近 N 条审计"""
    with _lock:
        return list(_audit_log)[-limit:]


def get_rate_status(target: str = None) -> dict:
    """看当前速率状态 (调试用)"""
    with _lock:
        now = time.time()
        cutoff = now - 3600
        result = {}
        for (t, a), recs in _records.items():
            if target and t != target:
                continue
            recent = [r for r in recs if r >= cutoff]
            if recent:
                result[f"{t}::{a}"] = len(recent)
        return result
