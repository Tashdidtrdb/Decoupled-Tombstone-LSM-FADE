import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sst import Record, SSTable, PUT, DELETE

def test_write_read_sorted():
	# create records out of order intentionally
	records = [
		Record(5, "val5", 3, PUT),
		Record(1, "val1", 1, PUT),
		Record(3, "", 4, DELETE),
		Record(2, "val2", 2, PUT),
	]
	records.sort(key=lambda r: r.key)

	path = "/tmp/test_a1.json"
	sst = SSTable(records, path)
	sst.write()

	loaded = SSTable.load(path)

	keys = [r.key for r in loaded.records]
	assert keys == sorted(keys), f"records not sorted: {keys}"
	assert loaded.min_key == 1
	assert loaded.max_key == 5
	assert loaded.size_bytes > 0

	# check DELETE record came back correctly
	delete_rec = next(r for r in loaded.records if r.key == 3)
	assert delete_rec.type == DELETE
	assert delete_rec.value == ""

	os.remove(path)
	print("A1 pass -- write/read/sorted/min_max/type all ok")

def test_overlaps():
	r = lambda key: Record(key, "v", 1, PUT)

	# [1,5] and [3,8] -> overlap
	a = SSTable([r(1), r(5)], "a")
	b = SSTable([r(3), r(8)], "b")
	assert a.overlaps(b)

	# [1,3] and [5,8] -> no overlap
	c = SSTable([r(1), r(3)], "c")
	d = SSTable([r(5), r(8)], "d")
	assert not c.overlaps(d)

	# touching boundary [1,5] and [5,9] -> overlap
	e = SSTable([r(1), r(5)], "e")
	f = SSTable([r(5), r(9)], "f")
	assert e.overlaps(f)

	print("A1 pass -- overlaps() ok")

if __name__ == "__main__":
	test_write_read_sorted()
	test_overlaps()
