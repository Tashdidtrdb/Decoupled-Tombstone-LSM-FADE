import os
from memtable import MemTable
from sst import SSTable
from stats import Stats

class LSM:
	def __init__(self, data_dir, memtable_size=1000, num_levels=4, l0_capacity_bytes=1024*1024, stats=None, deadline=None):
		os.makedirs(data_dir, exist_ok=True)
		self.data_dir = data_dir
		self.stats = stats or Stats()
		self.seqnum = 0
		self._file_counter = 0
		self.memtable = MemTable(memtable_size, self.stats)
		# levels[0] = L0, files ordered newest first so get() finds highest seqnum first
		self.levels = [[] for _ in range(num_levels)]
		# each level is 10x the capacity of the one above it
		self.level_capacity = [l0_capacity_bytes * (10 ** i) for i in range(num_levels)]
		# deadline D in ops; per-level TTL is an even split so shallow levels flush tombstones faster
		self.deadline = deadline
		# cumulative TTL: level i fires when tombstone age > (i+1) * (D/L)
		# this gives each level a D/L window before cascading the tombstone further down
		self.level_ttl = [(i + 1) * (deadline // num_levels) for i in range(num_levels)] if deadline else None

	def _new_path(self):
		path = os.path.join(self.data_dir, f"sst_{self._file_counter}.json")
		self._file_counter += 1
		return path

	def _flush_memtable(self):
		sst = self.memtable.flush(self._new_path())
		self.levels[0].insert(0, sst)  # newest L0 file at front
		self._maybe_compact()

	def put(self, key, value):
		self.seqnum += 1
		self.stats.record_ingest(len(str(value)))
		self.memtable.put(key, value, self.seqnum)
		if self.memtable.is_full():
			self._flush_memtable()

	def delete(self, key):
		self.seqnum += 1
		self.memtable.delete(key, self.seqnum)
		if self.memtable.is_full():
			self._flush_memtable()

	def get(self, key):
		# check memtable first
		if key in self.memtable.data:
			rec = self.memtable.data[key]
			self.stats.record_lookup(0)
			return None if rec.type == "DELETE" else rec.value

		files_touched = 0
		for level in self.levels:
			# L0 files can overlap, so scan all of them newest-first
			# L1+ files don't overlap, so at most one per level matches
			for sst in level:
				if sst.min_key <= key <= sst.max_key:
					files_touched += 1
					for rec in sst.records:
						if rec.key == key:
							self.stats.record_lookup(files_touched)
							return None if rec.type == "DELETE" else rec.value

		self.stats.record_lookup(files_touched)
		return None

	def flush(self):
		# force flush remaining memtable entries
		if self.memtable.data:
			self._flush_memtable()

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
			new_sst = SSTable(merged, self._new_path())
			new_sst.write()
			self.stats.record_write(new_sst.size_bytes)
			dst_level.append(new_sst)
			# keep L1+ sorted by key range so get() scans stay correct
			dst_level.sort(key=lambda s: s.min_key)

	def _expired_tombstone_file(self, level_idx):
		# find a file at this level whose oldest tombstone has exceeded the per-level TTL
		if not self.level_ttl:
			return None
		for sst in self.levels[level_idx]:
			if self.seqnum - sst.oldest_tombstone_time > self.level_ttl[level_idx]:
				return sst
		return None

	def _maybe_compact(self):
		# walk forward, re-checking each level until it's under capacity before advancing
		i = 0
		while i < len(self.levels) - 1:
			if self._level_bytes(i) > self.level_capacity[i]:
				self._compact(i)
			else:
				expired = self._expired_tombstone_file(i)
				if expired:
					self._compact(i, src_file=expired)
				else:
					i += 1
