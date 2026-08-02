import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lsm import LSM
from stats import Stats
from workload import generate

TMP = "/tmp/test_compaction_split"


def _run(skew="uniform", dr=0.25, cap=5000, seed=1, num_ops=6000,
		key_space=1000, target_file_records=50):
	shutil.rmtree(TMP, ignore_errors=True)
	stats = Stats()
	db = LSM(TMP, memtable_size=100, l0_capacity_bytes=cap, stats=stats,
		deadline=300, target_file_records=target_file_records)
	reference = {}
	for op, k, v in generate(num_ops=num_ops, key_space=key_space,
			delete_ratio=dr, skew=skew, seed=seed):
		if op == "put":
			db.put(k, v)
			reference[k] = v
		elif op == "delete":
			db.delete(k)
			reference[k] = None
	db.flush()
	return db, stats, reference


def _overlaps(db):
	found = []
	for level_idx, level in enumerate(db.data_levels):
		if level_idx == 0:
			continue  # L0 files legitimately overlap
		ordered = sorted(level, key=lambda f: f.min_key)
		for a, b in zip(ordered, ordered[1:]):
			if a.max_key >= b.min_key:
				found.append((level_idx, (a.min_key, a.max_key), (b.min_key, b.max_key)))
	return found


def test_l1_files_never_overlap():
	# get() assumes at most one file per level below L0 can hold a given key.
	# Splitting compaction output must not break that.
	failures = []
	for skew in ["zipf", "uniform"]:
		for cap in [2000, 5000, 50000]:
			for dr in [0.1, 0.25, 0.4]:
				db, _, _ = _run(skew=skew, dr=dr, cap=cap)
				bad = _overlaps(db)
				if bad:
					failures.append((skew, cap, dr, bad[0]))
	assert not failures, f"overlapping L1+ files: {failures[:3]}"
	print("split pass -- no overlapping L1+ files across 18 configurations")


def test_output_respects_target_size():
	db, _, _ = _run(target_file_records=25)
	oversized = [
		(i, len(f.records))
		for i, level in enumerate(db.data_levels) if i > 0
		for f in level if len(f.records) > 25
	]
	assert not oversized, f"files exceed target size: {oversized[:3]}"
	print("split pass -- every L1+ file honours the target record count")


def test_splitting_preserves_correctness():
	db, _, reference = _run(target_file_records=25)
	wrong = [k for k in range(1000) if db.get(k) != reference.get(k)]
	assert not wrong, f"{len(wrong)} keys wrong: {wrong[:5]}"
	print("split pass -- 1000 keys match reference with splitting enabled")


def test_smaller_targets_narrow_the_blast_radius():
	# the point of the change: an expiring tombstone key should touch less data
	def scope(target):
		db, _, _ = _run(skew="uniform", target_file_records=target)
		files = [f for level in db.data_levels for f in level]
		total = sum(f.size_bytes for f in files)
		costs = [
			sum(f.size_bytes for f in files if f.may_contain(rec.key))
			for level in db.tombstone_levels for t in level for rec in t.records
		]
		return (sum(costs) / len(costs)) / total if costs and total else 1.0

	unsplit = scope(None)
	split = scope(25)
	assert split < unsplit / 2, f"splitting did not narrow scope: {unsplit} -> {split}"
	print(f"split pass -- rewrite scope {100*unsplit:.1f}% -> {100*split:.1f}% of data")


def test_splitting_can_be_disabled():
	db, _, reference = _run(target_file_records=None)
	wrong = [k for k in range(1000) if db.get(k) != reference.get(k)]
	assert not wrong, "disabling the split broke correctness"
	assert not _overlaps(db), "unsplit output produced overlapping files"
	print("split pass -- target_file_records=None restores single-file output")


def test_dst_files_inside_merged_range_are_absorbed():
	# regression: dst overlaps were once computed from src_file alone, but L0
	# siblings widen the merged range. A dst file inside the widened span but
	# outside src_file survived while the output covered its keys.
	for seed in [1, 4, 7]:
		db, _, reference = _run(skew="uniform", cap=5000, seed=seed)
		assert not _overlaps(db), f"seed {seed} produced overlapping files"
		wrong = [k for k in range(1000) if db.get(k) != reference.get(k)]
		assert not wrong, f"seed {seed}: {len(wrong)} keys wrong"
	print("split pass -- dst files within the merged range are absorbed")


if __name__ == "__main__":
	test_l1_files_never_overlap()
	test_output_respects_target_size()
	test_splitting_preserves_correctness()
	test_smaller_targets_narrow_the_blast_radius()
	test_splitting_can_be_disabled()
	test_dst_files_inside_merged_range_are_absorbed()
