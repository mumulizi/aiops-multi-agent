"""IM notification (default: Baidu Infoflow, supports DingTalk/WeCom/Feishu).

Design:
- Upper layer only calls send_message(text)
- Switch IM provider via env var
- Always write local audit file (alerts/<timestamp>.txt)
- Never raises (notification failure should not break main flow)

Env vars:
  IM_PROVIDER       infoflow / dingtalk / wecom / feishu / none (default none)
  IM_WEBHOOK_URL    full webhook URL (with access_token etc.)
  IM_TOID           [infoflow only] group id JSON array, e.g. "[10246761]"
  ALERT_LOG_DIR     local audit dir (default alerts/)
"""
import json
import os
import time
from pathlib import Path

import httpx

IM_PROVIDER = os.getenv("IM_PROVIDER", "none").lower()
IM_WEBHOOK_URL = os.getenv("IM_WEBHOOK_URL", "")
IM_TOID = os.getenv("IM_TOID", "")
ALERT_LOG_DIR = Path(os.getenv("ALERT_LOG_DIR", "alerts"))
ALERT_LOG_DIR.mkdir(exist_ok=True)


# ===== Provider adapters =====

def _build_infoflow_payload(text: str) -> dict:
    """Infoflow: header.toid + body[].content"""
    toid_list = []
    if IM_TOID:
        try:
            toid_list = json.loads(IM_TOID)
        except Exception:
            pass
    return {
        "message": {
            "header": {"toid": toid_list},
            "body": [{"content": text, "type": "TEXT"}],
        }
    }


def _build_dingtalk_payload(text: str) -> dict:
    return {"msgtype": "text", "text": {"content": text}}


def _build_wecom_payload(text: str) -> dict:
    return {"msgtype": "text", "text": {"content": text}}


def _build_feishu_payload(text: str) -> dict:
    return {"msg_type": "text", "content": {"text": text}}


_BUILDERS = {
    "infoflow": _build_infoflow_payload,
    "dingtalk": _build_dingtalk_payload,
    "wecom": _build_wecom_payload,
    "feishu": _build_feishu_payload,
}


# ===== Local file fallback =====

def _write_local_alert(text: str) -> str:
    ts = time.strftime("%Y%m%d-%H%M%S-") + str(int(time.time() * 1000) % 1000)
    path = ALERT_LOG_DIR / f"{ts}.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)


# ===== Public API =====

