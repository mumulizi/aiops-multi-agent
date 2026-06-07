from agents.inspector import run_inspector

# 新版三阶段巡检
# top_n=None 表示返回所有真实异常 (生产用)
# top_n=10 仅看前 10 个 (调试用)
issues = run_inspector(top_n=None, deep_max_steps=4)

print()
print("=" * 60)
print(f"最终拿到 {len(issues)} 个 issues 对象")
print("=" * 60)
if issues:
    print(f"前 3 个详细结构:")
    for i, issue in enumerate(issues[:3]):
        print(f"  {i+1}. {issue}")
