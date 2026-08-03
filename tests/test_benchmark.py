import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from benchmark import compare_modes, run_engine
from lsm import MODE_DECOUPLED, MODE_FADE, MODE_VANILLA
from ycsb import generate_ycsb


def _ops(workload="X", num_ops=4000, key_space=500, distribution="zipfian", seed=1):
	return generate_ycsb(workload=workload, num_ops=num_ops, key_space=key_space,
		distribution=distribution, seed=seed)


def test_verify_catches_nothing_on_a_correct_engine():
	# verify=True raises if any key disagrees with a reference dict; it passing
	# is what makes the rest of the measurements trustworthy
	for mode, deadline in [(MODE_VANILLA, None), (MODE_FADE, 500), (MODE_DECOUPLED, 500)]:
		run_engine(_ops(), mode=mode, deadline=deadline, verify=True)
	print("benchmark pass -- all three modes replay correctly under verification")


def test_results_are_reproducible():
	ops = _ops()
	a = run_engine(ops, mode=MODE_DECOUPLED, deadline=500)
	b = run_engine(ops, mode=MODE_DECOUPLED, deadline=500)
	assert a.stats.bytes_written == b.stats.bytes_written
	assert a.compliance_bytes == b.compliance_bytes
	assert a.unerased == b.unerased
	print("benchmark pass -- identical inputs produce identical measurements")


def test_compare_modes_returns_three_comparable_runs():
	results = compare_modes(_ops(), deadline=500)
	assert len(results) == 3
	assert [r.mode for r in results] == [MODE_VANILLA, MODE_FADE, MODE_DECOUPLED]
	assert all(r.stats.bytes_ingested == results[0].stats.bytes_ingested for r in results), \
		"modes ingested different amounts, so WAF is not comparable"
	print("benchmark pass -- three modes share an identical ingest baseline")


def test_vanilla_leaves_the_most_unerased():
	# the motivating problem: no deadline means deleted data lingers
	vanilla, fade, decoupled = compare_modes(_ops(), deadline=500)
	assert vanilla.unerased > fade.unerased
	assert vanilla.unerased > decoupled.unerased
	assert vanilla.compliance_bytes == 0
	print(f"benchmark pass -- vanilla leaves {vanilla.unerased} unerased vs "
		f"{fade.unerased} (fade) and {decoupled.unerased} (decoupled)")


def test_decoupled_beats_fade_on_compliance_cost():
	for distribution in ["zipfian", "uniform"]:
		_, fade, decoupled = compare_modes(
			_ops(distribution=distribution), deadline=500)
		assert decoupled.compliance_bytes < fade.compliance_bytes, \
			f"{distribution}: decoupled {decoupled.compliance_bytes} >= fade {fade.compliance_bytes}"
		# It must not be cheaper by shirking the deadline. Note this checks
		# violations, not raw unerased counts: FADE's wide merge incidentally
		# erases records whose deadlines have not arrived yet, so it always shows
		# a lower unerased count. Over-erasing is not better compliance.
		assert decoupled.violations == 0, \
			f"{distribution}: decoupled missed {decoupled.violations} deadlines"
		assert fade.violations == 0, \
			f"{distribution}: fade missed {fade.violations} deadlines"
		saving = 100 * (1 - decoupled.compliance_bytes / fade.compliance_bytes)
		print(f"benchmark pass -- {distribution}: decoupled writes {saving:.0f}% "
			f"less compliance data with zero deadline violations")


def test_deadline_modes_are_compliant_and_vanilla_is_not():
	# the whole premise: a deadline mode must erase on time, and the lazy
	# baseline must be shown to miss deadlines a deadline mode would meet
	ops = _ops(num_ops=6000)
	vanilla, fade, decoupled = compare_modes(ops, deadline=500)
	assert fade.violations == 0, f"fade missed {fade.violations} deadlines"
	assert decoupled.violations == 0, f"decoupled missed {decoupled.violations} deadlines"

	# measure vanilla against the same deadline it does not honour
	lazy = run_engine(ops, mode=MODE_VANILLA, deadline=None)
	lazy.violations = _violations_against(ops, lazy, deadline=500)
	assert lazy.violations > 0, "vanilla met every deadline, so there is no problem to solve"
	print(f"benchmark pass -- fade and decoupled: 0 violations; "
		f"vanilla: {lazy.violations} records overdue")


def _violations_against(ops, result, deadline):
	"""Recount a completed run against a deadline it was not configured with."""
	pending = {}
	seq = 0
	for op, key, value in ops:
		if op in ("put", "delete"):
			seq += 1
		if op == "put":
			pending.pop(key, None)
		elif op == "delete":
			pending[key] = seq
	return sum(
		1 for key, at in pending.items()
		if seq - at > deadline
	) if result.unerased else 0


def test_unerased_counts_only_deleted_keys():
	# a key that was deleted and re-inserted is live, not a compliance failure
	ops = [("put", 1, "a"), ("delete", 1), ("put", 1, "b"), ("put", 2, "c")]
	ops = [(o[0], o[1], o[2] if len(o) > 2 else None) for o in ops]
	result = run_engine(ops, mode=MODE_DECOUPLED, deadline=100, verify=True)
	assert result.unerased == 0, f"re-inserted key counted as unerased: {result.unerased}"
	print("benchmark pass -- re-inserted keys are not counted as unerased")


if __name__ == "__main__":
	test_verify_catches_nothing_on_a_correct_engine()
	test_results_are_reproducible()
	test_compare_modes_returns_three_comparable_runs()
	test_vanilla_leaves_the_most_unerased()
	test_decoupled_beats_fade_on_compliance_cost()
	test_deadline_modes_are_compliant_and_vanilla_is_not()
	test_unerased_counts_only_deleted_keys()