def send_message(text: str, *, write_local: bool = True) -> dict:
    """Send text message. Never raises.
    Returns: {im_sent, im_status, local_file, error}
    """
    result = {
        "im_sent": False,
        "im_status": None,
        "local_file": None,
        "error": None,
    }

    # 1. Local audit file (fallback)
    if write_local:
        try:
            result["local_file"] = _write_local_alert(text)
        except Exception as e:
            result["error"] = f"local write failed: {e}"

    # 2. IM push
    if IM_PROVIDER == "none" or not IM_WEBHOOK_URL:
        return result

    builder = _BUILDERS.get(IM_PROVIDER)
    if builder is None:
        result["error"] = f"unsupported IM_PROVIDER: {IM_PROVIDER}"
        return result

    try:
        payload = builder(text)
        resp = httpx.post(
            IM_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        result["im_status"] = resp.status_code
        result["im_sent"] = 200 <= resp.status_code < 300
        if not result["im_sent"]:
            body_preview = resp.text[:200] if resp.text else ""
            result["error"] = f"IM status {resp.status_code}: {body_preview}"
    except Exception as e:
        result["error"] = f"IM request failed: {type(e).__name__}: {e}"

    return result


def format_alert_message(state: dict) -> str:
    """Build a concise text message from notifier state."""
    sev = (state.get("severity") or "?").upper()
    label = state.get("label") or "?"
    summary = state.get("event_summary") or "(no summary)"
    rca = state.get("rca_hypothesis") or ""
    trace_id = state.get("trace_id") or ""

    icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵"}.get(sev, "⚪")

    # v2.8: 多集群部署区分告警来源 (REGION 环境变量, 默认 "default")
    from tools.llm_factory import get_region
    region = get_region()

    lines = []
    lines.append(f"{icon} [{sev}] [{region}] AIOps 告警")
    lines.append("")

    # Pull namespace+pod from raw_alerts if present
    raw = state.get("raw_alerts") or []
    if raw:
        labels = (raw[0].get("labels") if isinstance(raw[0], dict) else {}) or {}
        ns = labels.get("namespace", "")
        pod = labels.get("instance", "")
        if ns and pod:
            lines.append(f"📍 {ns} / {pod}")

    lines.append(f"📝 {summary}")

    if rca:
        rca_short = rca if len(rca) <= 250 else rca[:247] + "..."
        lines.append(f"🔍 根因: {rca_short}")

    # Remediation plan
    plan = state.get("remediation_plan") or {}
    action = plan.get("action", "")
    safety = plan.get("safety_level", "")
    if action and action != "none":
        lines.append(f"🔧 修复方案: {action} ({safety})")

    # Approval
    decision = state.get("approval_decision", "")
    if decision == "executor":
        lines.append("🟢 安全门: ✓ 自动执行")
    elif decision == "human_review":
        approval_reason = state.get("approval_reason", "")
        lines.append(f"⚠️ 安全门: 待人审 ({approval_reason})")
    elif decision == "reject":
        lines.append("🚫 安全门: 已拒绝高危操作")

    # Execution
    exec_status = state.get("execution_status", "")
    if exec_status == "executed":
        exec_log = state.get("execution_log", "")
        log_short = exec_log if len(exec_log) <= 150 else exec_log[:147] + "..."
        lines.append(f"⚡ 执行: ✓ {log_short}")
    elif exec_status == "dry_run":
        lines.append("🧪 执行: dry-run (模拟, 未真动手)")
    elif exec_status == "failed":
        exec_log = state.get("execution_log", "")
        lines.append(f"❌ 执行失败: {exec_log[:150]}")

    # Validation
    validation = state.get("validation_result") or {}
    v_status = validation.get("status", "")
    v_reason = validation.get("reason", "")
    if v_status == "success":
        lines.append(f"✅ 验证: 修复生效 ({v_reason[:100]})")
    elif v_status == "failed":
        lines.append(f"❗ 验证失败: {v_reason[:100]}")
    elif v_status == "pending":
        lines.append(f"⏳ 验证: 重建中 ({v_reason[:100]})")

    if trace_id:
        lines.append("")
        lines.append(f"🆔 trace: {trace_id}")

    return "\n".join(lines)


def should_push(state: dict) -> bool:
    """Decide whether to push to IM. Avoid spam.

    Push when:
    - severity is critical or high
    - any real execution happened (executed / failed)
    - approval rejected high-risk action
    - L2 human_review needs attention (pending approval)
    """
    sev = state.get("severity") or "low"
    if sev in ("critical", "high"):
        return True
    exec_status = state.get("execution_status")
    if exec_status in ("executed", "failed"):
        return True
    decision = state.get("approval_decision")
    if decision in ("reject", "human_review"):
        return True
    return False


def format_approval_message(approval_id: str, plan: dict, state: dict, ttl_min: int) -> str:
    """Build an approval-request message with manual CLI commands."""
    action = plan.get("action", "?")
    target = plan.get("target", "?")
    safety = plan.get("safety_level", "?")
    rationale = plan.get("rationale", "")
    rollback = plan.get("rollback", "")

    sev = (state.get("severity") or "?").upper()
    summary = state.get("event_summary", "")
    rca = state.get("rca_hypothesis") or ""

    lines = []
    lines.append(f"⚠️  [APPROVAL NEEDED] L2 操作待审批")
    lines.append(f"approval_id: {approval_id}    severity: {sev}    TTL: {ttl_min} 分钟")
    lines.append("")
    lines.append(f"📋 操作: {action}    target: {target}    safety: {safety}")
    if summary:
        lines.append(f"📝 事件: {summary}")
    if rca:
        rca_short = rca if len(rca) <= 200 else rca[:197] + "..."
        lines.append(f"🔍 根因: {rca_short}")
    if rationale:
        lines.append(f"💡 修复理由: {rationale[:200]}")
    if rollback and rollback not in ("n/a", "N/A"):
        lines.append(f"⏪ 回滚方案: {rollback[:150]}")
    lines.append("")
    lines.append("=== 在服务器上执行 (项目目录) ===")
    lines.append(f"批准: uv run python -m scripts.aiops_review approve {approval_id}")
    lines.append(f"拒绝: uv run python -m scripts.aiops_review deny {approval_id}")
    lines.append(f"详情: uv run python -m scripts.aiops_review show {approval_id}")
    return "\n".join(lines)
