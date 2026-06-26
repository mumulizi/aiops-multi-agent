"""故障 Memory: SQLite 持久化已成功修复的故障指纹, 同指纹故障下次秒级响应.

设计哲学:
- "重复故障 80%, 让 LLM 重新分析既贵又慢"  ←  生产 AIOps 实战经验
- 命中 Memory → 跳过 Investigator + Remediator (省 4-8 次 LLM 调用)
- 必须是"高置信度 + 已验证修复成功"的 case 才入库 (低质量 case 进 Memory 是污染)

指纹 (fingerprint) 生成:
  md5(namespace + alertname + rca_signature)[:12]
  - rca_signature: RCA 文本前 100 字符 (包含关键错误关键词)
  - 不用全 RCA 哈希: 同样的 OOM 不同 Pod 的 RCA 内容会有 Pod 名差异, 不能命中

Schema:
  fault_memory(
    fingerprint TEXT PRIMARY KEY,
    namespace TEXT,
    alertname TEXT,
    rca_text TEXT,          -- 完整 RCA (供下次复用)
    plan_json TEXT,         -- 修复 plan (action/safety_level/...)
    confidence TEXT,        -- "高/中/低", 只有"高"才会被复用
    hits INT DEFAULT 0,     -- 命中次数, 用于热度分析
    first_seen INT,
    last_used INT,
    last_success INT,       -- 上次成功修复的时间戳, 给 TTL 用
    ttl_sec INT             -- 默认 3600 (1h), 可按指纹定制
  )

API:
  generate_fingerprint(ns, alert, rca) → str
  lookup(fp) → dict or None       (1h TTL 自动过滤过期)
  record_success(fp, ns, alert, rca, plan, confidence)
  record_hit(fp)                   (命中复用时 +1, 更新 last_used)
  list_hot(limit=10)               (按 hits 排序的高频故障, 给运营看)
  forget(fp)                       (删除一条, 用于人工清理 / 失败模式排出)
"""
import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

