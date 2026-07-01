from sst import Record, SSTable, PUT, DELETE

class MemTable:
	def __init__(self, max_size, stats):
		self.max_size = max_size  # flush when entry count hits this
		self.stats = stats
		self.data = {}  # key -> Record, only newest version per key

	def put(self, key, value, seqnum):
		self.data[key] = Record(key, value, seqnum, PUT)

	def delete(self, key, seqnum):
		self.data[key] = Record(key, "", seqnum, DELETE)

	def is_full(self):
		return len(self.data) >= self.max_size

	def flush(self, filepath):
		# sort by key and write to the given filepath
		records = sorted(self.data.values(), key=lambda r: r.key)
		sst = SSTable(records, filepath)
		sst.write()
		self.stats.record_write(sst.size_bytes)
		self.data = {}
		return sst
