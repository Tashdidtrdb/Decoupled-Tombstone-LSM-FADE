"""
Broad correctness sweep across the engine's configuration space.

Every other test targets one mechanism. This one runs whole workloads against a
plain dict and asserts the engine agrees, across the axes that interact badly:
delete ratio, key skew, L0 capacity (how much compaction happens), deadline
tightness, and compliance mode. Most regressions during development showed up
here first, so it is kept as a permanent guard.
"""

import itertools
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lsm import LSM, MODE_VANILLA, MODE_FADE, MODE_DECOUPLED
from stats import Stats
from workload import generate

TMP = "/tmp/test_engine_sweep"
KEY_SPACE = 300
NUM_OPS = 3000


def _check(delete_ratio, skew, l0_capacity, deadline, mode):
	shutil.rmtree(TMP, ignore_errors=True)
	stats = Stats()
	db = LSM(TMP, memtable_size=20, num_levels=4, l0_capacity_bytes=l0_capacity,
		stats=stats, deadline=deadline, mode=mode)
	reference = {}
	for op, key, value in generate(num_ops=NUM_OPS, key_space=KEY_SPACE,
			delete_ratio=delete_ratio, skew=skew, seed=7):
		if op == "put":
			db.put(key, value)
			reference[key] = value
		elif op == "delete":
			db.delete(key)
			reference[key] = None
		else:
			db.get(key)
	db.flush()
	return [k for k in range(KEY_SPACE) if db.get(k) != reference.get(k)], db


def test_engine_matches_reference_across_configurations():
	configs = list(itertools.product(
		[0.05, 0.25, 0.4],          # delete ratio
		["uniform", "zipf"],        # key skew
		[500, 5000, 10 ** 8],       # L0 capacity: heavy, moderate, no compaction
		[None, 100, 1000],          # deadline
	))
	failures = []
	for delete_ratio, skew, l0_capacity, deadline in configs:
		mode = MODE_VANILLA if deadline is None else MODE_DECOUPLED
		wrong, _ = _check(delete_ratio, skew, l0_capacity, deadline, mode)
		if wrong:
			failures.append((delete_ratio, skew, l0_capacity, deadline, len(wrong)))
	assert not failures, f"{len(failures)} configurations disagreed: {failures[:3]}"
	print(f"sweep pass -- {len(configs)} configurations match the reference exactly")


def test_all_modes_agree_on_visible_state():
	# the three strategies differ in what they erase and when, but a reader must
	# never be able to tell them apart
	results = {}
	for mode, deadline in [(MODE_VANILLA, None), (MODE_FADE, 500), (MODE_DECOUPLED, 500)]:
		wrong, db = _check(0.25, "zipf", 5000, deadline, mode)
		assert not wrong, f"{mode}: {len(wrong)} keys wrong"
		results[mode] = [db.get(k) for k in range(KEY_SPACE)]
	assert results[MODE_FADE] == results[MODE_DECOUPLED] == results[MODE_VANILLA], \
		"modes disagree on visible state"
	print("sweep pass -- all three modes expose identical visible state")


def test_l1_files_stay_disjoint_across_configurations():
	# get() assumes one file per level below L0 can hold a key
	failures = []
	for skew, l0_capacity, deadline in itertools.product(
			["uniform", "zipf"], [500, 5000], [None, 100]):
		mode = MODE_VANILLA if deadline is None else MODE_DECOUPLED
		_, db = _check(0.25, skew, l0_capacity, deadline, mode)
		for level_idx, level in enumerate(db.data_levels):
			if level_idx == 0:
				continue
			ordered = sorted(level, key=lambda f: f.min_key)
			for a, b in zip(ordered, ordered[1:]):
				if a.max_key >= b.min_key:
					failures.append((skew, l0_capacity, deadline, level_idx))
	assert not failures, f"overlapping files at L1+: {failures[:3]}"
	print("sweep pass -- L1+ files stay key-disjoint in every configuration")


if __name__ == "__main__":
	test_engine_matches_reference_across_configurations()
	test_all_modes_agree_on_visible_state()
	test_l1_files_stay_disjoint_across_configurations()
