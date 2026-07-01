class Stats:
	def __init__(self):
		self.bytes_written = 0  # all bytes flushed or compacted to disk
		self.bytes_ingested = 0  # raw user put data
		self.compaction_count = 0
		self.files_merged_total = 0
		self.lookup_io_total = 0  # total SST files touched across all gets
		self.lookup_count = 0

	def record_write(self, byte_count):
		self.bytes_written += byte_count

	def record_ingest(self, byte_count):
		self.bytes_ingested += byte_count

	def record_compaction(self, files_merged):
		self.compaction_count += 1
		self.files_merged_total += files_merged

	def record_lookup(self, files_touched):
		self.lookup_io_total += files_touched
		self.lookup_count += 1

	def waf(self):
		if self.bytes_ingested == 0:
			return 0
		return self.bytes_written / self.bytes_ingested

	def avg_lookup_io(self):
		if self.lookup_count == 0:
			return 0
		return self.lookup_io_total / self.lookup_count

	def report(self):
		print(f"bytes ingested: {self.bytes_ingested}")
		print(f"bytes written: {self.bytes_written}")
		print(f"WAF: {self.waf():.2f}")
		print(f"compactions: {self.compaction_count}")
		print(f"files merged: {self.files_merged_total}")
		print(f"avg lookup IO: {self.avg_lookup_io():.2f} files/get")