DB_PATH = Path(os.getenv("FAULT_MEMORY_DB_PATH", "data/fault_memory.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

DEFAULT_TTL_SEC = int(os.getenv("FAULT_MEMORY_TTL_SEC", "3600"))  # 1 hour

_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=10, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""
        CREATE TABLE IF NOT EXISTS fault_memory (
            fingerprint TEXT PRIMARY KEY,
            namespace   TEXT NOT NULL,
            alertname   TEXT NOT NULL,
            rca_text    TEXT NOT NULL,
            plan_json   TEXT NOT NULL,
            confidence  TEXT NOT NULL,
            hits        INTEGER NOT NULL DEFAULT 0,
            first_seen  INTEGER NOT NULL,
            last_used   INTEGER NOT NULL,
            last_success INTEGER NOT NULL,
            ttl_sec     INTEGER NOT NULL DEFAULT 3600
        )
    """)
    c.execute("""CREATE INDEX IF NOT EXISTS idx_ns_alert
                 ON fault_memory(namespace, alertname)""")
    return c


def generate_fingerprint(namespace: str, alertname: str, rca: str) -> str:
    """生成故障指纹.

    rca 取前 100 字符做签名 (包含关键错误关键词, 但忽略 Pod 名差异).
    返回 md5 前 12 位.
    """
    # 归一化处理: 去多余空格, 转小写, 截断长度
    sig = (rca or "")[:100].strip().lower()
    sig = " ".join(sig.split())  # 多空格压缩成单空格
    payload = f"{namespace}|{alertname}|{sig}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:12]


def lookup(fingerprint: str, *, only_high_confidence: bool = True
           ) -> Optional[dict]:
    """查 Memory. 命中返回 dict, 未命中或过期返回 None.

    only_high_confidence=True: 仅返回置信度=高 的记录 (默认安全策略)
    """
    if not fingerprint:
        return None
    with _lock:
        c = _conn()
        try:
            row = c.execute(
                """SELECT fingerprint, namespace, alertname, rca_text, plan_json,
                          confidence, hits, first_seen, last_used, last_success, ttl_sec
                   FROM fault_memory WHERE fingerprint=?""",
                (fingerprint,),
            ).fetchone()
        finally:
            c.close()
    if not row:
        return None

    (fp, ns, alert, rca, plan_json, confidence,
     hits, first_seen, last_used, last_success, ttl_sec) = row

    # TTL 检查 (从 last_success 起算, 不是 first_seen)
    age = int(time.time()) - last_success
    if age > ttl_sec:
        return None

    # 置信度过滤
    if only_high_confidence and confidence != "高":
        return None

    return {
        "fingerprint": fp,
        "namespace": ns,
        "alertname": alert,
        "rca_text": rca,
        "plan": json.loads(plan_json),
        "confidence": confidence,
        "hits": hits,
        "first_seen": first_seen,
        "last_used": last_used,
        "last_success": last_success,
        "age_sec": age,
        "ttl_sec": ttl_sec,
    }


def record_success(fingerprint: str, namespace: str, alertname: str,
                   rca: str, plan: dict, confidence: str = "高",
                   ttl_sec: int = DEFAULT_TTL_SEC) -> None:
    """记录一次成功的修复 (Validator 标 success 时调用).

    若 fingerprint 已存在: 更新 plan/rca/last_success, 保留 hits.
    """
    if not fingerprint:
        return
    now = int(time.time())
    plan_json = json.dumps(plan, ensure_ascii=False)
    with _lock:
        c = _conn()
        try:
            existing = c.execute(
                "SELECT hits, first_seen FROM fault_memory WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if existing:
                hits, first_seen = existing
                c.execute(
                    """UPDATE fault_memory SET
                         rca_text=?, plan_json=?, confidence=?,
                         last_success=?, ttl_sec=?
                       WHERE fingerprint=?""",
                    (rca, plan_json, confidence, now, ttl_sec, fingerprint),
                )
            else:
                c.execute(
                    """INSERT INTO fault_memory
                       (fingerprint, namespace, alertname, rca_text, plan_json,
                        confidence, hits, first_seen, last_used, last_success, ttl_sec)
                       VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
                    (fingerprint, namespace, alertname, rca, plan_json,
                     confidence, now, now, now, ttl_sec),
                )
        finally:
            c.close()


def record_hit(fingerprint: str) -> None:
    """命中复用时调用: hits += 1, last_used = now."""
    if not fingerprint:
        return
    now = int(time.time())
    with _lock:
        c = _conn()
        try:
            c.execute(
                """UPDATE fault_memory
                   SET hits = hits + 1, last_used = ?
                   WHERE fingerprint=?""",
                (now, fingerprint),
            )
        finally:
            c.close()


def list_hot(limit: int = 10) -> list:
    """高频故障排行 (运营看板用)."""
    with _lock:
        c = _conn()
        try:
            rows = c.execute(
                """SELECT fingerprint, namespace, alertname, hits,
                          first_seen, last_used, confidence
                   FROM fault_memory
                   ORDER BY hits DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        finally:
            c.close()
    return [{
        "fingerprint": r[0], "namespace": r[1], "alertname": r[2],
        "hits": r[3], "first_seen": r[4], "last_used": r[5],
        "confidence": r[6],
    } for r in rows]


def forget(fingerprint: str) -> bool:
    """删除一条 Memory (人工运维: 这条修复方案不该再用)."""
    if not fingerprint:
        return False
    with _lock:
        c = _conn()
        try:
            cur = c.execute(
                "DELETE FROM fault_memory WHERE fingerprint=?",
                (fingerprint,),
            )
            return cur.rowcount > 0
        finally:
            c.close()


def stats() -> dict:
    """整体统计: 总条数 / 总命中数 / 平均命中数."""
    with _lock:
        c = _conn()
        try:
            row = c.execute(
                """SELECT COUNT(*), COALESCE(SUM(hits), 0)
                   FROM fault_memory"""
            ).fetchone()
        finally:
            c.close()
    total, total_hits = row
    return {
        "total_entries": total,
        "total_hits": total_hits,
        "avg_hits_per_entry": (total_hits / total) if total > 0 else 0,
    }
