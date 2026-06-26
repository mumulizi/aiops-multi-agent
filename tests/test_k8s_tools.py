from tools.k8s_tools import (
    list_unhealthy_pods,
    get_cluster_overview,
    describe_pod_real,
    list_high_restart_pods,
)

print("=" * 60)
print("Test 1: 集群总览")
print("=" * 60)
print(get_cluster_overview())
print()

print("=" * 60)
print("Test 2: 异常 Pod 列表")
print("=" * 60)
print(list_unhealthy_pods(limit=10))
print()

print("=" * 60)
print("Test 3: 高重启 Pod")
print("=" * 60)
print(list_high_restart_pods(threshold=50))
print()

print("=" * 60)
print("Test 4: 真实 Pod 详情(用一个真实异常 Pod)")
print("=" * 60)
print(describe_pod_real(name="abcd-abcd-task-manager-0", namespace="abcd"))
