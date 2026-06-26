from tools.k8s_tools import get_pod_logs

# 测试一个真实 CrashLoopBackOff 的 Pod
print("=" * 60)
print("Test: xxxx-xxxxx 日志")
print("=" * 60)
result = get_pod_logs(
    name="xxxxxxx-68b7bcb984-tkxlz",
    namespace="xxxx-xxxx",
    lines=30,
)
print(result)

print()
print("=" * 60)
print("Test: kube-external-auditor 日志")
print("=" * 60)
result = get_pod_logs(
    name="kube-external-auditor-192.168.48.78",
    namespace="kube-system",
    lines=30,
)
print(result)
