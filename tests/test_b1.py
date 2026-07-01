import os
import sys
import shutil
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lsm import LSM
from sst import SSTable, Record, PUT, DELETE

DATA_DIR = "/tmp/test_b1_data"

def fresh(deadline=None):
	shutil.rmtree(DATA_DIR, ignore_errors=True)
	return LSM(DATA_DIR, memtable_size=10, num_levels=3, l0_capacity_bytes=1, deadline=deadline)

def cleanup():
	shutil.rmtree(DATA_DIR, ignore_errors=True)

def test_level_ttl_even_split():
	db = fresh(deadline=300)
	# cumulative: level i fires at age > (i+1) * (300/3) = (i+1)*100
	assert db.level_ttl == [100, 200, 300], f"unexpected TTLs: {db.level_ttl}"
	print(f"B1 pass -- level TTLs correct: {db.level_ttl}")
	cleanup()

def test_no_deadline_ttl_is_none():
	db = fresh(deadline=None)
	assert db.level_ttl is None
	print("B1 pass -- no deadline -> level_ttl is None")
	cleanup()

def test_oldest_tombstone_time_on_sst():
	# build an SST with mixed puts and deletes and check oldest_tombstone_time
	records = sorted([
		Record(1, "v1", 5, PUT),
		Record(2, "", 3, DELETE),   # oldest tombstone
		Record(3, "", 8, DELETE),
		Record(4, "v4", 10, PUT),
	], key=lambda r: r.key)
	sst = SSTable(records, "/tmp/test_sst_ts.json")
	assert sst.oldest_tombstone_time == 3, f"expected 3, got {sst.oldest_tombstone_time}"
	print("B1/B2 pass -- oldest_tombstone_time correctly set to min tombstone seqnum")

def test_oldest_tombstone_time_no_tombstones():
	records = sorted([
		Record(1, "v1", 5, PUT),
		Record(2, "v2", 7, PUT),
	], key=lambda r: r.key)
	sst = SSTable(records, "/tmp/test_sst_no_ts.json")
	assert sst.oldest_tombstone_time == float("inf")
	print("B1/B2 pass -- oldest_tombstone_time is inf when no tombstones present")

def test_tombstone_seqnum_matches_delete_seqnum():
	# verify that when we call db.delete(), the tombstone's seqnum
	# matches the engine's seqnum at that point
	db = fresh(deadline=1000)
	db.put(1, "hello")
	seqnum_before_delete = db.seqnum
	db.delete(1)
	expected_seqnum = seqnum_before_delete + 1
	# tombstone is in memtable now
	rec = db.memtable.data[1]
	assert rec.type == DELETE
	assert rec.seqnum == expected_seqnum, f"expected seqnum {expected_seqnum}, got {rec.seqnum}"
	print(f"B1 pass -- tombstone seqnum = {rec.seqnum}, matches logical clock")
	cleanup()

if __name__ == "__main__":
	test_level_ttl_even_split()
	test_no_deadline_ttl_is_none()
	test_oldest_tombstone_time_on_sst()
	test_oldest_tombstone_time_no_tombstones()
	test_tombstone_seqnum_matches_delete_seqnum()
