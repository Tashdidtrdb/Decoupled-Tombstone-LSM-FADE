import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sst import Record, SSTable, DELETE
from tombstone_index import TombstoneIndex, bits_for_level, DEFAULT_LEVEL_BITS

TMP = "/tmp/test_tombstone_index"
os.makedirs(TMP, exist_ok=True)


def _tomb_sst(keys, name):
	records = [Record(k, "", k, DELETE) for k in sorted(keys)]
	sst = SSTable(records, os.path.join(TMP, name))
	sst.write()
	return sst


def test_miss_is_definitive():
	# the property the lookup path depends on: if no level claims the key, there
	# is definitely no tombstone for it anywhere
	idx = TombstoneIndex(4)
	idx.rebuild_level(0, [_tomb_sst(range(0, 1000, 2), "even.json")])
	present = [k for k in range(0, 1000, 2) if not idx.may_contain(k, 0)]
	assert not present, f"{len(present)} tombstoned keys reported absent"
	print(f"tsindex pass -- no false negatives across 500 tombstone keys")


def test_absent_keys_mostly_rejected():
	idx = TombstoneIndex(4)
	idx.rebuild_level(0, [_tomb_sst(range(0, 1000, 2), "even2.json")])
	absent = [k for k in range(1, 1000, 2)]
	rejected = sum(1 for k in absent if not idx.any_level_may_contain(k))
	assert rejected > len(absent) * 0.95, f"only {rejected}/{len(absent)} rejected"
	print(f"tsindex pass -- rejected {rejected}/{len(absent)} absent keys with zero I/O")


def test_shallow_levels_get_more_bits():
	# Monkey premise: delete density is highest where tombstones are newest, so
	# shallow levels deserve a richer budget
	assert DEFAULT_LEVEL_BITS[0] > DEFAULT_LEVEL_BITS[-1]
	assert bits_for_level(0) > bits_for_level(3)
	# levels beyond the table reuse the leanest budget rather than crashing
	assert bits_for_level(99) == DEFAULT_LEVEL_BITS[-1]
	print(f"tsindex pass -- non-uniform allocation {DEFAULT_LEVEL_BITS} honoured")


def test_shallow_filter_has_lower_fpr():
	idx = TombstoneIndex(4)
	keys = list(range(2000))
	idx.rebuild_level(0, [_tomb_sst(keys, "l0.json")])
	idx.rebuild_level(3, [_tomb_sst(keys, "l3.json")])
	assert idx.filters[0].estimated_fpr() < idx.filters[3].estimated_fpr()
	print(f"tsindex pass -- L0 FPR {idx.filters[0].estimated_fpr():.4f} "
		f"< L3 FPR {idx.filters[3].estimated_fpr():.4f}")


def test_rebuild_drops_removed_keys():
	# Bloom filters cannot delete entries, so a level whose files changed must be
	# rebuilt from scratch or it would keep reporting keys that have moved away
	idx = TombstoneIndex(4)
	idx.rebuild_level(0, [_tomb_sst([1, 2, 3], "before.json")])
	assert idx.may_contain(2, 0)
	idx.rebuild_level(0, [_tomb_sst([9], "after.json")])
	assert idx.may_contain(9, 0)
	assert not idx.may_contain(2, 0), "stale key survived a rebuild"
	print("tsindex pass -- rebuild drops keys that left the level")


def test_empty_level_reports_nothing():
	idx = TombstoneIndex(4)
	idx.rebuild_level(1, [])
	assert not idx.may_contain(5, 1)
	assert not idx.any_level_may_contain(5)
	print("tsindex pass -- empty level never claims a key")


def test_locates_correct_levels():
	idx = TombstoneIndex(4)
	idx.rebuild_level(0, [_tomb_sst([10, 11], "a.json")])
	idx.rebuild_level(2, [_tomb_sst([50, 51], "b.json")])
	assert idx.levels_that_may_contain(10) == [0]
	assert idx.levels_that_may_contain(50) == [2]
	print("tsindex pass -- reports which levels may hold a tombstone")


if __name__ == "__main__":
	test_miss_is_definitive()
	test_absent_keys_mostly_rejected()
	test_shallow_levels_get_more_bits()
	test_shallow_filter_has_lower_fpr()
	test_rebuild_drops_removed_keys()
	test_empty_level_reports_nothing()
	test_locates_correct_levels()
