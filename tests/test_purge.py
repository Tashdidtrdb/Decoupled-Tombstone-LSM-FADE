import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lsm import LSM
from stats import Stats
from workload import generate

TMP = "/tmp/test_purge"


def _engine(deadline=100, memtable_size=20, **kw):
	shutil.rmtree(TMP, ignore_errors=True)
	stats = Stats()
	db = LSM(TMP, memtable_size=memtable_size, stats=stats, deadline=deadline,
		l0_capacity_bytes=kw.pop("l0_capacity_bytes", 10 ** 8), **kw)
	return db, stats


def _live_puts_on_disk(db, keys):
	"""PUT records still physically present for any of `keys`."""
	wanted = set(keys)
	return [
		(rec.key, rec.seqnum)
		for level in db.data_levels for sst in level for rec in sst.records
		if rec.key in wanted and rec.type == "PUT"
	]


def test_deleted_data_is_physically_erased():
	# THE compliance claim: once a tombstone passes its deadline, the record it
	# invalidates must be gone from disk, not merely hidden behind a marker
	db, stats = _engine()
	for i in range(50):
		db.put(i, "v" * 20)
	for i in range(25):
		db.delete(i)
	for i in range(1000, 1600):  # age the clock well past the deadline
		db.put(i, "v")
	db.flush()
	for i in range(2000, 2200):  # let the final flush's tombstones expire too
		db.put(i, "v")
	db.flush()

	surviving = _live_puts_on_disk(db, range(25))
	assert not surviving, f"{len(surviving)} deleted records still on disk: {surviving[:5]}"
	print(f"purge pass -- all 25 deleted records physically erased "
		f"({stats.tombstones_retired} tombstones retired)")


def test_live_data_survives_purging():
	db, stats = _engine()
	for i in range(50):
		db.put(i, "v" * 20)
	for i in range(25):
		db.delete(i)
	for i in range(1000, 1600):
		db.put(i, "v")
	db.flush()
	for i in range(25, 50):
		assert db.get(i) == "v" * 20, f"live key {i} was destroyed by a purge"
	print("purge pass -- keys that were never deleted survive intact")


def test_reinserted_key_survives_purge():
	# a purge must drop only records older than the tombstone; a newer PUT is a
	# resurrection and has to stay
	db, stats = _engine()
	db.put(1, "original")
	db.delete(1)
	db.put(1, "resurrected")
	for i in range(1000, 1600):
		db.put(i, "v")
	db.flush()
	assert db.get(1) == "resurrected", f"purge destroyed a re-inserted key: {db.get(1)}"
	print("purge pass -- re-inserted value survives the purge of its predecessor")


def test_purge_is_narrower_than_a_wide_merge():
	# the contribution: compliance work should touch a fraction of the data
	db, stats = _engine(memtable_size=100, l0_capacity_bytes=50000)
	for op, k, v in generate(num_ops=8000, key_space=1000, delete_ratio=0.25,
			skew="uniform", seed=3):
		if op == "put":
			db.put(k, v)
		elif op == "delete":
			db.delete(k)
	db.flush()
	total_data = sum(s.size_bytes for level in db.data_levels for s in level)
	assert stats.compliance_bytes_written > 0, "no compliance work happened at all"

	# What matters is the blast radius of each compliance event, not the total
	# across the run: a tight deadline legitimately fires many purges. A wide
	# merge would rewrite most of the dataset every time; a targeted rewrite
	# should touch a small fraction of it.
	per_event = stats.compliance_bytes_written / stats.compliance_compaction_count
	fraction = per_event / max(total_data, 1)
	assert fraction < 0.25, \
		f"each compliance rewrite touches {100*fraction:.1f}% of the dataset"
	print(f"purge pass -- each compliance rewrite touches {per_event:.0f}B, "
		f"{100*fraction:.1f}% of the {total_data}B dataset")


def test_tombstones_are_retired():
	# without retirement, tombstone files accumulate without bound
	db, stats = _engine()
	for i in range(40):
		db.put(i, "v" * 20)
	for i in range(40):
		db.delete(i)
	for i in range(1000, 1600):
		db.put(i, "v")
	db.flush()
	assert stats.tombstones_retired > 0, "no tombstone was ever retired"
	print(f"purge pass -- {stats.tombstones_retired} spent tombstones retired")


def test_correctness_under_workload():
	db, stats = _engine(deadline=200, memtable_size=100, l0_capacity_bytes=20000)
	reference = {}
	for op, k, v in generate(num_ops=6000, key_space=800, delete_ratio=0.3,
			skew="zipf", seed=17):
		if op == "put":
			db.put(k, v)
			reference[k] = v
		elif op == "delete":
			db.delete(k)
			reference[k] = None
	db.flush()
	wrong = [k for k in range(800) if db.get(k) != reference.get(k)]
	assert not wrong, f"{len(wrong)} keys disagree with reference: {wrong[:5]}"
	print("purge pass -- 800 keys match reference with purging active")


if __name__ == "__main__":
	test_deleted_data_is_physically_erased()
	test_live_data_survives_purging()
	test_reinserted_key_survives_purge()
	test_purge_is_narrower_than_a_wide_merge()
	test_tombstones_are_retired()
	test_correctness_under_workload()
