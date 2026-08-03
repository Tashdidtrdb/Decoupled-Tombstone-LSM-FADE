import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lsm import LSM, MODE_VANILLA, MODE_FADE, MODE_DECOUPLED
from stats import Stats
from workload import generate

TMP = "/tmp/test_modes"


def _run(mode, deadline, ops, key_space=500):
	shutil.rmtree(TMP, ignore_errors=True)
	stats = Stats()
	db = LSM(TMP, memtable_size=200, l0_capacity_bytes=50000, stats=stats,
		deadline=deadline, mode=mode)
	reference = {}
	for op, k, v in ops:
		if op == "put":
			db.put(k, v)
			reference[k] = v
		elif op == "delete":
			db.delete(k)
			reference[k] = None
		else:
			db.get(k)
	db.flush()
	wrong = [k for k in range(key_space) if db.get(k) != reference.get(k)]
	return db, stats, reference, wrong


def _unerased(db, reference):
	return sum(
		1
		for level in db.data_levels for sst in level for rec in sst.records
		if rec.type == "PUT" and reference.get(rec.key) is None
	)


def test_all_modes_are_correct():
	ops = generate(num_ops=6000, key_space=500, delete_ratio=0.25, skew="zipf", seed=1)
	for mode, deadline in [(MODE_VANILLA, None), (MODE_FADE, 200), (MODE_DECOUPLED, 200)]:
		_, _, _, wrong = _run(mode, deadline, ops)
		assert not wrong, f"{mode}: {len(wrong)} keys wrong: {wrong[:5]}"
	print("modes pass -- vanilla, fade and decoupled all return correct values")


def test_decoupled_costs_less_than_fade():
	# the contribution, stated as a test: same deadline, same erasures, less work
	for skew in ["zipf", "uniform"]:
		ops = generate(num_ops=6000, key_space=500, delete_ratio=0.25, skew=skew, seed=1)
		_, fade, ref_f, _ = _run(MODE_FADE, 200, ops)
		_, dec, ref_d, _ = _run(MODE_DECOUPLED, 200, ops)
		assert dec.compliance_bytes_written < fade.compliance_bytes_written, \
			f"{skew}: decoupled wrote {dec.compliance_bytes_written} vs fade {fade.compliance_bytes_written}"
		saving = 100 * (1 - dec.compliance_waf() / fade.compliance_waf())
		print(f"modes pass -- {skew}: decoupled compliance WAF {dec.compliance_waf():.2f} "
			f"vs fade {fade.compliance_waf():.2f} ({saving:.0f}% less)")


def test_both_deadline_modes_erase_equally():
	# a cheaper design is only meaningful if it honours the same deadline
	ops = generate(num_ops=6000, key_space=500, delete_ratio=0.25, skew="uniform", seed=1)
	db_f, _, ref_f, _ = _run(MODE_FADE, 200, ops)
	db_d, _, ref_d, _ = _run(MODE_DECOUPLED, 200, ops)
	assert _unerased(db_f, ref_f) == _unerased(db_d, ref_d), \
		"fade and decoupled left different amounts of deleted data on disk"
	print(f"modes pass -- both deadline modes leave the same {_unerased(db_d, ref_d)} "
		f"records pending (within their deadline window)")


def test_vanilla_does_no_compliance_work():
	ops = generate(num_ops=4000, key_space=500, delete_ratio=0.25, skew="zipf", seed=2)
	_, stats, _, _ = _run(MODE_VANILLA, None, ops)
	assert stats.compliance_bytes_written == 0
	assert stats.compliance_compaction_count == 0
	print("modes pass -- vanilla performs no deadline-driven work")


def test_vanilla_leaves_deleted_data_behind():
	# the motivating problem: without a deadline, deleted records linger
	ops = generate(num_ops=4000, key_space=500, delete_ratio=0.25, skew="zipf", seed=2)
	db, _, reference, _ = _run(MODE_VANILLA, None, ops)
	assert _unerased(db, reference) > 0, \
		"vanilla erased everything, so there is no problem to solve"
	print(f"modes pass -- vanilla leaves {_unerased(db, reference)} deleted records on disk")


def test_mode_validation():
	shutil.rmtree(TMP, ignore_errors=True)
	try:
		LSM(TMP, mode="nonsense")
		assert False, "accepted an unknown mode"
	except ValueError:
		pass
	try:
		LSM(TMP, mode=MODE_FADE, deadline=None)
		assert False, "accepted fade without a deadline"
	except ValueError:
		pass
	# defaults stay backwards compatible
	assert LSM(TMP, deadline=None).mode == MODE_VANILLA
	assert LSM(TMP, deadline=100).mode == MODE_DECOUPLED
	print("modes pass -- invalid modes rejected, defaults preserved")


if __name__ == "__main__":
	test_all_modes_are_correct()
	test_decoupled_costs_less_than_fade()
	test_both_deadline_modes_erase_equally()
	test_vanilla_does_no_compliance_work()
	test_vanilla_leaves_deleted_data_behind()
	test_mode_validation()
