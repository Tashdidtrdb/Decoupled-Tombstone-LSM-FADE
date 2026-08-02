from bloom import BloomFilter, DEFAULT_BITS_PER_ELEMENT

# Monkey [8] shows a uniform bits-per-element allocation across levels is
# suboptimal. Delete density is highest at shallow levels, where a tombstone is
# newest and most likely to be probed, so shallow levels get a richer budget and
# deep levels a leaner one. Values are bits per element, indexed by level.
DEFAULT_LEVEL_BITS = [14, 12, 10, 8]


def bits_for_level(level_idx, level_bits=None):
	table = level_bits or DEFAULT_LEVEL_BITS
	if level_idx < len(table):
		return table[level_idx]
	return table[-1]


class TombstoneIndex:
	"""
	Per-level Bloom filter over every tombstone key resident at that level.

	Purpose is the negative case. A miss at every level proves no tombstone exists
	for the key anywhere on disk, so a lookup can trust the data pipeline's answer
	without opening a single tombstone file.

	A hit does NOT prove the key is deleted: a key may be tombstoned and later
	re-inserted, in which case the newer PUT wins. Callers must still resolve by
	sequence number. The filter narrows which files to open, it does not decide
	visibility.

	Filters are rebuilt when a level's file set changes. Bloom filters cannot
	support deletion of individual entries, so a rebuild is the only correct way
	to drop keys once a tombstone file leaves the level.
	"""

	def __init__(self, num_levels, level_bits=None):
		self.num_levels = num_levels
		self.level_bits = level_bits or DEFAULT_LEVEL_BITS
		self.filters = [None] * num_levels
		self.key_counts = [0] * num_levels
		self.rebuild_count = 0

	def rebuild_level(self, level_idx, tombstone_ssts):
		"""Recompute one level's filter from the tombstone files now resident there."""
		keys = [rec.key for sst in tombstone_ssts for rec in sst.records]
		bits = bits_for_level(level_idx, self.level_bits)
		self.filters[level_idx] = BloomFilter.build(keys, bits) if keys else None
		self.key_counts[level_idx] = len(keys)
		self.rebuild_count += 1

	def may_contain(self, key, level_idx):
		f = self.filters[level_idx]
		if f is None:
			return False
		return f.contains(key)

	def any_level_may_contain(self, key):
		"""
		False means no tombstone for this key exists at any level -- definitive.
		True means some level probably holds one, and the caller must check.
		"""
		return any(self.may_contain(key, i) for i in range(self.num_levels))

	def levels_that_may_contain(self, key):
		return [i for i in range(self.num_levels) if self.may_contain(key, i)]

	def memory_bytes(self):
		return sum(f.memory_bytes() for f in self.filters if f is not None)

	def describe(self):
		rows = []
		for i, f in enumerate(self.filters):
			if f is None:
				rows.append(f"  L{i}: empty")
			else:
				rows.append(
					f"  L{i}: {self.key_counts[i]} keys, "
					f"{bits_for_level(i, self.level_bits)} bits/elem, "
					f"{f.memory_bytes()}B, est FPR {f.estimated_fpr():.4f}"
				)
		return "\n".join(rows)
