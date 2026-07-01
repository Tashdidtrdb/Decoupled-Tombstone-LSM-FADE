import shutil
from lsm import LSM
from stats import Stats
from workload import generate

DATA_DIR = "/tmp/lsm_experiment"

def run_engine(ops, deadline=None, l0_capacity_bytes=50000):
	shutil.rmtree(DATA_DIR, ignore_errors=True)
	stats = Stats()
	db = LSM(
		DATA_DIR,
		memtable_size=200,
		num_levels=4,
		l0_capacity_bytes=l0_capacity_bytes,
		stats=stats,
		deadline=deadline
	)
	for op, key, value in ops:
		if op == "put":
			db.put(key, value)
		elif op == "delete":
			db.delete(key)
		elif op == "get":
			db.get(key)
	db.flush()
	shutil.rmtree(DATA_DIR, ignore_errors=True)
	return stats

def print_table(rows, headers):
	col_widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
	def fmt_row(row):
		return "  ".join(str(row[i]).ljust(col_widths[i]) for i in range(len(row)))
	sep = "  ".join("-" * w for w in col_widths)
	print(fmt_row(headers))
	print(sep)
	for row in rows:
		print(fmt_row(row))

def section(title, description):
	print(f"\n{'=' * 60}")
	print(f"  {title}")
	print(f"  {description}")
	print(f"{'=' * 60}")

# experiment 1: show that tighter deadlines force more eager compaction
# capacity is set very high so only FADE triggers compaction, not capacity pressure
def experiment_vary_deadline(delete_ratio=0.25, num_ops=8000, key_space=500):
	section(
		"Experiment 1: Deadline tightness vs WAF",
		"Same workload, Vanilla vs FADE at different deadlines (D = ops before forced purge)."
	)
	print(f"  workload: {num_ops} ops, {int(delete_ratio*100)}% deletes, zipf skew, {key_space} keys\n")

	ops = generate(num_ops=num_ops, key_space=key_space, delete_ratio=delete_ratio, skew="zipf")
	configs = [
		("Vanilla (no deadline)", None),
		("FADE  D=2000", 2000),
		("FADE  D=500", 500),
		("FADE  D=100", 100),
	]

	rows = []
	for label, deadline in configs:
		s = run_engine(ops, deadline=deadline, l0_capacity_bytes=10**8)
		rows.append([label, f"{s.waf():.2f}", s.bytes_written,
			s.compaction_count, s.files_merged_total, f"{s.avg_lookup_io():.2f}"])

	print_table(rows, ["config", "WAF", "bytes_written", "compactions", "files_merged", "avg_lookup_IO"])
	print("\n  -> tighter deadline = more eager compactions = higher WAF")
	print("  -> this is the cost FADE pays to guarantee physical deletion within D ops")

# experiment 2: show that higher delete ratios amplify FADE's write overhead
def experiment_vary_delete_ratio(deadline=200, num_ops=8000, key_space=500):
	section(
		"Experiment 2: Delete ratio vs WAF",
		f"Vanilla vs FADE (D={deadline}) across workloads with increasing delete ratios."
	)
	print(f"  workload: {num_ops} ops, zipf skew, {key_space} keys\n")

	rows = []
	for dr in [0.05, 0.15, 0.25, 0.40]:
		ops = generate(num_ops=num_ops, key_space=key_space, delete_ratio=dr, skew="zipf")
		v = run_engine(ops, deadline=None)
		f = run_engine(ops, deadline=deadline)
		overhead = f"{((f.waf() / v.waf() - 1) * 100):.0f}%"
		rows.append([f"{int(dr*100)}%", f"{v.waf():.2f}", f"{f.waf():.2f}",
			v.compaction_count, f.compaction_count, overhead])

	print_table(rows, ["delete_ratio", "vanilla_WAF", "fade_WAF",
		"vanilla_compact", "fade_compact", "FADE_overhead"])
	print("\n  -> more deletes = more tombstones = more eager compactions triggered by FADE")
	print("  -> overhead column shows how much extra write work FADE adds over vanilla")

# experiment 3: show that uniform key access causes far more compaction than skewed access
def experiment_vary_skew(deadline=200, num_ops=8000, key_space=500, delete_ratio=0.25):
	section(
		"Experiment 3: Key skew vs WAF",
		f"Vanilla vs FADE (D={deadline}), uniform vs Zipf key distribution."
	)
	print(f"  workload: {num_ops} ops, {int(delete_ratio*100)}% deletes, {key_space} keys\n")

	rows = []
	for label, skew in [("uniform", "uniform"), ("zipf (skewed)", "zipf")]:
		ops = generate(num_ops=num_ops, key_space=key_space, delete_ratio=delete_ratio, skew=skew)
		v = run_engine(ops, deadline=None)
		f = run_engine(ops, deadline=deadline)
		rows.append([label, f"{v.waf():.2f}", f"{f.waf():.2f}",
			v.compaction_count, f.compaction_count])

	print_table(rows, ["key_skew", "vanilla_WAF", "fade_WAF", "vanilla_compact", "fade_compact"])
	print("\n  -> uniform deletes spread across all keys, each compaction must merge more files")
	print("  -> zipf concentrates deletes on hot keys, compactions stay more localized")

if __name__ == "__main__":
	print("LSM-tree Baseline Evaluation: Vanilla vs FADE")
	print("baseline engine: Python LSM, 4 levels, leveled compaction")
	experiment_vary_deadline()
	experiment_vary_delete_ratio()
	experiment_vary_skew()
	print(f"\n{'=' * 60}")
	print("  done")
	print(f"{'=' * 60}\n")
