import os
import sys
import shutil
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from memtable import MemTable
from stats import Stats
from sst import PUT, DELETE

SST_DIR = "/tmp/test_a2_ssts"

def setup():
	shutil.rmtree(SST_DIR, ignore_errors=True)
	os.makedirs(SST_DIR)

def teardown():
	shutil.rmtree(SST_DIR, ignore_errors=True)

def _path(n):
	return os.path.join(SST_DIR, f"sst_{n}.json")

def test_flush_on_full():
	setup()
	stats = Stats()
	mt = MemTable(max_size=1000, stats=stats)

	seqnum = 0
	flushed = []
	counter = 0

	for i in range(2500):
		seqnum += 1
		mt.put(i, f"v{i}", seqnum)
		if mt.is_full():
			flushed.append(mt.flush(_path(counter)))
			counter += 1

	if mt.data:
		flushed.append(mt.flush(_path(counter)))

	# 2500 entries at max_size=1000 -> 3 flushes
	assert len(flushed) == 3, f"expected 3 flushes, got {len(flushed)}"

	for sst in flushed:
		keys = [r.key for r in sst.records]
		assert keys == sorted(keys), f"SST not sorted: {keys[:10]}"

	assert stats.bytes_written > 0
	print(f"A2 pass -- {len(flushed)} flushes, all sorted, bytes_written={stats.bytes_written}")
	teardown()

def test_delete_in_memtable():
	stats = Stats()
	mt = MemTable(max_size=1000, stats=stats)

	mt.put(42, "hello", 1)
	mt.delete(42, 2)

	# tombstone overwrites the put since seqnum 2 > 1
	assert mt.data[42].type == DELETE
	assert mt.data[42].seqnum == 2
	print("A2 pass -- delete overwrites put in memtable ok")

def test_newest_version_kept():
	stats = Stats()
	mt = MemTable(max_size=1000, stats=stats)

	mt.put(7, "first", 1)
	mt.put(7, "second", 2)
	mt.put(7, "third", 3)

	assert mt.data[7].value == "third"
	assert mt.data[7].seqnum == 3
	print("A2 pass -- newest version kept in memtable ok")

if __name__ == "__main__":
	test_flush_on_full()
	test_delete_in_memtable()
	test_newest_version_kept()
