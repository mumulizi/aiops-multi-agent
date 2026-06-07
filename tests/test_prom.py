from tools.mock_tools import prometheus_query

print("=== Test 1: node memory ===")
print(prometheus_query("node_memory_MemAvailable_bytes"))
print()
print("=== Test 2: GPU util ===")
print(prometheus_query("DCGM_FI_DEV_GPU_UTIL"))
print()
print("=== Test 3: pod count ===")
