"""端到端测试: 模拟一批 Alertmanager 告警走完整 4 Agent 流水线"""
import uuid
from graph import build_graph


def main():
    sample_alerts = [
        {
            "labels": {
                "alertname": "PodOOMKilled",
                "severity": "critical",
                "namespace": "default",
                "instance": "nginx-7d8-abcd",
            },
            "annotations": {
                "summary": "Pod nginx-7d8 OOMKilled",
                "description": "container memory exceeded limit 512Mi (used 530Mi)",
            },
            "startsAt": "2026-06-07T10:00:00Z",
        },
        {
            "labels": {
                "alertname": "DeploymentReplicaUnhealthy",
                "severity": "high",
                "namespace": "default",
                "instance": "deployment/nginx",
            },
            "annotations": {
                "summary": "Deployment nginx 副本数不足",
                "description": "available 1/3 replicas",
            },
            "startsAt": "2026-06-07T10:00:30Z",
        },
    ]

    trace_id = str(uuid.uuid4())[:8]
    initial_state = {
        "raw_alerts": sample_alerts,
        "trace_id": trace_id,
        "alert_count": 0,
        "evidence": [],
        "notification_sent": False,
    }

    app = build_graph()
    print()
    print(f">>> 启动告警分析流水线 (trace_id={trace_id})")
    print()
    final_state = app.invoke(initial_state)
    sent = final_state.get("notification_sent")
    print()
    print(f">>> 流水线结束, notification_sent={sent}")


if __name__ == "__main__":
    main()
