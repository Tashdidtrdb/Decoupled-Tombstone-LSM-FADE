import os
from memtable import MemTable
from sst import SSTable
from planner import plan_purge
from stats import Stats
from tombstone_index import TombstoneIndex

DEBUG = False

# Logical payload charged to ingest for one delete. put() charges only the value
# payload (not the serialized record), so a delete is charged the equivalent unit:
# the key identifying what to erase. Deletes are user work and must appear in the
# WAF denominator, otherwise delete-heavy workloads look artificially amplified.
TOMBSTONE_INGEST_BYTES = 8

# Max records per compaction output file. Compaction merges its inputs into one
# sorted run and then cuts it into files of this size, so each output file covers
# a narrow, disjoint key range. Without the cut, one output file spans the union
# of all input ranges and every later targeted rewrite degenerates into a full
# rewrite. Smaller values sharpen targeting but multiply file count -- the
# proposal flags file proliferation as a risk, so this is a tunable knob.
DEFAULT_TARGET_FILE_RECORDS = 50

# Compliance strategies compared in the evaluation.
#   vanilla   -- no deadline; tombstones propagate lazily via capacity compaction
#   fade      -- deadline met by a wide merge of every range-overlapping data file
#   decoupled -- deadline met by rewriting only the files that hold a deleted key
MODE_VANILLA = "vanilla"
MODE_FADE = "fade"
MODE_DECOUPLED = "decoupled"

