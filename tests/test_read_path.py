import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lsm import LSM
from stats import Stats
from workload import generate

TMP = "/tmp/test_read_path"


def _engine(deadline=None, **kw):
	shutil.rmtree(TMP, ignore_errors=True)
	stats = Stats()
	db = LSM(TMP, memtable_size=kw.pop("memtable_size", 100), stats=stats,
		deadline=deadline, l0_capacity_bytes=kw.pop("l0_capacity_bytes", 50000), **kw)
	return db, stats


def test_correctness_unchanged_by_filters():
	# the filters must never change an answer, only the cost of getting it
	db, stats = _engine()
	reference = {}
	ops = generate(num_ops=3000, key_space=300, delete_ratio=0.25, skew="zipf", seed=11)
	for op, k, v in ops:
		if op == "put":
			db.put(k, v)
			reference[k] = v
		elif op == "delete":
			db.delete(k)
			reference[k] = None
	db.flush()
	wrong = [k for k in range(300) if db.get(k) != reference.get(k)]
	assert not wrong, f"{len(wrong)} keys returned wrong values: {wrong[:5]}"
	print(f"readpath pass -- all 300 keys correct with filters active")


def test_filter_skips_happen():
	# keys must be written in random order: sequential writes produce files with
	# disjoint key ranges, and the cheap min/max check rejects those before the
	# filter is ever consulted. The filter earns its keep when ranges overlap.
	import random
	rng = random.Random(5)
	db, stats = _engine()
	keys = list(range(4000))
	rng.shuffle(keys)
	for k in keys:
		db.put(k, "v" * 20)
	db.flush()
	for k in range(0, 4000, 7):
		db.get(k)
	assert stats.filter_skips > 0, "filters never ruled out a file"
	print(f"readpath pass -- filter ruled out {stats.filter_skips} files with zero I/O")


def test_no_false_negatives_end_to_end():
	# every key that was written must still be findable; a false negative in the
	# filter would silently turn a live key into a miss
	db, stats = _engine()
	keys = list(range(0, 3000, 3))
	for k in keys:
		db.put(k, f"value{k}")
	db.flush()
	missing = [k for k in keys if db.get(k) != f"value{k}"]
	assert not missing, f"{len(missing)} keys lost: {missing[:5]}"
	print(f"readpath pass -- all {len(keys)} written keys still retrievable")


def test_observed_fpr_is_low():
	# random insertion order so files overlap and the filter is actually exercised
	import random
	rng = random.Random(9)
	db, stats = _engine()
	present = list(range(0, 4000, 2))  # even keys only
	rng.shuffle(present)
	for k in present:
		db.put(k, "v" * 20)
	db.flush()
	queries = list(range(1, 4000, 2))  # query odd keys, all absent
	for k in queries:
		db.get(k)
	# Every key queried here is absent, so any block we read is by definition a
	# false positive -- observed_fpr() is ~1.0 and says nothing useful. What matters
	# is how RARELY the filter let a probe through at all, per (query, file) pair.
	candidates = stats.filter_skips + stats.block_reads
	leak_rate = stats.block_reads / candidates
	assert leak_rate < 0.05, f"filter let through {leak_rate:.3f} of absent-key probes"
	assert stats.filter_skips > candidates * 0.9, "filter skipped too few files"
	print(f"readpath pass -- filter blocked {100*(1-leak_rate):.1f}% of absent-key probes "
		f"({stats.block_reads} leaked of {candidates})")


def test_block_reads_are_smaller_than_files():
	db, stats = _engine()
	for i in range(3000):
		db.put(i, "v" * 30)
	db.flush()
	for i in range(0, 3000, 5):
		db.get(i)
	assert stats.block_reads > 0
	avg_block = stats.block_bytes_read / stats.block_reads
	total_bytes = sum(s.size_bytes for lvl in db.data_levels for s in lvl)
	avg_file = total_bytes / max(sum(len(lvl) for lvl in db.data_levels), 1)
	assert avg_block < avg_file, f"avg block {avg_block} not smaller than avg file {avg_file}"
	print(f"readpath pass -- avg block read {avg_block:.0f}B vs avg file {avg_file:.0f}B")


def test_deleted_keys_still_return_none():
	db, stats = _engine(deadline=500)
	for i in range(500):
		db.put(i, "v" * 20)
	for i in range(0, 500, 2):
		db.delete(i)
	db.flush()
	for i in range(0, 500, 2):
		assert db.get(i) is None, f"deleted key {i} still visible"
	for i in range(1, 500, 2):
		assert db.get(i) == "v" * 20, f"live key {i} lost"
	print("readpath pass -- deletes and live keys both correct through the filter path")


if __name__ == "__main__":
	test_correctness_unchanged_by_filters()
	test_filter_skips_happen()
	test_no_false_negatives_end_to_end()
	test_observed_fpr_is_low()
	test_block_reads_are_smaller_than_files()
	test_deleted_keys_still_return_none()
