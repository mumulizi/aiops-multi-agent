"""审批存储: SQLite 持久化 L2 待审批操作.

Schema:
  approvals(id TEXT PRIMARY KEY, created_at INT, plan_json TEXT, state_json TEXT,
            status TEXT, ttl_sec INT, decided_by TEXT, decided_at INT, result_json TEXT)

status:
  pending   待审批
  approved  已批准 (待执行 / 执行中)
  executed  已执行成功
  rejected  人工拒绝
  expired   超时未审批
  failed    执行失败
"""
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

DB_PATH = Path(os.getenv("APPROVAL_DB_PATH", "data/approvals.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

DEFAULT_TTL_SEC = int(os.getenv("APPROVAL_TTL_SEC", "1800"))  # 30 min

_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=10, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""
        CREATE TABLE IF NOT EXISTS approvals (
            id TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL,
            plan_json TEXT NOT NULL,
            state_json TEXT NOT NULL,
            status TEXT NOT NULL,
            ttl_sec INTEGER NOT NULL,
            decided_by TEXT,
            decided_at INTEGER,
            result_json TEXT
        )
    """)
    # v2.14: 扩展支持诊断命令审批 (kind='diagnostic_cmd')
    # 旧的 plan 审批 kind='remediation' (NULL 也兼容)
    # 用 ALTER TABLE IF NOT EXISTS 形式 (sqlite 不支持, 这里 try/except 兜底)
    for col_def in (
        "kind TEXT DEFAULT 'remediation'",
        "cmd_payload TEXT",
        "execution_result TEXT",
    ):
        col_name = col_def.split(" ", 1)[0]
        try:
            c.execute(f"ALTER TABLE approvals ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass  # 已存在
    return c


def _gen_id() -> str:
    """生成 8 位大写字母+数字 ID, 方便人手动输入"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉易混 I/O/0/1
    return "".join(secrets.choice(alphabet) for _ in range(8))


def create_pending(plan: dict, state: dict, ttl_sec: Optional[int] = None) -> str:
    """新建待审批记录, 返回 approval_id"""
    if ttl_sec is None:
        ttl_sec = DEFAULT_TTL_SEC

    # 精简 state, 只保留关键字段 (避免 SQLite blob 过大)
    keep_keys = (
        "trace_id", "raw_alerts", "event_summary", "label", "severity",
        "rca_hypothesis", "remediation_plan", "approval_reason",
    )
    slim_state = {k: state.get(k) for k in keep_keys if k in state}

    aid = _gen_id()
    with _lock:
        c = _conn()
        try:
            c.execute(
                """INSERT INTO approvals
                (id, created_at, plan_json, state_json, status, ttl_sec)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (aid, int(time.time()),
                 json.dumps(plan, ensure_ascii=False, default=str),
                 json.dumps(slim_state, ensure_ascii=False, default=str),
                 "pending", ttl_sec),
            )
        finally:
            c.close()
    return aid


def get(approval_id: str) -> Optional[dict]:
    with _lock:
        c = _conn()
        try:
            row = c.execute(
                "SELECT id, created_at, plan_json, state_json, status, ttl_sec, "
                "decided_by, decided_at, result_json FROM approvals WHERE id=?",
                (approval_id,),
            ).fetchone()
        finally:
            c.close()
    if not row:
        return None
    return {
        "id": row[0],
        "created_at": row[1],
        "plan": json.loads(row[2]) if row[2] else None,
        "state": json.loads(row[3]) if row[3] else None,
        "status": row[4],
        "ttl_sec": row[5],
        "decided_by": row[6],
        "decided_at": row[7],
        "result": json.loads(row[8]) if row[8] else None,
    }


def is_expired(rec: dict) -> bool:
    age = int(time.time()) - rec["created_at"]
    return age > rec["ttl_sec"]


def list_pending(limit: int = 50) -> list:
    """列出未过期的 pending"""
    cutoff = int(time.time())
    with _lock:
        c = _conn()
        try:
            rows = c.execute(
                "SELECT id, created_at, plan_json, status, ttl_sec FROM approvals "
                "WHERE status='pending' AND (created_at + ttl_sec) > ? "
                "ORDER BY created_at DESC LIMIT ?",
                (cutoff, limit),
            ).fetchall()
        finally:
            c.close()
    out = []
    for r in rows:
        plan = json.loads(r[2]) if r[2] else {}
        out.append({
            "id": r[0],
            "created_at": r[1],
            "age_sec": cutoff - r[1],
            "remaining_sec": r[1] + r[4] - cutoff,
            "plan": plan,
            "status": r[3],
        })
    return out


def list_recent(limit: int = 20) -> list:
    """列出最近的所有审批 (含已决策的)"""
    with _lock:
        c = _conn()
        try:
            rows = c.execute(
                "SELECT id, created_at, plan_json, status, decided_by, decided_at "
                "FROM approvals ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            c.close()
    out = []
    for r in rows:
        plan = json.loads(r[2]) if r[2] else {}
        out.append({
            "id": r[0],
            "created_at": r[1],
            "plan": plan,
            "status": r[3],
            "decided_by": r[4],
            "decided_at": r[5],
        })
    return out


def mark_approved(approval_id: str, by: str) -> tuple:
    """标记为已批准. 返回 (ok, reason)"""
    with _lock:
        c = _conn()
        try:
            row = c.execute(
                "SELECT created_at, ttl_sec, status FROM approvals WHERE id=?",
                (approval_id,),
            ).fetchone()
            if not row:
                return False, "approval not found"
            created_at, ttl_sec, status = row
            if status != "pending":
                return False, f"current status={status}, cannot approve"
            if int(time.time()) - created_at > ttl_sec:
                c.execute("UPDATE approvals SET status='expired' WHERE id=?", (approval_id,))
                return False, "expired"
            c.execute(
                "UPDATE approvals SET status='approved', decided_by=?, decided_at=? WHERE id=?",
                (by, int(time.time()), approval_id),
            )
        finally:
            c.close()
    return True, "ok"


def mark_rejected(approval_id: str, by: str, reason: str = "") -> tuple:
    with _lock:
        c = _conn()
        try:
            row = c.execute(
                "SELECT status FROM approvals WHERE id=?", (approval_id,),
            ).fetchone()
            if not row:
                return False, "approval not found"
            if row[0] != "pending":
                return False, f"current status={row[0]}"
            result = {"reason": reason} if reason else {}
            c.execute(
                "UPDATE approvals SET status='rejected', decided_by=?, decided_at=?, result_json=? WHERE id=?",
                (by, int(time.time()), json.dumps(result, ensure_ascii=False), approval_id),
            )
        finally:
            c.close()
    return True, "ok"


def mark_executed(approval_id: str, result: dict) -> None:
    """记录执行结果"""
    with _lock:
        c = _conn()
        try:
            c.execute(
                "UPDATE approvals SET status=?, result_json=? WHERE id=?",
                ("executed" if result.get("ok") else "failed",
                 json.dumps(result, ensure_ascii=False, default=str),
                 approval_id),
            )
        finally:
            c.close()


# === v2.14: 诊断命令审批 (kind='diagnostic_cmd') ===

def create_diagnostic_pending(payload: dict,
                               ttl_sec: Optional[int] = None) -> str:
    """新建诊断命令审批记录, 返回 approval_id.

    payload 字段:
      kind: "ssh" | "kubectl_exec"
      node: ssh 目标节点 (kind=ssh 时必填)
      namespace: pod 所在 ns (kind=kubectl_exec 时必填)
      pod: pod 名 (kind=kubectl_exec 时必填)
      cmd: 完整 shell 命令
      reason: 给运维看的执行理由
      trace_id: LangGraph trace ID
      fingerprint: 故障指纹 (用于写入 diagnostic_cmd_history)
    """
    if ttl_sec is None:
        ttl_sec = DEFAULT_TTL_SEC
    aid = _gen_id()
    # plan_json / state_json 留空字符串而不是空对象, 避免现有 list_pending 渲染异常
    with _lock:
        c = _conn()
        try:
            c.execute(
                """INSERT INTO approvals
                (id, created_at, plan_json, state_json, status, ttl_sec,
                 kind, cmd_payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (aid, int(time.time()),
                 "{}", "{}",
                 "pending", ttl_sec,
                 "diagnostic_cmd",
                 json.dumps(payload, ensure_ascii=False, default=str)),
            )
        finally:
            c.close()
    return aid


def get_diagnostic(approval_id: str) -> Optional[dict]:
    """读取诊断命令审批记录."""
    with _lock:
        c = _conn()
        try:
            row = c.execute(
                "SELECT id, created_at, status, ttl_sec, decided_by, decided_at, "
                "kind, cmd_payload, execution_result FROM approvals WHERE id=?",
                (approval_id,),
            ).fetchone()
        finally:
            c.close()
    if not row:
        return None
    return {
        "id": row[0],
        "created_at": row[1],
        "status": row[2],
        "ttl_sec": row[3],
        "decided_by": row[4],
        "decided_at": row[5],
        "kind": row[6] or "remediation",
        "payload": json.loads(row[7]) if row[7] else None,
        "execution_result": json.loads(row[8]) if row[8] else None,
    }


def list_approved_diagnostic_pending(limit: int = 10) -> list:
    """daemon 拉到期任务: kind=diagnostic_cmd 且 status=approved
    且尚未执行 (execution_result IS NULL).
    """
    with _lock:
        c = _conn()
        try:
            rows = c.execute(
                "SELECT id, created_at, decided_by, cmd_payload, ttl_sec "
                "FROM approvals "
                "WHERE kind='diagnostic_cmd' AND status='approved' "
                "  AND execution_result IS NULL "
                "ORDER BY decided_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            c.close()
    out = []
    for r in rows:
        out.append({
            "id": r[0],
            "created_at": r[1],
            "decided_by": r[2],
            "payload": json.loads(r[3]) if r[3] else {},
            "ttl_sec": r[4],
        })
    return out


def mark_diagnostic_executed(approval_id: str, result: dict) -> None:
    """诊断命令 daemon 跑完写结果. result 是 dict: {exit_code, stdout_head,
    stderr_head, executed_at}.
    """
    with _lock:
        c = _conn()
        try:
            new_status = "executed" if result.get("exit_code") == 0 else "failed"
            c.execute(
                "UPDATE approvals SET status=?, execution_result=? WHERE id=?",
                (new_status,
                 json.dumps(result, ensure_ascii=False, default=str),
                 approval_id),
            )
        finally:
            c.close()


def expire_old_diagnostic(now: Optional[int] = None) -> int:
    """把超过 TTL 的 pending 诊断命令标记为 expired. 返回受影响行数."""
    if now is None:
        now = int(time.time())
    with _lock:
        c = _conn()
        try:
            cur = c.execute(
                "UPDATE approvals SET status='expired' "
                "WHERE kind='diagnostic_cmd' AND status='pending' "
                "  AND (created_at + ttl_sec) < ?",
                (now,),
            )
            return cur.rowcount or 0
        finally:
            c.close()
