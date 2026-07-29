import os
from memtable import MemTable
from sst import SSTable
from stats import Stats

DEBUG = False

class LSM:
	def __init__(self, data_dir, memtable_size=1000, num_levels=4, l0_capacity_bytes=1024*1024, stats=None, deadline=None):
		os.makedirs(data_dir, exist_ok=True)
		self.data_dir = data_dir
		self.stats = stats or Stats()
		self.seqnum = 0
		self._file_counter = 0

		# Separate MemTables
		self.data_memtable = MemTable(memtable_size, self.stats)
		# levels[0] = L0, files ordered newest first so get() finds highest seqnum first
		self.tombstone_memtable = MemTable(memtable_size, self.stats)

		# Separate SST hierarchies
		self.data_levels = [[] for _ in range(num_levels)]
		# each level is 10x the capacity of the one above it
		self.tombstone_levels = [[] for _ in range(num_levels)]

		# Temporary compatibility with existing compaction code.
		# Will be removed after separate compaction is implemented.
		self.levels = self.data_levels

		self.level_capacity = [l0_capacity_bytes * (10 ** i) for i in range(num_levels)]
		# deadline D in ops; per-level TTL is an even split so shallow levels flush tombstones faster
		self.deadline = deadline
		# cumulative TTL: level i fires when tombstone age > (i+1) * (D/L)
		# this gives each level a D/L window before cascading the tombstone further down
		self.level_ttl = [(i + 1) * (deadline // num_levels) for i in range(num_levels)] if deadline else None

	def _new_path(self, prefix):
		path = os.path.join(
			self.data_dir,
			f"{prefix}_sst_{self._file_counter}.json"
		)
		self._file_counter += 1
		return path

	def _flush_data_memtable(self):
		sst = self.data_memtable.flush(
			self._new_path("data")
		)
		self.data_levels[0].insert(0, sst)

		# For now keep the existing scheduler
		self._maybe_compact_data()


	def _flush_tombstone_memtable(self):
		sst = self.tombstone_memtable.flush(
			self._new_path("tombstone")
		)
		sst.level_entry_seqnum = self.seqnum
		self.tombstone_levels[0].insert(0, sst)

		# Scheduler will be separated later
		self._maybe_compact_tombstones()

	def put(self, key, value):
		self.seqnum += 1
		self.stats.record_ingest(len(str(value)))
		self.data_memtable.put(key, value, self.seqnum)

		if self.data_memtable.is_full():
			self._flush_data_memtable()
		self._maybe_compact_tombstones()

	def delete(self, key):
		self.seqnum += 1
		self.tombstone_memtable.delete(key, self.seqnum)

		if self.tombstone_memtable.is_full():
			self._flush_tombstone_memtable()
		self._maybe_compact_tombstones()

	def get(self, key):
		"""
		Find the newest PUT or DELETE record across both pipelines.
		The record with the highest sequence number wins.
		"""
		newest_record = None
		files_touched = 0

		# Check both MemTables
		for memtable in [self.data_memtable, self.tombstone_memtable]:
			if key in memtable.data:
				rec = memtable.data[key]

				if newest_record is None or rec.seqnum > newest_record.seqnum:
					newest_record = rec

		# Check both data SSTs and tombstone SSTs
		for levels in [self.data_levels, self.tombstone_levels]:
			for level in levels:
				for sst in level:
					if sst.min_key <= key <= sst.max_key:
						files_touched += 1

						for rec in sst.records:
							if rec.key == key:
								if (
									newest_record is None
									or rec.seqnum > newest_record.seqnum
								):
									newest_record = rec

								break

		self.stats.record_lookup(files_touched)

		if newest_record is None:
			return None

		if newest_record.type == "DELETE":
			return None

		return newest_record.value

	def flush(self):
		# force flush remaining memtable entries
		if self.data_memtable.data:
			self._flush_data_memtable()

		if self.tombstone_memtable.data:
			self._flush_tombstone_memtable()

	def _level_bytes(self, level_idx):
		return sum(sst.size_bytes for sst in self.levels[level_idx])

	def _compact(self, level_idx, src_file=None):
		src_level = self.levels[level_idx]
		dst_level = self.levels[level_idx + 1]

		if src_file is None:
			# capacity-driven: oldest in L0, first in L1+
			src_file = src_level[-1] if level_idx == 0 else src_level[0]

		overlapping_dst = [sst for sst in dst_level if src_file.overlaps(sst)]

		# L0 files can overlap each other, so also pull in any L0 siblings that
		# cover the same key range, otherwise a newer record in another L0 file
		# could end up below a tombstone that was compacted without it
		overlapping_src = []
		if level_idx == 0:
			overlapping_src = [sst for sst in src_level if sst is not src_file and src_file.overlaps(sst)]

		all_records = list(src_file.records)
		for sst in overlapping_src + overlapping_dst:
			all_records.extend(sst.records)

		# sort by key asc, seqnum desc so highest seqnum per key comes first
		all_records.sort(key=lambda r: (r.key, -r.seqnum))

		is_bottom = (level_idx + 1 == len(self.levels) - 1)
		merged = []
		seen = set()
		for rec in all_records:
			if rec.key in seen:
				continue
			seen.add(rec.key)
			# only drop tombstones at the bottom level, below here there is no older data
			if rec.type == "DELETE" and is_bottom:
				continue
			merged.append(rec)

		self.stats.record_compaction(1 + len(overlapping_src) + len(overlapping_dst))

		for sst in [src_file] + overlapping_src + overlapping_dst:
			if sst in src_level:
				src_level.remove(sst)
			elif sst in dst_level:
				dst_level.remove(sst)
			os.remove(sst.filepath)

		if merged:
			new_sst = SSTable(merged, self._new_path("data"))
			new_sst.write()
			self.stats.record_write(new_sst.size_bytes)
			dst_level.append(new_sst)
			# keep L1+ sorted by key range so get() scans stay correct
			dst_level.sort(key=lambda s: s.min_key)
	def _compact_tombstones(self, level_idx, expired_sst):
		"""
		Compliance-driven tombstone compaction.

		Compacts only tombstone SST files. It does not perform
		normal capacity-driven data compaction.
		"""
		src_level = self.tombstone_levels[level_idx]
		dst_level = self.tombstone_levels[level_idx + 1]

		# Find tombstone SSTs in the next level with overlapping key ranges
		overlapping_dst = [
			sst for sst in dst_level
			if expired_sst.overlaps(sst)
		]

		# Collect records only from tombstone files
		all_records = list(expired_sst.records)

		for sst in overlapping_dst:
			all_records.extend(sst.records)

		# Newest tombstone for each key must win
		all_records.sort(key=lambda r: (r.key, -r.seqnum))

		merged = []
		seen = set()

		for rec in all_records:
			if rec.key in seen:
				continue

			seen.add(rec.key)
			merged.append(rec)

		# Record this independently triggered compaction
		self.stats.record_compaction(
			1 + len(overlapping_dst)
		)

		# Remove old tombstone SST files
		input_files = [expired_sst] + overlapping_dst

		for sst in input_files:
			if sst in src_level:
				src_level.remove(sst)

			if sst in dst_level:
				dst_level.remove(sst)

			if os.path.exists(sst.filepath):
				os.remove(sst.filepath)

		# Write a new dedicated tombstone SST
		if merged:
			new_sst = SSTable(
				merged,
				self._new_path("tombstone"),
				level_entry_seqnum=self.seqnum
			)

			new_sst.write()
			self.stats.record_write(new_sst.size_bytes)

			dst_level.append(new_sst)
			dst_level.sort(key=lambda sst: sst.min_key)
		if DEBUG:
			print(
					f"[TTL] Compacted tombstone SST "
					f"L{level_idx} -> L{level_idx + 1}"
				)

	def _expired_tombstone_file(self, level_idx):
		if not self.level_ttl:
			return None

		for sst in self.tombstone_levels[level_idx]:
			if sst.level_entry_seqnum is None:
				sst.level_entry_seqnum = self.seqnum

			# Each level receives its own portion of the total deadline
			per_level_ttl = max(
				1,
				self.deadline // len(self.tombstone_levels)
			)

			age_in_level = self.seqnum - sst.level_entry_seqnum

			if age_in_level > per_level_ttl:
				return sst

		return None

	def _maybe_compact_data(self):
		# walk forward, re-checking each level until it's under capacity before advancing
		# data compaction is capacity-driven only; tombstone TTL is handled by
		# _maybe_compact_tombstones, which owns the tombstone hierarchy
		i = 0
		while i < len(self.data_levels) - 1:
			if self._level_bytes(i) > self.level_capacity[i]:
				self._compact(i)
			else:
				i += 1

	def _maybe_compact_tombstones(self):
		# Vanilla mode has no delete-persistence deadline
		if not self.level_ttl:
			return

		for level_idx in range(len(self.tombstone_levels) - 1):
			while True:
				expired = self._expired_tombstone_file(level_idx)

				if expired is None:
					break

				self._compact_tombstones(
					level_idx,
					expired
				)
