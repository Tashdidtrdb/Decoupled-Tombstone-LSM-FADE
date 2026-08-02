import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sst import Record, SSTable, PUT, DELETE

TMP = "/tmp/test_sst_bloom"
os.makedirs(TMP, exist_ok=True)


def _sst(keys, name="a.json"):
	records = [Record(k, "v" * 10, k, PUT) for k in sorted(keys)]
	sst = SSTable(records, os.path.join(TMP, name))
	sst.write()
	return sst


def test_no_false_negatives_on_sst():
	# the property get() will rely on: a key stored in this file must never be
	# reported absent, or the lookup would skip the file holding it
	keys = list(range(0, 2000, 2))
	sst = _sst(keys)
	missing = [k for k in keys if not sst.may_contain(k)]
	assert not missing, f"{len(missing)} present keys reported absent"
	print(f"sst-bloom pass -- all {len(keys)} present keys pass may_contain")


def test_rejects_absent_keys_within_range():
	# the useful case: keys inside [min_key, max_key] that the file does not hold.
	# range checks alone cannot rule these out, the filter is what does
	keys = list(range(0, 2000, 2))
	sst = _sst(keys)
	absent = [k for k in range(1, 2000, 2)]
	rejected = sum(1 for k in absent if not sst.may_contain(k))
	assert rejected > len(absent) * 0.9, f"only rejected {rejected}/{len(absent)}"
	print(f"sst-bloom pass -- rejected {rejected}/{len(absent)} absent in-range keys")


def test_out_of_range_short_circuits():
	sst = _sst([10, 20, 30])
	assert not sst.may_contain(5)
	assert not sst.may_contain(100)
	print("sst-bloom pass -- out-of-range keys rejected by min/max")


def test_load_rebuilds_filter():
	keys = [1, 5, 9, 44, 100]
	sst = _sst(keys, "load.json")
	reloaded = SSTable.load(sst.filepath)
	assert all(reloaded.may_contain(k) for k in keys)
	assert reloaded.bloom.num_added == len(keys)
	print("sst-bloom pass -- load() rebuilds a working filter")


def test_tombstone_records_are_indexed():
	# tombstone SSTs need the filter too: task 14 uses it to find which data file
	# holds the key a tombstone invalidates
	records = [Record(k, "", k, DELETE) for k in [3, 7, 11]]
	sst = SSTable(records, os.path.join(TMP, "tomb.json"))
	sst.write()
	assert all(sst.may_contain(k) for k in [3, 7, 11])
	print("sst-bloom pass -- DELETE records are indexed in the filter")


def test_bits_per_element_is_configurable():
	keys = list(range(1000))
	small = SSTable([Record(k, "v", k, PUT) for k in keys], os.path.join(TMP, "s.json"), bits_per_element=4)
	large = SSTable([Record(k, "v", k, PUT) for k in keys], os.path.join(TMP, "l.json"), bits_per_element=16)
	assert large.bloom.memory_bytes() > small.bloom.memory_bytes()
	assert large.bloom.estimated_fpr() < small.bloom.estimated_fpr()
	print("sst-bloom pass -- per-file bits/element budget is honoured")


if __name__ == "__main__":
	test_no_false_negatives_on_sst()
	test_rejects_absent_keys_within_range()
	test_out_of_range_short_circuits()
	test_load_rebuilds_filter()
	test_tombstone_records_are_indexed()
	test_bits_per_element_is_configurable()