class LSM:
	def __init__(self, data_dir, memtable_size=1000, num_levels=4, l0_capacity_bytes=1024*1024, stats=None, deadline=None, tombstone_memtable_share=0.5, tombstone_filter_bits=None, target_file_records=DEFAULT_TARGET_FILE_RECORDS, mode=None):
		os.makedirs(data_dir, exist_ok=True)
		self.data_dir = data_dir
		self.stats = stats or Stats()
		self.seqnum = 0
		self._file_counter = 0

		# memtable_size is the TOTAL in-memory buffer budget, split across the two
		# memtables. Giving each one the full budget would hand this engine 2x the
		# memory of the vanilla single-memtable engine and make it flush at a
		# different rate, so Vanilla vs Decoupled WAF would not be comparable.
		self.tombstone_memtable_size = max(1, int(memtable_size * tombstone_memtable_share))
		self.data_memtable_size = max(1, memtable_size - self.tombstone_memtable_size)

		# Separate MemTables
		self.data_memtable = MemTable(self.data_memtable_size, self.stats)
		# levels[0] = L0, files ordered newest first so get() finds highest seqnum first
		self.tombstone_memtable = MemTable(self.tombstone_memtable_size, self.stats)

		# Separate SST hierarchies
		self.data_levels = [[] for _ in range(num_levels)]
		# each level is 10x the capacity of the one above it
		self.tombstone_levels = [[] for _ in range(num_levels)]
		# Per-level membership filters over tombstone keys. A miss at every level
		# proves no tombstone exists for a key, letting get() skip the whole
		# tombstone hierarchy without opening a file.
		self.tombstone_index = TombstoneIndex(num_levels, level_bits=tombstone_filter_bits)

		# Temporary compatibility with existing compaction code.
		# Will be removed after separate compaction is implemented.
		self.levels = self.data_levels

		# None disables splitting (one output file per compaction, the old behaviour)
		self.target_file_records = target_file_records
		self.level_capacity = [l0_capacity_bytes * (10 ** i) for i in range(num_levels)]
		# deadline D in ops; per-level TTL is an even split so shallow levels flush tombstones faster
		self.deadline = deadline
		# mode defaults from the deadline so existing callers keep working:
		# no deadline means vanilla, a deadline means the decoupled design
		if mode is None:
			mode = MODE_VANILLA if deadline is None else MODE_DECOUPLED
		if mode not in (MODE_VANILLA, MODE_FADE, MODE_DECOUPLED):
			raise ValueError(f"unknown mode: {mode}")
		if mode != MODE_VANILLA and deadline is None:
			raise ValueError(f"mode {mode!r} requires a deadline")
		self.mode = mode
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
		self._refresh_tombstone_index(0)

		# Scheduler will be separated later
		self._maybe_compact_tombstones()

	def _refresh_tombstone_index(self, *level_idxs):
		"""
		Rebuild the filters for levels whose tombstone file set just changed.
		Bloom filters cannot retract individual keys, so any change to a level's
		contents requires rebuilding that level's filter from scratch.
		"""
		for level_idx in level_idxs:
			self.tombstone_index.rebuild_level(
				level_idx,
				self.tombstone_levels[level_idx]
			)

	def put(self, key, value):
		self.seqnum += 1
		self.stats.record_ingest(len(str(value)))
		self.data_memtable.put(key, value, self.seqnum)

		if self.data_memtable.is_full():
			self._flush_data_memtable()
		self._maybe_compact_tombstones()

	def delete(self, key):
		self.seqnum += 1
		# a delete is user-issued work that causes real writes, so it must count
		# toward ingest or WAF inflates purely because a workload deletes more
		self.stats.record_ingest(TOMBSTONE_INGEST_BYTES)
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

		# Consult the tombstone registry once. A miss at every level is definitive:
		# no tombstone exists for this key, so the entire tombstone hierarchy can be
		# skipped without opening a file. A hit is not conclusive -- a key may be
		# tombstoned and later re-inserted -- so the seqnum comparison below still
		# decides which record wins.
		search_spaces = [self.data_levels]
		if self.tombstone_index.any_level_may_contain(key):
			search_spaces.append(self.tombstone_levels)
		else:
			self.stats.record_tombstone_hierarchy_skip()

		for levels in search_spaces:
			for level in levels:
				for sst in level:
					if not (sst.min_key <= key <= sst.max_key):
						continue

					# Range says maybe; the filter is what rules the file out.
					# A negative here costs no I/O at all.
					if not sst.bloom.contains(key):
						self.stats.record_filter_skip()
						continue

					# Filter says probably present, so pay for exactly one block
					# instead of scanning the whole file.
					block = sst.find_block(key)
					if block is None:
						self.stats.record_filter_skip()
						continue

					files_touched += 1
					self.stats.record_block_read(block.num_bytes)

					for rec in sst.read_block(block):
						if rec.key == key:
							if (
								newest_record is None
								or rec.seqnum > newest_record.seqnum
							):
								newest_record = rec

							break
					else:
						# filter said maybe but the block did not hold it
						self.stats.record_false_positive()

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

		# L0 files can overlap each other, so also pull in any L0 siblings that
		# cover the same key range, otherwise a newer record in another L0 file
		# could end up below a tombstone that was compacted without it
		overlapping_src = []
		if level_idx == 0:
			overlapping_src = [sst for sst in src_level if sst is not src_file and src_file.overlaps(sst)]

		# Destination files must be selected against the FULL key range being
		# merged, not against src_file alone. L0 siblings can widen that range
		# beyond src_file, and a dst file inside the widened span but outside
		# src_file would survive while the output covers its keys, leaving two
		# files at the same level claiming the same key.
		merge_min = min([src_file.min_key] + [s.min_key for s in overlapping_src])
		merge_max = max([src_file.max_key] + [s.max_key for s in overlapping_src])
		overlapping_dst = [
			sst for sst in dst_level
			if sst.min_key <= merge_max and sst.max_key >= merge_min
		]

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

		# Split the merged output into several key-disjoint files rather than one
		# file spanning everything. A single output file would cover the union of
		# every input range, so every subsequent compliance rewrite would have to
		# touch it. Narrow files are what let a targeted rewrite stay targeted.
		#
		# Any dst file inside the merged key range was pulled into this merge, so
		# the runs below cannot collide with a survivor.
		for run in self._split_into_runs(merged):
			new_sst = SSTable(run, self._new_path("data"))
			new_sst.write()
			self.stats.record_write(new_sst.size_bytes)
			dst_level.append(new_sst)

		# keep L1+ sorted by key range so get() scans stay correct
		dst_level.sort(key=lambda s: s.min_key)

	def _split_into_runs(self, records):
		"""
		Chop a sorted record list into consecutive runs of at most
		target_file_records. Records are already key-sorted and deduplicated, so
		consecutive runs have strictly disjoint key ranges.
		"""
		if not records:
			return []
		if not self.target_file_records:
			return [records]
		return [
			records[i:i + self.target_file_records]
			for i in range(0, len(records), self.target_file_records)
		]
	def _scrub_file(self, sst, keys, cutoff):
		"""
		Fetch one data file, drop every record invalidated by an expiring
		tombstone, and rewrite it. This is the "fetch-drop-rewrite" step: the file
		was chosen by probing per-file Bloom filters, so it is one of the few that
		actually houses a deleted record rather than every file whose key range
		happens to overlap.

		Only records older than their tombstone are dropped. A key that was deleted
		and later re-inserted has a newer PUT that must survive.
		"""
		level_idx = self._level_of(sst)
		if level_idx is None:
			return False

		kept = [
			rec for rec in sst.records
			if not (rec.key in keys and rec.seqnum < cutoff.get(rec.key, 0))
		]
		if len(kept) == len(sst.records):
			# every candidate was a filter false positive: nothing to erase here
			return False

		self.data_levels[level_idx].remove(sst)
		if os.path.exists(sst.filepath):
			os.remove(sst.filepath)

		# rewriting the scrubbed file is the cost of meeting the deadline
		self.stats.record_compaction(1, compliance=True)
		if kept:
			new_sst = SSTable(kept, self._new_path("data"))
			new_sst.write()
			self.stats.record_write(new_sst.size_bytes, compliance=True)
			self.data_levels[level_idx].append(new_sst)

		if level_idx > 0:
			self.data_levels[level_idx].sort(key=lambda s: s.min_key)
		return True

	def _level_of(self, sst):
		for level_idx, level in enumerate(self.data_levels):
			if sst in level:
				return level_idx
		return None

	def _purge_expired_tombstones(self, level_idx, expired_sst):
		"""
		Honour the delete deadline for one expired tombstone file.

		Each tombstone is applied to the data hierarchy individually. Batching a
		whole file's keys into one plan would select nearly every data file --
		hundreds of scattered keys collectively span the keyspace -- which is
		exactly the wide-merge behaviour this design replaces. Per-key purging is
		what keeps the blast radius to a single file.

		A tombstone is retired once its key is confirmed gone from the data
		hierarchy; nothing older can resurface, so the marker has no further work
		to do. Tombstones that still shadow live data are kept and cascade to the
		next level as before.
		"""
		# Plan per key so targeting stays narrow, but group the rewrites by file:
		# a file holding twenty tombstoned keys must be rewritten once, not twenty
		# times. Planning granularity controls the blast radius; rewrite grouping
		# controls how often each file in that radius is paid for.
		cutoff = {}
		targets = {}
		for rec in expired_sst.records:
			plan = plan_purge([rec.key], self.data_levels, self.stats)
			cutoff[rec.key] = max(cutoff.get(rec.key, 0), rec.seqnum)
			for sst in plan.files:
				targets.setdefault(id(sst), (sst, set()))[1].add(rec.key)

		for sst, keys in targets.values():
			self._scrub_file(sst, keys, cutoff)

		survivors = [
			rec for rec in sorted(expired_sst.records, key=lambda r: r.key)
			if self._tombstone_still_needed(rec.key, rec.seqnum)
		]
		for _ in range(len(expired_sst.records) - len(survivors)):
			self.stats.record_tombstone_retired()

		self._retire_tombstone_file(level_idx, expired_sst, survivors)

	def _tombstone_still_needed(self, key, tombstone_seqnum):
		"""
		A tombstone may only be dropped once it can no longer affect a read.

		Two reasons to keep it:
		  - an older record survives somewhere, so the tombstone is still shadowing
		    data that has not been erased yet
		  - a newer record exists for the key, in which case this tombstone is no
		    longer the newest version but is still part of the version chain that
		    a later purge must reason about

		Only when no record for the key remains at all is the marker truly spent.
		Dropping it while a newer PUT exists would be harmless for that PUT, but
		dropping it while it is the newest version would resurrect deleted data.

		The memtable counts. A purge only scrubs on-disk files, so an unflushed
		record is invisible to the planner; retiring the tombstone here would let
		that record reach disk with nothing left to shadow it.
		"""
		if key in self.data_memtable.data:
			return True

		for level in self.data_levels:
			for sst in level:
				if not sst.may_contain(key):
					continue
				for rec in sst.records:
					if rec.key == key:
						return True
		return False

	def _retire_tombstone_file(self, level_idx, expired_sst, survivors):
		"""
		Drop a spent tombstone file. Any tombstone whose data has not yet been
		fully erased is rewritten one level down so it keeps shadowing until the
		purge completes.
		"""
		src_level = self.tombstone_levels[level_idx]
		if expired_sst in src_level:
			src_level.remove(expired_sst)
		if os.path.exists(expired_sst.filepath):
			os.remove(expired_sst.filepath)

		refreshed = [level_idx]
		if survivors:
			dst_idx = min(level_idx + 1, len(self.tombstone_levels) - 1)
			dst_level = self.tombstone_levels[dst_idx]
			new_sst = SSTable(
				survivors,
				self._new_path("tombstone"),
				level_entry_seqnum=self.seqnum
			)
			new_sst.write()
			self.stats.record_write(new_sst.size_bytes, compliance=True)
			self.stats.record_compaction(1, compliance=True)
			dst_level.append(new_sst)
			dst_level.sort(key=lambda s: s.min_key)
			refreshed.append(dst_idx)

		self._refresh_tombstone_index(*set(refreshed))

	def _fade_merge(self, level_idx, expired_sst):
		"""
		FADE-style compliance compaction: the control we are measuring against.

		Selects data files by key-range overlap with the expiring tombstone file
		and rewrites every one of them. That is the wide-scope merge the decoupled
		design replaces -- most selected files hold none of the deleted keys but
		are rewritten anyway because their range happens to span the tombstone.

		Erases the same records as the targeted path, so both modes meet the same
		deadline and only the cost differs.
		"""
		keys = {rec.key: rec.seqnum for rec in expired_sst.records}
		lo, hi = min(keys), max(keys)

		for data_level_idx, level in enumerate(self.data_levels):
			for sst in [s for s in level if s.min_key <= hi and s.max_key >= lo]:
				kept = [
					rec for rec in sst.records
					if not (rec.key in keys and rec.seqnum < keys[rec.key])
				]
				level.remove(sst)
				if os.path.exists(sst.filepath):
					os.remove(sst.filepath)

				self.stats.record_compaction(1, compliance=True)
				if kept:
					new_sst = SSTable(kept, self._new_path("data"))
					new_sst.write()
					self.stats.record_write(new_sst.size_bytes, compliance=True)
					level.append(new_sst)
			if data_level_idx > 0:
				level.sort(key=lambda s: s.min_key)

		survivors = [
			rec for rec in sorted(expired_sst.records, key=lambda r: r.key)
			if self._tombstone_still_needed(rec.key, rec.seqnum)
		]
		for _ in range(len(expired_sst.records) - len(survivors)):
			self.stats.record_tombstone_retired()
		self._retire_tombstone_file(level_idx, expired_sst, survivors)

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

		# Record this independently triggered compaction. It is deadline-driven,
		# so it counts as compliance work.
		self.stats.record_compaction(
			1 + len(overlapping_dst),
			compliance=True
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
			self.stats.record_write(new_sst.size_bytes, compliance=True)

			dst_level.append(new_sst)
			dst_level.sort(key=lambda sst: sst.min_key)

		# both levels changed: tombstones left src_level and landed in dst_level
		self._refresh_tombstone_index(level_idx, level_idx + 1)
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

				if self.mode == MODE_FADE:
					self._fade_merge(level_idx, expired)
				else:
					self._purge_expired_tombstones(level_idx, expired)
