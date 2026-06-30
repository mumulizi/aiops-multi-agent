"""异步验证任务存储 (v2.12 §4).

为什么需要:
- 旧 Validator 主流程内 time.sleep(30) 阻塞调度
- 30s 太短, Pod 重启常常需要 60-120s 才 ready
- 需要 30s/2min/10min 三轮检查覆盖快慢恢复

设计:
- 单进程 + SQLite + daemon 线程, 不引入 Redis/Celery
- WAL 模式保证读写并发安全 (跟 fault_memory 一致)
- 进程退出时 status=pending 的任务保留, 重启后自动 pick up

Schema:
  verification_tasks(
    task_id        TEXT PRIMARY KEY,      -- uuid
    trace_id       TEXT,                  -- LangGraph trace
    namespace      TEXT,
    pod            TEXT,
    action         TEXT,                  -- restart_pod / delete_evicted_pod / ...
    plan_json      TEXT,                  -- 完整 remediation_plan
    state_json     TEXT,                  -- AlertState 快照 (含 snapshot_before / rca / retry_count)
    created_at     INTEGER,               -- 入队时间
    check_at       INTEGER,               -- 下次该跑验证的时间 (按 round 递增)
    check_round    INTEGER DEFAULT 0,     -- 已检查轮次 0/1/2/3
    status         TEXT DEFAULT 'pending',-- pending | success | failed | escalate_human | timeout
    last_result    TEXT,                  -- 最近一次 _verify_once 的 dict JSON
    updated_at     INTEGER
  )

Check 节奏 (按 created_at 偏移):
  round 0 → 30s
  round 1 → 2min   (前面未通过)
  round 2 → 10min  (前面仍未通过)
  round 3 终态 timeout
"""
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

DB_PATH = Path(os.getenv("AIOPS_DB_PATH", "data/aiops.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()

# 三轮验证的偏移秒数 (相对 created_at)
ROUND_OFFSETS = [30, 120, 600]


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=10, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""
        CREATE TABLE IF NOT EXISTS verification_tasks (
            task_id      TEXT PRIMARY KEY,
            trace_id     TEXT,
            namespace    TEXT,
            pod          TEXT,
            action       TEXT,
            plan_json    TEXT NOT NULL,
            state_json   TEXT NOT NULL,
            created_at   INTEGER NOT NULL,
            check_at     INTEGER NOT NULL,
            check_round  INTEGER NOT NULL DEFAULT 0,
            status       TEXT NOT NULL DEFAULT 'pending',
            last_result  TEXT,
            updated_at   INTEGER NOT NULL
        )
    """)
    c.execute("""CREATE INDEX IF NOT EXISTS idx_verif_due
                 ON verification_tasks(status, check_at)""")
    return c


def enqueue(state: dict, plan: dict) -> str:
    """把一个等待验证的任务写入表, 返回 task_id.

    state 是 AlertState 的浅拷贝 — 这里只保留必要字段, 避免存太大对象.
    """
    task_id = uuid.uuid4().hex
    now = int(time.time())
    check_at = now + ROUND_OFFSETS[0]

    target = (plan or {}).get("target", "")
    ns, pod = "", ""
    if "/" in (target or ""):
        ns, pod = target.split("/", 1)
    action = (plan or {}).get("action", "")
    trace_id = state.get("trace_id", "")

    # state 只挑必要字段持久化, 避免 LangGraph 内部对象不可序列化
    slim_state = {
        "trace_id": trace_id,
        "raw_alerts": state.get("raw_alerts", []),
        "rca_hypothesis": state.get("rca_hypothesis", ""),
        "snapshot_before": state.get("snapshot_before", {}),
        "snapshot_after": state.get("snapshot_after", {}),
        "retry_count": state.get("retry_count", 0),
        "fingerprint": state.get("fingerprint", ""),
        "severity": state.get("severity", ""),
        "label": state.get("label", ""),
    }

    with _lock:
        c = _conn()
        try:
            c.execute(
                "INSERT INTO verification_tasks "
                "(task_id, trace_id, namespace, pod, action, plan_json, state_json, "
                " created_at, check_at, check_round, status, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'pending', ?)",
                (
                    task_id, trace_id, ns, pod, action,
                    json.dumps(plan, ensure_ascii=False),
                    json.dumps(slim_state, ensure_ascii=False, default=str),
                    now, check_at, now,
                ),
            )
        finally:
            c.close()
    return task_id


def claim_due(limit: int = 10) -> list:
    """拉到期任务 (status=pending 且 check_at <= now), 返回 dict 列表.

    幂等: worker 会在 update_status 里更新 check_at 或终态.
    """
    now = int(time.time())
    with _lock:
        c = _conn()
        try:
            cur = c.execute(
                "SELECT task_id, trace_id, namespace, pod, action, plan_json, "
                "state_json, created_at, check_at, check_round, status "
                "FROM verification_tasks "
                "WHERE status='pending' AND check_at <= ? "
                "ORDER BY check_at ASC LIMIT ?",
                (now, limit),
            )
            rows = cur.fetchall()
        finally:
            c.close()
    out = []
    for r in rows:
        out.append({
            "task_id": r[0],
            "trace_id": r[1],
            "namespace": r[2],
            "pod": r[3],
            "action": r[4],
            "plan": json.loads(r[5]) if r[5] else {},
            "state": json.loads(r[6]) if r[6] else {},
            "created_at": r[7],
            "check_at": r[8],
            "check_round": r[9],
            "status": r[10],
        })
    return out


def update_status(task_id: str, *, status: str = None,
                  last_result: Optional[dict] = None,
                  next_check_at: Optional[int] = None,
                  check_round: Optional[int] = None) -> None:
    """更新任务状态 (终态或下次检查时间).

    传入 None 表示该字段不更新.
    """
    now = int(time.time())
    sets = []
    args = []
    if status is not None:
        sets.append("status=?")
        args.append(status)
    if last_result is not None:
        sets.append("last_result=?")
        args.append(json.dumps(last_result, ensure_ascii=False, default=str))
    if next_check_at is not None:
        sets.append("check_at=?")
        args.append(int(next_check_at))
    if check_round is not None:
        sets.append("check_round=?")
        args.append(int(check_round))
    sets.append("updated_at=?")
    args.append(now)
    args.append(task_id)
    sql = f"UPDATE verification_tasks SET {', '.join(sets)} WHERE task_id=?"
    with _lock:
        c = _conn()
        try:
            c.execute(sql, args)
        finally:
            c.close()


def list_recent(limit: int = 50, status: str = None) -> list:
    """CLI 用: 列任务表 (pending 优先, 然后按 updated_at 倒序)."""
    with _lock:
        c = _conn()
        try:
            if status:
                cur = c.execute(
                    "SELECT task_id, namespace, pod, action, check_round, status, "
                    "       created_at, check_at, updated_at, last_result "
                    "FROM verification_tasks WHERE status=? "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (status, limit),
                )
            else:
                cur = c.execute(
                    "SELECT task_id, namespace, pod, action, check_round, status, "
                    "       created_at, check_at, updated_at, last_result "
                    "FROM verification_tasks "
                    "ORDER BY (status='pending') DESC, updated_at DESC LIMIT ?",
                    (limit,),
                )
            rows = cur.fetchall()
        finally:
            c.close()
    out = []
    for r in rows:
        out.append({
            "task_id": r[0],
            "namespace": r[1],
            "pod": r[2],
            "action": r[3],
            "check_round": r[4],
            "status": r[5],
            "created_at": r[6],
            "check_at": r[7],
            "updated_at": r[8],
            "last_result": json.loads(r[9]) if r[9] else None,
        })
    return out
