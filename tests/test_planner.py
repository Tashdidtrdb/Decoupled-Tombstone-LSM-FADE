import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lsm import LSM
from planner import plan_purge, scope_if_range_based
from stats import Stats
from workload import generate

TMP = "/tmp/test_planner"


def _engine(skew="uniform", num_ops=8000, key_space=1500, seed=5):
	shutil.rmtree(TMP, ignore_errors=True)
	stats = Stats()
	db = LSM(TMP, memtable_size=150, l0_capacity_bytes=50000, stats=stats,
		deadline=400, target_file_records=50)
	for op, k, v in generate(num_ops=num_ops, key_space=key_space,
			delete_ratio=0.25, skew=skew, seed=seed):
		if op == "put":
			db.put(k, v)
		elif op == "delete":
			db.delete(k)
	db.flush()
	return db, stats


def _tombstone_keys(db):
	return [rec.key for level in db.tombstone_levels for t in level for rec in t.records]


def test_plan_never_misses_a_file_holding_a_key():
	# the safety property: a false positive costs a wasted rewrite, but a missed
	# file would leave deleted data on disk and break the compliance guarantee
	db, _ = _engine()
	keys = _tombstone_keys(db)[:50]
	plan = plan_purge(keys, db.data_levels)
	selected = set(id(f) for f in plan.files)
	missed = [
		(f.filepath, rec.key)
		for level in db.data_levels for f in level
		for rec in f.records
		if rec.key in set(keys) and id(f) not in selected
	]
	assert not missed, f"{len(missed)} files holding a purge key were not selected"
	print(f"planner pass -- no file holding any of {len(keys)} keys was missed")


def test_plan_is_narrower_than_range_selection():
	# the contribution: probing filters per key beats selecting by range overlap
	db, _ = _engine()
	keys = _tombstone_keys(db)
	total_fade = 0
	total_targeted = 0
	for key in keys[:200]:
		total_fade += scope_if_range_based([key], db.data_levels)
		total_targeted += plan_purge([key], db.data_levels).bytes_to_rewrite()
	assert total_targeted < total_fade / 2, \
		f"targeted {total_targeted} not much better than range {total_fade}"
	saving = 100 * (1 - total_targeted / total_fade)
	print(f"planner pass -- per-key targeting rewrites {saving:.1f}% fewer bytes than range selection")


def test_single_key_selects_few_files():
	db, _ = _engine()
	keys = _tombstone_keys(db)
	counts = [len(plan_purge([k], db.data_levels)) for k in keys[:100]]
	avg = sum(counts) / len(counts)
	total_files = sum(len(level) for level in db.data_levels)
	assert avg < total_files / 2, f"one key selects {avg} of {total_files} files"
	print(f"planner pass -- one key selects {avg:.1f} of {total_files} data files on average")


def test_plan_records_keys_per_file():
	db, _ = _engine()
	keys = _tombstone_keys(db)[:30]
	plan = plan_purge(keys, db.data_levels)
	for sst in plan.files:
		assigned = plan.keys_for(sst)
		assert assigned, "file selected with no keys attached"
		assert all(sst.min_key <= k <= sst.max_key for k in assigned)
	print(f"planner pass -- {len(plan)} files each carry their own key set")


def test_absent_keys_select_nothing():
	db, _ = _engine()
	plan = plan_purge([10 ** 7, 10 ** 7 + 1], db.data_levels)
	assert len(plan) == 0, "keys outside the keyspace selected files"
	assert not plan
	print("planner pass -- keys absent from every file select nothing")


def test_empty_input():
	db, _ = _engine()
	plan = plan_purge([], db.data_levels)
	assert len(plan) == 0 and plan.bytes_to_rewrite() == 0
	assert scope_if_range_based([], db.data_levels) == 0
	print("planner pass -- empty key set produces an empty plan")


def test_stats_are_recorded():
	db, stats = _engine()
	before = stats.purge_plans
	plan_purge(_tombstone_keys(db)[:20], db.data_levels, stats)
	assert stats.purge_plans == before + 1
	assert stats.purge_files_probed > 0
	print(f"planner pass -- planning recorded: {stats.purge_files_probed} probes, "
		f"{stats.purge_filter_rejections} filter rejections")


if __name__ == "__main__":
	test_plan_never_misses_a_file_holding_a_key()
	test_plan_is_narrower_than_range_selection()
	test_single_key_selects_few_files()
	test_plan_records_keys_per_file()
	test_absent_keys_select_nothing()
	test_empty_input()
	test_stats_are_recorded()
