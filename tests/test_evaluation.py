"""
Guards on the claims the evaluation makes.

These assert the *shape* of the results rather than exact numbers, so they fail
if a change quietly inverts a conclusion in the paper.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from benchmark import compare_modes, run_engine
from lsm import MODE_DECOUPLED
from ycsb import generate_ycsb

NUM_OPS = 4000
KEY_SPACE = 500
DEADLINE = 500


def _ops(distribution="zipfian", workload="X", seed=1):
	return generate_ycsb(workload=workload, num_ops=NUM_OPS, key_space=KEY_SPACE,
		distribution=distribution, seed=seed)


def test_headline_claim_holds():
	# the paper's central claim: same deadline, materially less compliance work
	vanilla, fade, decoupled = compare_modes(_ops(), deadline=DEADLINE, verify=True)
	assert decoupled.compliance_bytes < fade.compliance_bytes * 0.8, \
		"decoupled does not beat FADE by a meaningful margin"
	assert fade.violations == 0 and decoupled.violations == 0, \
		"a deadline mode missed its deadline"
	assert vanilla.compliance_bytes == 0
	saving = 100 * (1 - decoupled.compliance_bytes / fade.compliance_bytes)
	print(f"eval pass -- decoupled writes {saving:.0f}% less compliance data, both compliant")


def test_saving_holds_across_delete_ratios():
	for workload in ["X-light", "X", "X-heavy"]:
		_, fade, decoupled = compare_modes(_ops(workload=workload), deadline=DEADLINE)
		assert decoupled.compliance_bytes < fade.compliance_bytes, \
			f"{workload}: decoupled did not beat FADE"
		assert decoupled.violations == 0, f"{workload}: missed deadlines"
	print("eval pass -- the saving holds at 5%, 25% and 40% delete ratios")


def test_saving_holds_across_distributions():
	for distribution in ["zipfian", "latest", "uniform"]:
		_, fade, decoupled = compare_modes(_ops(distribution=distribution), deadline=DEADLINE)
		assert decoupled.compliance_bytes < fade.compliance_bytes, \
			f"{distribution}: decoupled did not beat FADE"
		assert decoupled.violations == 0, f"{distribution}: missed deadlines"
	print("eval pass -- the saving holds under zipfian, latest and uniform keys")


def test_uniform_is_the_worst_case_for_range_selection():
	# the argument for per-key targeting: range-based selection degrades most
	# when deletes are spread across the key space
	_, fade_zipf, _ = compare_modes(_ops(distribution="zipfian"), deadline=DEADLINE)
	_, fade_uniform, _ = compare_modes(_ops(distribution="uniform"), deadline=DEADLINE)
	assert fade_uniform.compliance_waf > fade_zipf.compliance_waf, \
		"uniform keys were not worse for FADE, which contradicts the motivation"
	print(f"eval pass -- FADE compliance WAF {fade_zipf.compliance_waf:.1f} (zipf) "
		f"-> {fade_uniform.compliance_waf:.1f} (uniform)")


def test_smaller_files_narrow_the_blast_radius():
	# experiment 4's curve must be monotonic in the direction claimed
	ops = _ops(distribution="uniform")
	# ordered coarse -> fine
	sizes = [None, 100, 25]
	events = []
	files = []
	for target in sizes:
		result = run_engine(ops, mode=MODE_DECOUPLED, deadline=DEADLINE,
			target_file_records=target)
		events.append(result.bytes_per_compliance_event)
		files.append(result.file_count)
		assert result.violations == 0, f"target={target} missed deadlines"
	assert events[0] > events[-1], \
		f"smaller files did not narrow the rewrite: {events}"
	assert files[0] < files[-1], \
		f"smaller files did not increase file count: {files}"
	print(f"eval pass -- bytes/event {events[0]:.0f} -> {events[-1]:.0f} "
		f"as files grow {files[0]} -> {files[-1]}")


def test_compliance_costs_more_lookup_io():
	# an honest cost of the design, reported rather than hidden
	vanilla, _, decoupled = compare_modes(_ops(), deadline=DEADLINE)
	assert decoupled.file_count > vanilla.file_count, \
		"deadline mode did not produce more files, so the trade-off claim is wrong"
	print(f"eval pass -- honouring the deadline grows the file count "
		f"{vanilla.file_count} -> {decoupled.file_count}")


if __name__ == "__main__":
	test_headline_claim_holds()
	test_saving_holds_across_delete_ratios()
	test_saving_holds_across_distributions()
	test_uniform_is_the_worst_case_for_range_selection()
	test_smaller_files_narrow_the_blast_radius()
	test_compliance_costs_more_lookup_io()
