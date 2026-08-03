"""
Benchmark harness: replays one YCSB workload against several engine
configurations and collects comparable measurements.

Kept separate from the experiment definitions so the evaluation (task 16) only
has to describe *what* to compare, not how to run it.
"""

import shutil
import time

from lsm import LSM, MODE_DECOUPLED, MODE_FADE, MODE_VANILLA
from stats import Stats

DATA_DIR = "/tmp/lsm_benchmark"


class Result:
	"""Measurements from one engine run, plus the config that produced them."""

	def __init__(self, label, mode, stats, elapsed, unerased, live_bytes,
			file_count, violations=0):
		self.label = label
		self.mode = mode
		self.stats = stats
		self.elapsed = elapsed
		# deleted records still physically present when the run ended. Some of
		# these are legitimately pending -- their deadline has not arrived yet --
		# so this is a progress indicator, not a compliance verdict.
		self.unerased = unerased
		# deleted records whose deadline HAS passed and are still on disk. This
		# is the compliance verdict, and it must be zero for any deadline mode.
		self.violations = violations
		self.live_bytes = live_bytes
		self.file_count = file_count

	@property
	def waf(self):
		return self.stats.waf()

	@property
	def compliance_waf(self):
		return self.stats.compliance_waf()

	@property
	def compliance_bytes(self):
		return self.stats.compliance_bytes_written

	@property
	def compliance_compactions(self):
		return self.stats.compliance_compaction_count

	@property
	def avg_lookup_io(self):
		return self.stats.avg_lookup_io()

	@property
	def bytes_per_compliance_event(self):
		if not self.compliance_compactions:
			return 0
		return self.compliance_bytes / self.compliance_compactions


def run_engine(ops, mode=MODE_VANILLA, deadline=None, label=None, verify=False, **engine_kw):
	"""
	Replay ops against one engine configuration.

	verify=True checks every key against a reference dict at the end. Off by
	default because it doubles runtime on large workloads, but worth enabling
	whenever a new configuration is introduced.
	"""
	shutil.rmtree(DATA_DIR, ignore_errors=True)
	stats = Stats()
	db = LSM(
		DATA_DIR,
		memtable_size=engine_kw.pop("memtable_size", 200),
		num_levels=engine_kw.pop("num_levels", 4),
		l0_capacity_bytes=engine_kw.pop("l0_capacity_bytes", 50000),
		stats=stats,
		deadline=deadline,
		mode=mode,
		**engine_kw
	)

	reference = {} if verify else None
	start = time.perf_counter()
	for op, key, value in ops:
		if op == "put":
			db.put(key, value)
			if verify:
				reference[key] = value
		elif op == "delete":
			db.delete(key)
			if verify:
				reference[key] = None
		else:
			db.get(key)
	db.flush()
	elapsed = time.perf_counter() - start

	if verify:
		wrong = [k for k in reference if db.get(k) != reference[k]]
		if wrong:
			raise AssertionError(f"{label or mode}: {len(wrong)} keys incorrect")

	unerased = _count_unerased(db, ops)
	violations = _count_deadline_violations(db, ops, deadline)
	live_bytes = sum(s.size_bytes for level in db.data_levels for s in level)
	file_count = sum(len(level) for level in db.data_levels)
	file_count += sum(len(level) for level in db.tombstone_levels)

	shutil.rmtree(DATA_DIR, ignore_errors=True)
	return Result(label or mode, mode, stats, elapsed, unerased, live_bytes,
		file_count, violations)


def _count_unerased(db, ops):
	"""
	Deleted records still physically on disk.

	This is the compliance measure: a design that is cheap because it skips
	erasures is not cheaper, it is non-compliant. Comparing this across modes
	keeps the WAF comparison honest.
	"""
	final_state = {}
	for op, key, value in ops:
		if op == "put":
			final_state[key] = value
		elif op == "delete":
			final_state[key] = None

	deleted = {k for k, v in final_state.items() if v is None}
	return sum(
		1
		for level in db.data_levels for sst in level for rec in sst.records
		if rec.type == "PUT" and rec.key in deleted
	)


def _count_deadline_violations(db, ops, deadline):
	"""
	Deleted records still on disk after their deadline expired.

	This is the compliance verdict, and the only fair way to compare designs.
	Raw unerased counts favour FADE artificially: its wide merge rewrites every
	range-overlapping file, so it incidentally erases records whose deadlines
	have not yet arrived. Doing more work than required is not better compliance.
	"""
	if deadline is None:
		return 0

	deleted_at = {}
	for op, key, value in ops:
		if op == "put":
			deleted_at.pop(key, None)
		elif op == "delete":
			deleted_at[key] = None  # filled in below with the engine's clock

	# replay order gives relative timing; use the engine's final clock as "now"
	now = db.seqnum
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
		1
		for level in db.data_levels for sst in level for rec in sst.records
		if rec.type == "PUT" and rec.key in pending
		and rec.seqnum < pending[rec.key]
		and now - pending[rec.key] > deadline
	)


def compare_modes(ops, deadline, labels=None, verify=False, **engine_kw):
	"""Run the same workload through vanilla, FADE and the decoupled design."""
	labels = labels or {}
	configs = [
		(MODE_VANILLA, None, labels.get(MODE_VANILLA, "Vanilla")),
		(MODE_FADE, deadline, labels.get(MODE_FADE, f"FADE D={deadline}")),
		(MODE_DECOUPLED, deadline, labels.get(MODE_DECOUPLED, f"Decoupled D={deadline}")),
	]
	return [
		run_engine(ops, mode=mode, deadline=dl, label=label, verify=verify, **engine_kw)
		for mode, dl, label in configs
	]


def print_table(rows, headers):
	widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
	line = "  ".join(str(headers[i]).ljust(widths[i]) for i in range(len(headers)))
	print(line)
	print("  ".join("-" * w for w in widths))
	for row in rows:
		print("  ".join(str(row[i]).ljust(widths[i]) for i in range(len(row))))


def section(title, subtitle=None):
	print(f"\n{'=' * 74}")
	print(f"  {title}")
	if subtitle:
		print(f"  {subtitle}")
	print("=" * 74)
