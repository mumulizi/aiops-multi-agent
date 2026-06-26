"""巡检忽略策略 (v2.6).

让运维通过 YAML 配置告诉巡检 Agent: "这些 namespace / Pod 不要管".

YAML 格式 (config/policies.yaml):

  ignores:
    - namespace: "monitoring"             # 整个 namespace 忽略
      reason: "由监控团队独立维护"

    - namespace: "default"                # namespace + 精确 pod 名
      pod: "my-debug-pod"
      reason: "已知 debug Pod, 不修"

    - namespace: "test"                   # namespace + pod glob 通配
      pod_pattern: "load-test-*"
      reason: "压测 Pod"

匹配规则:
- 列表里任一规则匹配 → 该 issue 被忽略 (return True)
- 单条规则内: namespace 必填, pod / pod_pattern 二选一 (都不写则匹配整个 ns)
- pod 是精确字符串匹配
- pod_pattern 是 fnmatch glob (* 任意, ? 单字符, [abc] 字符集)

设计原则:
- YAML 文件不存在 / 格式错 → 返回空策略 + 打印警告, 不阻塞主流程
- 改了配置不需要重启, 每轮巡检都重新加载 (load_policies 不缓存)
- 不抛异常, 出错时返回 (False, "") 让 issue 正常进流水线
"""
import fnmatch
import os
import sys
from pathlib import Path
from typing import Tuple

DEFAULT_POLICIES_FILE = os.getenv("POLICIES_FILE", "config/policies.yaml")


def _log(msg: str) -> None:
    print(msg, flush=True)
    sys.stdout.flush()


def load_policies(path: str = None) -> dict:
    """加载 YAML 策略文件. 出错时返回空策略 (不影响主流程).

    返回 dict: {"ignores": [...]} 或 {} (出错时)
    """
    p = path or DEFAULT_POLICIES_FILE
    file_path = Path(p)
    if not file_path.exists():
        # 文件没有就是 "无策略" 的合法状态, 不打印警告
        return {}
    try:
        import yaml
    except ImportError:
        _log(f"[Policy] PyYAML 未安装, 跳过策略加载 ({p})")
        return {}
    try:
        with file_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        _log(f"[Policy] ⚠ 加载 {p} 失败: {e}, 当作无策略处理")
        return {}
    if not isinstance(data, dict):
        _log(f"[Policy] ⚠ {p} 顶层不是 dict, 当作无策略")
        return {}
    return data


def _match_one_rule(rule: dict, ns: str, pod: str) -> bool:
    """单条规则与 (ns, pod) 的匹配.

    规则字段:
    - namespace: 必填 (空字符串视为不匹配)
    - pod: 精确匹配 (可选)
    - pod_pattern: glob 匹配 (可选)
    - 都不写 pod/pod_pattern → 整 ns 忽略 (任意 pod 都命中)
    """
    if not isinstance(rule, dict):
        return False
    rule_ns = rule.get("namespace", "")
    if not rule_ns or rule_ns != ns:
        return False
    # 整 ns 模式: 没指定 pod / pod_pattern → ns 命中即视为整组忽略
    if "pod" not in rule and "pod_pattern" not in rule:
        return True
    # 精确 pod 匹配
    if "pod" in rule:
        if rule["pod"] == pod:
            return True
    # glob pod_pattern 匹配
    if "pod_pattern" in rule:
        pat = rule.get("pod_pattern", "")
        if pat and fnmatch.fnmatchcase(pod, pat):
            return True
    return False


def should_ignore(issue: dict, policies: dict) -> Tuple[bool, str]:
    """判断一条 issue 是否被忽略.

    返回 (是否忽略 bool, 命中规则的 reason 字符串)
    """
    if not policies or not isinstance(policies, dict):
        return False, ""
    ignores = policies.get("ignores") or []
    if not ignores:
        return False, ""
    ns = (issue or {}).get("namespace", "") or ""
    pod = (issue or {}).get("pod", "") or ""
    if not ns:
        return False, ""
    for rule in ignores:
        if _match_one_rule(rule, ns, pod):
            reason = rule.get("reason", "(无理由说明)")
            # 显式标注命中的规则形态, 方便审计
            if "pod" in rule:
                tag = f"ns={ns}+pod={rule['pod']}"
            elif "pod_pattern" in rule:
                tag = f"ns={ns}+pattern={rule['pod_pattern']}"
            else:
                tag = f"整 ns={ns}"
            return True, f"[{tag}] {reason}"
    return False, ""


def filter_issues(issues: list, policies: dict) -> Tuple[list, list]:
    """过滤掉所有匹配忽略策略的 issue.

    返回: (kept_issues, ignored_log)
      kept_issues: 进流水线的 issue 列表
      ignored_log: [{"pod": "...", "namespace": "...", "reason": "..."}, ...]
    """
    if not issues:
        return [], []
    kept = []
    ignored = []
    for it in issues:
        ok, reason = should_ignore(it, policies)
        if ok:
            ignored.append({
                "namespace": it.get("namespace", ""),
                "pod": it.get("pod", ""),
                "reason": reason,
            })
        else:
            kept.append(it)
    return kept, ignored
