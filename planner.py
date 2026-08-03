class PurgePlan:
	"""
	The set of data files that must be rewritten to physically erase a batch of
	tombstoned keys, plus which keys land in each file.
	"""

	def __init__(self):
		self.targets = {}  # id(sst) -> (sst, set of keys to drop)
		self.probed_files = 0
		self.filter_rejections = 0

	def add(self, sst, key):
		entry = self.targets.get(id(sst))
		if entry is None:
			self.targets[id(sst)] = (sst, {key})
		else:
			entry[1].add(key)

	@property
	def files(self):
		return [sst for sst, _ in self.targets.values()]

	def keys_for(self, sst):
		return self.targets[id(sst)][1]

	def bytes_to_rewrite(self):
		return sum(sst.size_bytes for sst in self.files)

	def __len__(self):
		return len(self.targets)

	def __bool__(self):
		return bool(self.targets)


def plan_purge(keys, data_levels, stats=None):
	"""
	Work out the minimum set of data files that actually hold any of `keys`.

	This is the core of the decoupled design. FADE selects files by key-range
	overlap, which pulls in every file whose range happens to span the tombstone,
	most of which hold none of the deleted keys. Here each key is probed against
	per-file Bloom filters, so a file is selected only when it plausibly holds a
	key being erased.

	The Bloom filter's false positives cost a wasted rewrite, never a missed
	erasure: a filter never reports absent for a key it holds, so no file that
	needs scrubbing is skipped.
	"""
	plan = PurgePlan()
	for level in data_levels:
		for sst in level:
			for key in keys:
				if not (sst.min_key <= key <= sst.max_key):
					continue
				plan.probed_files += 1
				if not sst.bloom.contains(key):
					plan.filter_rejections += 1
					continue
				plan.add(sst, key)
	if stats is not None:
		stats.record_purge_planning(plan.probed_files, plan.filter_rejections)
	return plan


def scope_if_range_based(keys, data_levels):
	"""
	Bytes a FADE-style range merge would rewrite for the same keys: every file
	whose range spans the tombstone batch, regardless of whether it holds any of
	the keys. Used to quantify what the targeted plan avoids.
	"""
	if not keys:
		return 0
	lo, hi = min(keys), max(keys)
	return sum(
		sst.size_bytes
		for level in data_levels
		for sst in level
		if sst.min_key <= hi and sst.max_key >= lo
	)
