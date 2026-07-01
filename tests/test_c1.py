import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from workload import generate

def test_op_counts():
	ops = generate(num_ops=1000, delete_ratio=0.2, get_ratio=0.3)
	puts = sum(1 for o in ops if o[0] == "put")
	deletes = sum(1 for o in ops if o[0] == "delete")
	gets = sum(1 for o in ops if o[0] == "get")
	total = puts + deletes + gets
	assert total == 1000
	# allow 5% tolerance around expected ratios
	assert abs(deletes / 1000 - 0.2) < 0.05, f"delete ratio off: {deletes/1000:.2f}"
	assert abs(gets / 1000 - 0.3) < 0.05, f"get ratio off: {gets/1000:.2f}"
	print(f"C1 pass -- puts={puts} deletes={deletes} gets={gets} (total={total})")

def test_key_space_bounded():
	ops = generate(num_ops=5000, key_space=100)
	keys = [o[1] for o in ops]
	assert min(keys) >= 0
	assert max(keys) < 100
	print(f"C1 pass -- all keys in [0, 99], min={min(keys)} max={max(keys)}")

def test_reproducible_with_seed():
	ops1 = generate(num_ops=500, seed=7)
	ops2 = generate(num_ops=500, seed=7)
	assert ops1 == ops2
	print("C1 pass -- same seed produces identical workload")

def test_different_seeds_differ():
	ops1 = generate(num_ops=500, seed=1)
	ops2 = generate(num_ops=500, seed=2)
	assert ops1 != ops2
	print("C1 pass -- different seeds produce different workloads")

def test_zipf_skew():
	ops = generate(num_ops=5000, key_space=500, skew="zipf", zipf_param=1.2)
	keys = [o[1] for o in ops]
	# with zipf skew, the most common key should appear far more than average
	from collections import Counter
	counts = Counter(keys)
	most_common_count = counts.most_common(1)[0][1]
	avg_count = len(keys) / 500
	assert most_common_count > avg_count * 5, "zipf skew not showing concentration"
	print(f"C1 pass -- zipf skew: most common key appears {most_common_count}x vs avg {avg_count:.1f}x")

if __name__ == "__main__":
	test_op_counts()
	test_key_space_bounded()
	test_reproducible_with_seed()
	test_different_seeds_differ()
	test_zipf_skew()
