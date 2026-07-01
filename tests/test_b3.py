import os
import sys
import shutil
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lsm import LSM

DATA_DIR = "/tmp/test_b3_data"

def cleanup():
	shutil.rmtree(DATA_DIR, ignore_errors=True)

def fresh_fade(deadline, memtable_size=5, l0_capacity_bytes=10**9):
	shutil.rmtree(DATA_DIR, ignore_errors=True)
	# large l0_capacity_bytes so capacity-driven compaction never fires on its own
	return LSM(DATA_DIR, memtable_size=memtable_size, num_levels=3,
		l0_capacity_bytes=l0_capacity_bytes, deadline=deadline)

def test_fade_triggers_without_capacity_pressure():
	# deadline=50, 3 levels -> level_ttl=[16, 33, 50]
	# L0 fires when tombstone age > 16
	db = fresh_fade(deadline=50)

	db.put(99, "to delete")
	db.flush()
	tombstone_seqnum = db.seqnum + 1
	db.delete(99)
	db.flush()  # tombstone now in L0

	compactions_before = db.stats.compaction_count

	# advance the clock past level_ttl[0]=16 without triggering capacity compaction
	# each put + flush = memtable_size=5 puts then flush
	for i in range(200):
		db.put(i + 1000, f"filler{i}")
	db.flush()

	assert db.stats.compaction_count > compactions_before, "FADE eager compaction did not fire"
	print(f"B3 pass -- FADE fired {db.stats.compaction_count - compactions_before} compaction(s) without capacity pressure")
	cleanup()

def test_fade_higher_waf_than_vanilla():
	# same workload, tight deadline on FADE engine vs no deadline on vanilla
	# FADE should write more because it eagerly rewrites files to enforce the deadline

	def run(deadline):
		shutil.rmtree(DATA_DIR, ignore_errors=True)
		db = LSM(DATA_DIR, memtable_size=10, num_levels=3,
			l0_capacity_bytes=10**9, deadline=deadline)
		for i in range(300):
			db.put(i, f"val{i}")
		for i in range(0, 100):
			db.delete(i)
		for i in range(300, 500):
			db.put(i, f"val{i}")
		db.flush()
		return db.stats

	vanilla = run(deadline=None)
	fade = run(deadline=100)

	print(f"B3 -- vanilla WAF: {vanilla.waf():.2f}, FADE WAF: {fade.waf():.2f}")
	print(f"B3 -- vanilla compactions: {vanilla.compaction_count}, FADE compactions: {fade.compaction_count}")
	assert fade.bytes_written >= vanilla.bytes_written, "FADE should write >= vanilla under eager compaction"
	print("B3 pass -- FADE writes >= vanilla (eager compaction adds write overhead)")
	cleanup()

def test_deleted_key_gone_after_fade_compaction():
	db = fresh_fade(deadline=50)
	db.put(42, "secret")
	db.flush()
	db.delete(42)
	db.flush()

	# drive clock forward until FADE fires and tombstone reaches bottom
	for i in range(500):
		db.put(i + 2000, f"v{i}")
	db.flush()

	assert db.get(42) is None
	print("B3 pass -- deleted key gone after FADE compaction cascade")
	cleanup()

if __name__ == "__main__":
	test_fade_triggers_without_capacity_pressure()
	test_fade_higher_waf_than_vanilla()
	test_deleted_key_gone_after_fade_compaction()
