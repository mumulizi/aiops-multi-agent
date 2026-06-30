"""MetricsInspector Agent: 跑一组内置 PromQL 规则, 输出指标层异常 issue.

为什么需要:
- 旧 Inspector 只看 Pod phase/waiting/ready, 漏掉 "Pod Running 但慢/错" 的故障
- 真实生产中 80%+ 故障表现在指标层 (5xx 涨/延迟飙/资源吃满), 不是 Pod 崩
- 这个 Agent 跟 Inspector 并行跑, 输出 issue 合并进同一个调度队列

设计:
- 纯代码逻辑, 不调 LLM (跟 Inspector 一致)
- 单条规则查询失败 → 跳过 + 日志, 不阻塞其他规则
- 整体 crash → 由调度器 catch, 不阻塞 Inspector

v2.12+ fix: Stale metric 过滤
- Prometheus 可能保留已下线节点/已删 Pod 的旧指标 (TSDB retention 内, 指标 staleness 5min 期内仍可能返回)
- 这些 stale 指标转成 issue 会让 Investigator 浪费一步去 describe 找不到的 Pod
- 跑规则前用 K8s API 一次拿全集群 Pod/Node 名集合, metric label 不在集合里就丢
"""
import os
import sys

import httpx

from tools.metrics_rules import get_rules
from tools.langfuse_setup import TraceTimer

# 跟 mock_tools 用同一个 PromQL endpoint
VMSELECT_URL = os.getenv("PROM_BASE_URL",
                         "http://10.16.120.255:8481/select/1/prometheus")
TIMEOUT = 8


def _log(msg):
    print(msg, flush=True)
    sys.stdout.flush()


def _load_live_resources() -> tuple:
    """v2.12+ fix: 一次性拉全集群 Pod 和 Node 名集合, 用于 stale metric 过滤.

    返回 (pod_set, node_set):
    - pod_set: {(namespace, pod_name), ...} — 全集群存活 Pod
    - node_set: {node_name, ...} — Ready 节点名

    失败时返回 (None, None), 调用方应该不过滤 (退化为旧行为).
    """
    try:
        from tools.k8s_tools import _v1, _kube_ok
    except Exception:
        return None, None
    if not _kube_ok:
        return None, None
    try:
        pods = _v1.list_pod_for_all_namespaces(timeout_seconds=15).items
        nodes = _v1.list_node(timeout_seconds=10).items
    except Exception as e:
        _log(f"[MetricsInspector] K8s 查询失败, 跳过 stale 过滤: {e}")
        return None, None
    pod_set = {(p.metadata.namespace, p.metadata.name) for p in pods}
    node_set = {n.metadata.name for n in nodes}
    return pod_set, node_set


def _prom_query(query: str) -> list:
    """跑一次 PromQL, 返回原始结果列表 (result 数组). 失败返回 [].

    跟 tools.mock_tools.prometheus_query 区别:
    - 这里返回结构化数据, 上面返回字符串给 LLM
    """
    try:
        resp = httpx.get(
            f"{VMSELECT_URL}/api/v1/query",
            params={"query": query},
            timeout=TIMEOUT,
        )
        data = resp.json()
    except Exception:
        return []
    if data.get("status") != "success":
        return []
    return data.get("data", {}).get("result", []) or []


def _make_issue(rule: dict, labels: dict, value: float) -> dict:
    """把一条命中规则的时序转成 issue (跟 Pod issue 结构对齐).

    关键字段:
    - source="metrics" 用于下游区分
    - type 用 rule["type"] (PascalCase, 跟 Pod 的 CrashLoopBackOff 这种风格一致)
    - pod 字段尽量填: 有 pod label 就用, 否则填 "(metric)"
    """
    rid = rule["id"]
    rtype = rule["type"]
    threshold = rule["threshold"]
    unit = rule.get("unit", "")
    desc = rule.get("description", "")
    severity = rule.get("severity", "medium")

    # 取 namespace / pod / node
    ns = ""
    pod = "(metric)"
    node = ""
    if rule.get("label_for_ns"):
        ns = labels.get(rule["label_for_ns"], "") or ""
    if rule.get("label_for_pod"):
        pod = labels.get(rule["label_for_pod"], "") or "(metric)"
    if rule.get("label_for_node"):
        node = labels.get(rule["label_for_node"], "") or ""
        if not pod or pod == "(metric)":
            pod = f"node:{node}"
        # node 级规则没有 ns, 用集群占位
        if not ns:
            ns = "_node_"

    # ns 仍为空时 (apiserver/kubelet) 给个集群级占位 namespace
    if not ns:
        ns = "_cluster_"

    summary = (
        f"{rtype} {pod} 当前值 {round(value, 4)} {unit} "
        f"超阈值 {threshold} ({desc})"
    )

    return {
        "namespace": ns,
        "pod": pod,
        "type": rtype,
        "severity": severity,
        "summary": summary,
        "owner_kind": "Unknown",  # MetricsInspector 不查 owner
        "restarts": 0,
        "phase": "",
        "reason": "metric_anomaly",
        # === 新字段 ===
        "source": "metrics",
        "metric_id": rid,
        "metric_value": float(value),
        "metric_query": rule["query"],
        "metric_labels": dict(labels),
        "node": node,
    }


