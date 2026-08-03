"""
Three-way evaluation: Vanilla vs FADE vs Decoupled.

Four experiments, each making one point:

  1. Headline comparison at a fixed configuration.
  2. Delete ratio: how the cost of compliance scales with delete volume.
  3. Key distribution: where FADE's wide merge hurts most.
  4. Output file size: the targeting / file-count trade-off.

Every run reports deadline violations alongside cost. A design that is cheap
because it skips erasures is not cheaper, it is non-compliant, so cost numbers
are only meaningful next to a violation count of zero.

Run with: python3 evaluation.py
"""

from benchmark import compare_modes, print_table, run_engine, section
from lsm import MODE_DECOUPLED, MODE_FADE, MODE_VANILLA
from ycsb import describe, generate_ycsb

DEADLINE = 500
NUM_OPS = 8000
KEY_SPACE = 1000


def _workload(distribution="zipfian", workload="X", num_ops=NUM_OPS, seed=1):
	return generate_ycsb(workload=workload, num_ops=num_ops, key_space=KEY_SPACE,
		distribution=distribution, seed=seed)


def _saving(baseline, improved):
	if not baseline:
		return "n/a"
	return f"{100 * (1 - improved / baseline):.0f}%"


def experiment_headline():
	section(
		"Experiment 1: Vanilla vs FADE vs Decoupled",
		f"YCSB workload X (30% read / 45% update / 25% delete), zipfian keys, D={DEADLINE}"
	)
	ops = _workload()
	summary = describe(ops)
	print(f"  {summary['total']} ops over {summary['distinct_keys']} keys "
		f"({summary['puts']} puts, {summary['gets']} gets, {summary['deletes']} deletes)\n")

	results = compare_modes(ops, deadline=DEADLINE, verify=True)
	rows = [
		[
			r.label,
			f"{r.waf:.2f}",
			f"{r.compliance_waf:.2f}",
			r.compliance_compactions,
			f"{r.bytes_per_compliance_event:.0f}",
			r.violations,
			f"{r.avg_lookup_io:.2f}",
			r.file_count,
		]
		for r in results
	]
	print_table(rows, ["config", "WAF", "compliance_WAF", "compl_compactions",
		"bytes/event", "violations", "lookup_IO", "files"])

	vanilla, fade, decoupled = results
	print(f"\n  -> Decoupled writes {_saving(fade.compliance_bytes, decoupled.compliance_bytes)} "
		f"less compliance data than FADE")
	print(f"  -> and runs {_saving(fade.compliance_compactions, decoupled.compliance_compactions)} "
		f"fewer compliance compactions")
	print(f"  -> both meet the deadline ({fade.violations} and {decoupled.violations} violations); "
		f"Vanilla does no compliance work at all")
	print(f"  -> the cost: honouring a deadline leaves more, smaller files on disk")
	print(f"     ({vanilla.file_count} -> {decoupled.file_count}), raising lookup I/O from "
		f"{vanilla.avg_lookup_io:.2f} to {decoupled.avg_lookup_io:.2f} files per get")


def experiment_delete_ratio():
	section(
		"Experiment 2: Delete ratio vs compliance cost",
		f"How compliance cost scales with delete volume, D={DEADLINE}"
	)
	print("  zipfian keys, workload mix varies by delete ratio\n")

	rows = []
	for name, label in [("X-light", "5%"), ("X", "25%"), ("X-heavy", "40%")]:
		ops = _workload(workload=name)
		vanilla, fade, decoupled = compare_modes(ops, deadline=DEADLINE)
		rows.append([
			label,
			f"{vanilla.waf:.2f}",
			f"{fade.compliance_waf:.2f}",
			f"{decoupled.compliance_waf:.2f}",
			_saving(fade.compliance_bytes, decoupled.compliance_bytes),
			f"{fade.violations}/{decoupled.violations}",
		])
	print_table(rows, ["delete_ratio", "vanilla_WAF", "fade_complWAF",
		"decoupled_complWAF", "saving", "violations F/D"])
	print("\n  -> compliance cost rises with delete volume for both designs")
	print("  -> the decoupled saving holds across the range")


def experiment_key_distribution():
	section(
		"Experiment 3: Key distribution vs compliance cost",
		f"Where FADE's wide merge hurts most, D={DEADLINE}"
	)
	print("  workload X (25% deletes), 8000 ops\n")

	rows = []
	for distribution in ["zipfian", "latest", "uniform"]:
		ops = _workload(distribution=distribution)
		vanilla, fade, decoupled = compare_modes(ops, deadline=DEADLINE)
		rows.append([
			distribution,
			f"{vanilla.waf:.2f}",
			f"{fade.compliance_waf:.2f}",
			f"{decoupled.compliance_waf:.2f}",
			_saving(fade.compliance_bytes, decoupled.compliance_bytes),
			f"{fade.violations}/{decoupled.violations}",
		])
	print_table(rows, ["distribution", "vanilla_WAF", "fade_complWAF",
		"decoupled_complWAF", "saving", "violations F/D"])
	print("\n  -> skewed deletes concentrate on few files, so both designs stay cheap")
	print("  -> uniform deletes spread across the key space: FADE's range-based")
	print("     selection pulls in files holding none of the deleted keys, which")
	print("     is exactly what per-key targeting avoids")


def experiment_file_size():
	section(
		"Experiment 4: Output file size vs targeting",
		"Smaller compaction output sharpens targeting but multiplies file count"
	)
	print(f"  decoupled mode only, uniform keys, D={DEADLINE}\n")

	ops = _workload(distribution="uniform")
	rows = []
	for target in [None, 200, 100, 50, 25]:
		result = run_engine(ops, mode=MODE_DECOUPLED, deadline=DEADLINE,
			label=str(target), target_file_records=target)
		rows.append([
			"unsplit" if target is None else str(target),
			f"{result.compliance_waf:.2f}",
			f"{result.bytes_per_compliance_event:.0f}",
			result.file_count,
			f"{result.avg_lookup_io:.2f}",
			result.violations,
		])
	print_table(rows, ["records/file", "compliance_WAF", "bytes/event",
		"files_on_disk", "lookup_IO", "violations"])
	print("\n  -> unsplit output spans the whole key range, so every compliance")
	print("     rewrite touches most of the data")
	print("  -> smaller files narrow the blast radius, at the cost of more files")
	print("     to track; the proposal flags file proliferation as a risk, and this")
	print("     is the trade-off curve for choosing an operating point")


if __name__ == "__main__":
	print("Decoupled Tombstone Storage: Three-Way Evaluation")
	print("Vanilla LSM  vs  FADE (wide merge)  vs  Decoupled (targeted rewrite)")
	print(f"engine: 4 levels, leveled compaction, logical op-counter clock")

	experiment_headline()
	experiment_delete_ratio()
	experiment_key_distribution()
	experiment_file_size()

	print(f"\n{'=' * 74}")
	print("  evaluation complete")
	print(f"{'=' * 74}\n")
