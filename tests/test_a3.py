import os
import sys
import shutil
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lsm import LSM

DATA_DIR = "/tmp/test_a3_data"

def fresh():
	shutil.rmtree(DATA_DIR, ignore_errors=True)
	return LSM(DATA_DIR, memtable_size=10)

def cleanup():
	shutil.rmtree(DATA_DIR, ignore_errors=True)

def test_basic_put_get():
	db = fresh()
	db.put(1, "hello")
	assert db.get(1) == "hello"
	print("A3 pass -- basic put/get ok")
	cleanup()

def test_delete_returns_none():
	db = fresh()
	db.put(5, "data")
	db.delete(5)
	assert db.get(5) is None
	print("A3 pass -- delete returns None ok")
	cleanup()

def test_put_after_delete():
	db = fresh()
	db.put(3, "first")
	db.delete(3)
	db.put(3, "second")
	assert db.get(3) == "second"
	print("A3 pass -- put after delete returns new value ok")
	cleanup()

def test_get_missing_key():
	db = fresh()
	assert db.get(999) is None
	print("A3 pass -- missing key returns None ok")
	cleanup()

def test_get_across_flushed_l0():
	# memtable_size=10 so keys will spill across multiple L0 files
	db = fresh()
	for i in range(50):
		db.put(i, f"val{i}")

	# all 50 keys must still be findable
	for i in range(50):
		assert db.get(i) == f"val{i}", f"missing key {i}"

	assert len(db.levels[0]) > 1, "expected multiple L0 files"
	print(f"A3 pass -- get across {len(db.levels[0])} L0 files ok")
	cleanup()

def test_update_overwrites():
	db = fresh()
	db.put(7, "old")
	db.put(7, "new")
	assert db.get(7) == "new"
	print("A3 pass -- update overwrites ok")
	cleanup()

def test_tombstone_across_flush():
	# put and delete in different flushes, delete must win
	db = fresh()
	db.put(42, "alive")
	db.flush()  # force put to L0

	db.delete(42)  # tombstone in fresh memtable
	assert db.get(42) is None
	print("A3 pass -- tombstone in memtable shadows flushed put ok")
	cleanup()

if __name__ == "__main__":
	test_basic_put_get()
	test_delete_returns_none()
	test_put_after_delete()
	test_get_missing_key()
	test_get_across_flushed_l0()
	test_update_overwrites()
	test_tombstone_across_flush()
