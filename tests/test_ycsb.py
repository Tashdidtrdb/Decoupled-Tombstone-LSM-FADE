import collections
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ycsb import (WORKLOADS, ZipfianGenerator, describe, generate_ycsb,
	make_key_generator)
import random


def test_no_clamping_artefact():
	# numpy's unbounded zipf had to be clamped to the key space, which piled the
	# whole tail onto the last key -- over 20% of operations at zipf_param=1.2.
	# The bounded formulation must not do that.
	ops = generate_ycsb(workload="X", num_ops=20000, key_space=1000,
		distribution="zipfian", seed=1, load_phase=False)
	counts = collections.Counter(k for _, k, _ in ops)
	last_key_share = counts[999] / len(ops)
	assert last_key_share < 0.01, f"last key absorbs {100*last_key_share:.1f}% of ops"
	print(f"ycsb pass -- last key takes {100*last_key_share:.2f}% of operations, not the tail")


def test_zipfian_is_actually_skewed():
	ops = generate_ycsb(workload="X", num_ops=20000, key_space=1000,
		distribution="zipfian", seed=1, load_phase=False)
	counts = collections.Counter(k for _, k, _ in ops)
	hottest = counts.most_common(1)[0][1] / len(ops)
	assert hottest > 0.05, f"hottest key only {100*hottest:.1f}% -- not skewed"
	# and it should decay, not be a single spike
	top = [c for _, c in counts.most_common(5)]
	assert top == sorted(top, reverse=True) and top[0] > top[4]
	print(f"ycsb pass -- hottest key {100*hottest:.1f}%, distribution decays smoothly")


def test_uniform_is_flat():
	ops = generate_ycsb(workload="X", num_ops=20000, key_space=1000,
		distribution="uniform", seed=1, load_phase=False)
	counts = collections.Counter(k for _, k, _ in ops)
	hottest = counts.most_common(1)[0][1] / len(ops)
	assert hottest < 0.01, f"uniform distribution has a {100*hottest:.1f}% hot key"
	assert len(counts) > 950, f"only {len(counts)} of 1000 keys touched"
	print(f"ycsb pass -- uniform touches {len(counts)}/1000 keys, no hot spot")


def test_latest_favours_recent_keys():
	ops = generate_ycsb(workload="X", num_ops=10000, key_space=1000,
		distribution="latest", seed=1, load_phase=False)
	counts = collections.Counter(k for _, k, _ in ops)
	hottest_key = counts.most_common(1)[0][0]
	assert hottest_key > 900, f"latest distribution peaked at key {hottest_key}"
	print(f"ycsb pass -- latest distribution peaks at key {hottest_key}")


def test_workload_mixes_match_their_definition():
	for name in ["A", "B", "C", "X", "X-heavy"]:
		ops = generate_ycsb(workload=name, num_ops=10000, key_space=500,
			seed=1, load_phase=False)
		summary = describe(ops)
		expected = WORKLOADS[name]
		actual_deletes = summary["deletes"] / summary["total"]
		assert abs(actual_deletes - expected["delete"]) < 0.03, \
			f"{name}: delete ratio {actual_deletes:.2f} vs expected {expected['delete']}"
		actual_reads = summary["gets"] / summary["total"]
		assert abs(actual_reads - expected["read"]) < 0.03, \
			f"{name}: read ratio {actual_reads:.2f} vs expected {expected['read']}"
	print("ycsb pass -- all workload mixes match their declared ratios")


def test_load_phase_populates_key_space():
	ops = generate_ycsb(workload="X", num_ops=100, key_space=200, seed=1)
	loaded = [k for op, k, _ in ops[:200] if op == "put"]
	assert sorted(loaded) == list(range(200)), "load phase did not insert every key"
	print("ycsb pass -- load phase inserts every key exactly once")


def test_value_size_is_honoured():
	ops = generate_ycsb(workload="A", num_ops=200, key_space=50,
		value_size=64, seed=1, load_phase=False)
	sizes = {len(v) for op, _, v in ops if op == "put"}
	assert sizes == {64}, f"unexpected value sizes: {sizes}"
	print("ycsb pass -- generated values honour the requested size")


def test_reproducible():
	a = generate_ycsb(workload="X", num_ops=2000, key_space=300, seed=99)
	b = generate_ycsb(workload="X", num_ops=2000, key_space=300, seed=99)
	c = generate_ycsb(workload="X", num_ops=2000, key_space=300, seed=100)
	assert a == b, "same seed produced different workloads"
	assert a != c, "different seeds produced identical workloads"
	print("ycsb pass -- generation is reproducible and seed-sensitive")


def test_keys_stay_in_range():
	for dist in ["uniform", "zipfian", "latest"]:
		gen = make_key_generator(dist, 500, random.Random(3))
		keys = [gen.next_key() for _ in range(5000)]
		assert all(0 <= k < 500 for k in keys), f"{dist} produced out-of-range keys"
	print("ycsb pass -- all distributions stay within the key space")


def test_unknown_names_rejected():
	for bad in [("workload", "Z"), ("distribution", "gaussian")]:
		try:
			generate_ycsb(**{bad[0]: bad[1]}, num_ops=10, key_space=10)
			assert False, f"accepted unknown {bad[0]} {bad[1]!r}"
		except ValueError:
			pass
	print("ycsb pass -- unknown workload and distribution names are rejected")


if __name__ == "__main__":
	test_no_clamping_artefact()
	test_zipfian_is_actually_skewed()
	test_uniform_is_flat()
	test_latest_favours_recent_keys()
	test_workload_mixes_match_their_definition()
	test_load_phase_populates_key_space()
	test_value_size_is_honoured()
	test_reproducible()
	test_keys_stay_in_range()
	test_unknown_names_rejected()
