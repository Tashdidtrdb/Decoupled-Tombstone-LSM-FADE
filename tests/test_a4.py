import os
import sys
import shutil
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lsm import LSM

DATA_DIR = "/tmp/test_a4_data"

def fresh():
	shutil.rmtree(DATA_DIR, ignore_errors=True)
	# tiny capacities so compaction triggers after just a couple flushes
	# memtable_size=10, l0_capacity_bytes=1 forces compaction after every flush
	return LSM(DATA_DIR, memtable_size=10, num_levels=3, l0_capacity_bytes=1)

def cleanup():
	shutil.rmtree(DATA_DIR, ignore_errors=True)

def test_compaction_runs():
	db = fresh()
	for i in range(50):
		db.put(i, f"val{i}")
	db.flush()

	assert db.stats.compaction_count > 0, "no compactions ran"
	print(f"A4 pass -- compaction ran {db.stats.compaction_count} times")
	cleanup()

def test_keys_readable_after_compaction():
	db = fresh()
	for i in range(50):
		db.put(i, f"val{i}")
	db.flush()

	for i in range(50):
		assert db.get(i) == f"val{i}", f"key {i} missing after compaction"
	print("A4 pass -- all keys readable after compaction")
	cleanup()

def test_deleted_keys_gone_after_compaction():
	db = fresh()
	for i in range(30):
		db.put(i, f"val{i}")
	for i in range(10):
		db.delete(i)
	db.flush()

	for i in range(10):
		assert db.get(i) is None, f"key {i} should be deleted but got a value"
	for i in range(10, 30):
		assert db.get(i) == f"val{i}", f"key {i} should still exist"
	print("A4 pass -- deleted keys gone, live keys intact after compaction")
	cleanup()

def test_shadowed_versions_collapsed():
	db = fresh()
	# write key 5 multiple times across multiple flushes
	for version in range(5):
		db.put(5, f"v{version}")
		db.flush()

	# only the latest version should survive
	result = db.get(5)
	assert result == "v4", f"expected v4, got {result}"
	print("A4 pass -- shadowed versions collapsed, only newest survives")
	cleanup()

def test_waf_greater_than_one():
	db = fresh()
	for i in range(100):
		db.put(i, f"val{i}")
	db.flush()

	# compaction rewrites data, so bytes_written > bytes_ingested
	assert db.stats.bytes_written > db.stats.bytes_ingested, "WAF should be > 1 with compaction"
	print(f"A4 pass -- WAF = {db.stats.waf():.2f} (bytes_written={db.stats.bytes_written}, bytes_ingested={db.stats.bytes_ingested})")
	cleanup()

def test_tombstone_dropped_at_bottom():
	db = fresh()
	db.put(99, "to be deleted")
	db.flush()
	db.delete(99)
	# force everything down to the bottom level
	for _ in range(20):
		db.put(1, "filler")
		db.flush()

	# key should be gone
	assert db.get(99) is None
	# check that no tombstone record for key 99 survives in the bottom level
	bottom = db.data_levels[-1]
	for sst in bottom:
		for rec in sst.records:
			assert not (rec.key == 99 and rec.type == "DELETE"), "tombstone survived at bottom level"
	print("A4 pass -- tombstone dropped at bottom level")
	cleanup()

if __name__ == "__main__":
	test_compaction_runs()
	test_keys_readable_after_compaction()
	test_deleted_keys_gone_after_compaction()
	test_shadowed_versions_collapsed()
	test_waf_greater_than_one()
	test_tombstone_dropped_at_bottom()
