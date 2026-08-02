import json
import os

from bloom import BloomFilter, DEFAULT_BITS_PER_ELEMENT

PUT = "PUT"
DELETE = "DELETE"

# Records per block. Real engines size blocks in bytes (typically 4-64KB); a record
# count keeps the arithmetic obvious while giving the index the same shape.
DEFAULT_BLOCK_SIZE = 32


class BlockHandle:
	"""
	Where one block lives in the file and which keys it covers.

	Because records are written in key order, [first_key, last_key] of each block
	are disjoint and ascending, so a binary search over the handles locates the one
	block that could hold a given key.
	"""

	def __init__(self, first_key, start_offset):
		self.first_key = first_key
		self.last_key = first_key
		self.start_offset = start_offset
		self.end_offset = start_offset

	@property
	def num_bytes(self):
		return self.end_offset - self.start_offset

	def __repr__(self):
		return f"Block[{self.first_key}..{self.last_key}]@{self.start_offset}+{self.num_bytes}"

class Record:
	def __init__(self, key, value, seqnum, rec_type):
		self.key = key
		self.value = value  # empty string for DELETE records
		self.seqnum = seqnum
		self.type = rec_type

	def to_dict(self):
		return {"key": self.key, "value": self.value, "seqnum": self.seqnum, "type": self.type}

	@staticmethod
	def from_dict(d):
		return Record(d["key"], d["value"], d["seqnum"], d["type"])

class SSTable:
	def __init__(self, records, filepath, level_entry_seqnum=None,
			bits_per_element=DEFAULT_BITS_PER_ELEMENT,
			block_size=DEFAULT_BLOCK_SIZE):
		# must pass records already sorted by key
		self.records = records
		self.filepath = filepath
		self.min_key = records[0].key
		self.max_key = records[-1].key
		self.size_bytes = 0
		# Logical time when this SST entered its current level
		self.level_entry_seqnum = level_entry_seqnum
		# seqnum of the oldest tombstone in this file, float("inf") if none
		tombstone_seqnums = [r.seqnum for r in records if r.type == DELETE]
		self.oldest_tombstone_time = min(tombstone_seqnums) if tombstone_seqnums else float("inf")
		# Membership filter over this file's keys, built once at creation. A negative
		# answer is definitive, so a lookup can skip the file without reading it.
		# min_key/max_key only bound the range; within that range the filter is what
		# tells us whether a specific key is worth scanning for.
		self.bits_per_element = bits_per_element
		self.bloom = BloomFilter.build([r.key for r in records], bits_per_element)
		# Populated by write(), which is where byte offsets become known.
		self.block_size = block_size
		self.block_index = []

	def may_contain(self, key):
		"""
		Cheap pre-check for the lookup and compaction-targeting paths.
		False means the key is definitely not in this file.
		"""
		if key < self.min_key or key > self.max_key:
			return False
		return self.bloom.contains(key)

	def write(self):
		"""
		Serialize records as one JSON object per line, grouped into fixed-size blocks,
		and record the byte offset where each block starts.

		Line-delimited rather than a single JSON array so a block can be read back by
		seeking to its offset and reading a byte span, without parsing the whole file.
		That is what makes the block index meaningful: a lookup reads one block, not
		the entire SST.
		"""
		self.block_index = []
		with open(self.filepath, "w") as f:
			for i, rec in enumerate(self.records):
				if i % self.block_size == 0:
					# start of a new block: remember where it begins and its first key
					self.block_index.append(BlockHandle(rec.key, f.tell()))
				f.write(json.dumps(rec.to_dict()))
				f.write("\n")
				self.block_index[-1].end_offset = f.tell()
				self.block_index[-1].last_key = rec.key
		self.size_bytes = os.path.getsize(self.filepath)

	def find_block(self, key):
		"""
		Binary search the block index for the one block that could hold key.
		Returns a BlockHandle, or None if no block covers it.
		"""
		lo, hi = 0, len(self.block_index) - 1
		while lo <= hi:
			mid = (lo + hi) // 2
			block = self.block_index[mid]
			if key < block.first_key:
				hi = mid - 1
			elif key > block.last_key:
				lo = mid + 1
			else:
				return block
		return None

	def read_block(self, block):
		"""
		Read exactly one block off disk by seeking to its offset. This is the call
		that a real engine pays I/O for, and the reason the index exists.
		"""
		with open(self.filepath, "r") as f:
			f.seek(block.start_offset)
			raw = f.read(block.num_bytes)
		return [Record.from_dict(json.loads(line)) for line in raw.splitlines() if line]

	@staticmethod
	def load(filepath, bits_per_element=DEFAULT_BITS_PER_ELEMENT,
			block_size=DEFAULT_BLOCK_SIZE):
		records = []
		with open(filepath, "r") as f:
			for line in f:
				line = line.strip()
				if line:
					records.append(Record.from_dict(json.loads(line)))
		# filter and block index are metadata, not file content, so they are rebuilt
		# on load rather than serialized alongside the records
		sst = SSTable(records, filepath, bits_per_element=bits_per_element,
			block_size=block_size)
		sst._rebuild_block_index()
		sst.size_bytes = os.path.getsize(filepath)
		return sst

	def _rebuild_block_index(self):
		"""
		Recompute block offsets from the file without rewriting it. Used by load(),
		which has records but not the offsets write() would have recorded.
		"""
		self.block_index = []
		offset = 0
		with open(self.filepath, "rb") as f:
			for i, line in enumerate(f):
				if i % self.block_size == 0:
					self.block_index.append(BlockHandle(self.records[i].key, offset))
				offset += len(line)
				self.block_index[-1].end_offset = offset
				self.block_index[-1].last_key = self.records[i].key

	def overlaps(self, other):
		# check if the key ranges of two SSTables intersect
		return self.min_key <= other.max_key and self.max_key >= other.min_key