def _run_rule(rule: dict, live_pods=None, live_nodes=None) -> list:
    """跑单条规则, 返回 issue 列表 (可能多条时序命中).

    v2.12+ fix: live_pods/live_nodes 不为 None 时, 过滤掉指 stale 资源的时序.
    """
    series_list = _prom_query(rule["query"])
    if not series_list:
        return []
    issues = []
    stale_dropped = 0
    for series in series_list:
        labels = series.get("metric", {}) or {}
        value_pair = series.get("value", [None, None])
        try:
            value = float(value_pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        # 已经在 PromQL 里过滤了 > threshold, 这里再兜底一次 (== 的情况 PromQL 不过滤)
        cmp = rule.get("comparator", ">")
        threshold = rule["threshold"]
        if cmp == ">" and not (value > threshold):
            continue
        if cmp == "<" and not (value < threshold):
            continue
        if cmp == "==" and not (value == threshold):
            continue

        # v2.12+ fix: stale 检查 — Prom 还有指标但 K8s 里资源已经没了
        if live_pods is not None and rule.get("label_for_pod"):
            ns = labels.get(rule.get("label_for_ns", "namespace"), "")
            pod = labels.get(rule["label_for_pod"], "")
            if ns and pod and (ns, pod) not in live_pods:
                stale_dropped += 1
                continue
        if live_nodes is not None and rule.get("label_for_node") \
                and not rule.get("label_for_pod"):
            node = labels.get(rule["label_for_node"], "")
            # node label 可能带 :port 后缀 (instance="192.168.48.51:9100")
            node_name = node.split(":", 1)[0] if node else ""
            if node_name and node_name not in live_nodes:
                stale_dropped += 1
                continue

        issues.append(_make_issue(rule, labels, value))
    if stale_dropped:
        _log(f"[MetricsInspector]   ⓘ {rule['id']}: 丢弃 {stale_dropped} 条 stale "
             f"指标 (Prom 还有但 K8s 已无对应资源)")
    return issues


def run_metrics_inspector() -> list:
    """主入口: 跑全部内置规则, 返回 issue 列表.

    失败兜底:
    - 单条规则失败 → 跳过 + 日志
    - 整体异常 → 上层 catch
    """
    if os.getenv("METRICS_INSPECTOR_ENABLED", "true").lower() != "true":
        _log("[MetricsInspector] METRICS_INSPECTOR_ENABLED=false, 跳过")
        return []

    _log("=" * 60)
    _log("[MetricsInspector] 启动指标层巡检 (无 LLM)")
    _log("=" * 60)

    # v2.12+ fix: 先拉一次集群存活资源, 用于过滤 stale 指标
    live_pods, live_nodes = _load_live_resources()
    if live_pods is not None:
        _log(f"[MetricsInspector] 集群快照: {len(live_pods)} 个 Pod, "
             f"{len(live_nodes)} 个 Node (用于 stale 过滤)")
    else:
        _log("[MetricsInspector] K8s 不可用, 跳过 stale 过滤 (可能有过期 Pod 指标)")

    rules = get_rules()
    all_issues = []
    with TraceTimer("metrics_inspector", "run_all_rules") as t:
        for rule in rules:
            rid = rule["id"]
            try:
                with TraceTimer("metrics_inspector", f"rule:{rid}"):
                    rule_issues = _run_rule(rule, live_pods=live_pods,
                                             live_nodes=live_nodes)
            except Exception as e:
                _log(f"[MetricsInspector] rule {rid} 失败, 跳过: {e}")
                continue
            n = len(rule_issues)
            if n:
                _log(f"[MetricsInspector]  ✗ {rid}: {n} 个时序命中")
            else:
                _log(f"[MetricsInspector]  ✓ {rid}: 无异常")
            all_issues.extend(rule_issues)
        t.set_output({"total_issues": len(all_issues), "rules_run": len(rules)})

    # 简单展示
    if all_issues:
        sev_count = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for i in all_issues:
            sev_count[i["severity"]] = sev_count.get(i["severity"], 0) + 1
        _log(f"[MetricsInspector] 共 {len(all_issues)} 个指标异常, "
             f"严重度分布: {sev_count}")
        show_n = min(10, len(all_issues))
        _log(f"[MetricsInspector] Top {show_n}:")
        for idx in range(show_n):
            i = all_issues[idx]
            _log(f"  {idx+1}. [{i['severity']}] {i['summary']}")
    else:
        _log("[MetricsInspector] 全部规则通过, 无指标异常")

    return all_issues
