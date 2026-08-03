import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lsm import LSM
from stats import Stats
from workload import generate

TMP = "/tmp/test_tombstone_lookup"


def _engine(deadline=None, memtable_size=50):
	shutil.rmtree(TMP, ignore_errors=True)
	stats = Stats()
	db = LSM(TMP, memtable_size=memtable_size, stats=stats, deadline=deadline,
		l0_capacity_bytes=50000)
	return db, stats


def test_reinserted_key_is_visible():
	# the case that makes a naive "tombstone filter hit -> not found" short-circuit
	# wrong: the key IS in a tombstone filter, but a newer PUT supersedes it
	db, stats = _engine(deadline=1000)
	db.delete(7)
	db.put(7, "resurrected")
	for i in range(20, 60):
		db.put(i, "filler")
	db.flush()
	assert db.tombstone_index.any_level_may_contain(7), "test premise: filter should hit"
	assert db.get(7) == "resurrected", "re-inserted key was hidden by its old tombstone"
	print("tslookup pass -- re-inserted key wins over its older tombstone")


def test_deleted_key_still_hidden():
	db, stats = _engine(deadline=1000)
	for i in range(100):
		db.put(i, "v" * 10)
	for i in range(0, 100, 2):
		db.delete(i)
	db.flush()
	for i in range(0, 100, 2):
		assert db.get(i) is None, f"deleted key {i} became visible"
	for i in range(1, 100, 2):
		assert db.get(i) == "v" * 10, f"live key {i} was lost"
	print("tslookup pass -- deletes hidden, live keys intact")


def test_hierarchy_skipped_when_no_tombstone():
	db, stats = _engine(deadline=1000)
	for i in range(500):
		db.put(i, "v" * 10)
	db.delete(9999)  # one tombstone for a key nobody queries
	db.flush()
	before = stats.tombstone_hierarchy_skips
	for i in range(500):
		db.get(i)
	skipped = stats.tombstone_hierarchy_skips - before
	assert skipped > 450, f"only {skipped}/500 lookups skipped the tombstone hierarchy"
	print(f"tslookup pass -- {skipped}/500 lookups skipped the tombstone hierarchy entirely")


def test_index_updated_after_compaction():
	# Tombstones move between levels on TTL expiry, and are retired once their key
	# is physically erased. Either way the filters must stay consistent with the
	# files actually resident, and the key must stay invisible.
	db, stats = _engine(deadline=100, memtable_size=8)
	for i in range(50):
		db.put(i, "v" * 10)
	for i in range(0, 50, 2):
		db.delete(i)
	for i in range(1000, 1400):  # drive the clock so TTLs expire and cascade
		db.put(i, "v")
	db.flush()

	# whatever the filters claim must match the tombstone files that exist
	resident = {
		rec.key
		for level in db.tombstone_levels for sst in level for rec in sst.records
	}
	for key in resident:
		assert db.tombstone_index.any_level_may_contain(key), \
			f"tombstone {key} is on disk but missing from the index"

	# and the deletes must still hold, whether via a surviving tombstone or
	# because the record was physically purged
	for i in range(0, 50, 2):
		assert db.get(i) is None, f"deleted key {i} resurfaced after compaction"
	for i in range(1, 50, 2):
		assert db.get(i) == "v" * 10, f"live key {i} was lost"
	print(f"tslookup pass -- index consistent with {len(resident)} resident tombstones, "
		f"{stats.tombstones_retired} retired after purge")


def test_matches_reference_under_workload():
	db, stats = _engine(deadline=300, memtable_size=60)
	reference = {}
	ops = generate(num_ops=4000, key_space=400, delete_ratio=0.3, skew="zipf", seed=21)
	for op, k, v in ops:
		if op == "put":
			db.put(k, v)
			reference[k] = v
		elif op == "delete":
			db.delete(k)
			reference[k] = None
	db.flush()
	wrong = [k for k in range(400) if db.get(k) != reference.get(k)]
	assert not wrong, f"{len(wrong)} keys disagree with reference: {wrong[:5]}"
	print(f"tslookup pass -- 400 keys match reference under a 4000-op workload")


def test_filter_memory_is_small():
	db, stats = _engine(deadline=1000)
	for i in range(2000):
		db.put(i, "v" * 20)
	for i in range(0, 2000, 4):
		db.delete(i)
	db.flush()
	tomb_bytes = sum(s.size_bytes for lvl in db.tombstone_levels for s in lvl)
	filter_bytes = db.tombstone_index.memory_bytes()
	assert filter_bytes < tomb_bytes * 0.1, \
		f"filters cost {filter_bytes}B against {tomb_bytes}B of tombstone files"
	print(f"tslookup pass -- filters {filter_bytes}B vs {tomb_bytes}B of tombstone data")


if __name__ == "__main__":
	test_reinserted_key_is_visible()
	test_deleted_key_still_hidden()
	test_hierarchy_skipped_when_no_tombstone()
	test_index_updated_after_compaction()
	test_matches_reference_under_workload()
	test_filter_memory_is_small()
