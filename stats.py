class Stats:
	def __init__(self):
		self.bytes_written = 0  # all bytes flushed or compacted to disk
		self.bytes_ingested = 0  # raw user put/delete payload
		self.compaction_count = 0
		self.files_merged_total = 0
		self.lookup_io_total = 0  # total SST files touched across all gets
		self.lookup_count = 0
		# deadline-driven (compliance) work only, split out from capacity-driven work.
		# This is the headline metric for the decoupled design: how many bytes must be
		# rewritten purely to honour a delete deadline.
		self.compliance_bytes_written = 0
		self.compliance_compaction_count = 0
		self.compliance_files_merged = 0
		# read-path effectiveness of the per-SST Bloom filters and block indexes
		self.filter_skips = 0  # files ruled out with zero I/O
		self.filter_false_positives = 0  # filter said maybe, block did not hold the key
		self.block_reads = 0  # blocks actually read off disk
		self.block_bytes_read = 0  # bytes read, vs whole-file scans before

	def record_write(self, byte_count, compliance=False):
		self.bytes_written += byte_count
		if compliance:
			self.compliance_bytes_written += byte_count

	def record_ingest(self, byte_count):
		self.bytes_ingested += byte_count

	def record_compaction(self, files_merged, compliance=False):
		self.compaction_count += 1
		self.files_merged_total += files_merged
		if compliance:
			self.compliance_compaction_count += 1
			self.compliance_files_merged += files_merged

	def record_lookup(self, files_touched):
		self.lookup_io_total += files_touched
		self.lookup_count += 1

	def record_filter_skip(self):
		self.filter_skips += 1

	def record_false_positive(self):
		self.filter_false_positives += 1

	def record_block_read(self, byte_count):
		self.block_reads += 1
		self.block_bytes_read += byte_count

	def observed_fpr(self):
		"""
		Measured false positive rate on the read path: of the files the filter let
		through, how many turned out not to hold the key.

		Every probe increments block_reads (a block is read either way), and a probe
		that finds nothing additionally increments filter_false_positives, so
		block_reads is already the total number of probes.
		"""
		if self.block_reads == 0:
			return 0
		return self.filter_false_positives / self.block_reads

	def waf(self):
		if self.bytes_ingested == 0:
			return 0
		return self.bytes_written / self.bytes_ingested

	def compliance_waf(self):
		# write amplification attributable solely to meeting the delete deadline
		if self.bytes_ingested == 0:
			return 0
		return self.compliance_bytes_written / self.bytes_ingested

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
		print(f"compliance bytes written: {self.compliance_bytes_written}")
		print(f"compliance WAF: {self.compliance_waf():.2f}")
		print(f"compliance compactions: {self.compliance_compaction_count}")
		print(f"avg lookup IO: {self.avg_lookup_io():.2f} files/get")
		print(f"filter skips (zero-IO): {self.filter_skips}")
		print(f"filter false positives: {self.filter_false_positives} ({self.observed_fpr():.3f})")
		print(f"block reads: {self.block_reads} ({self.block_bytes_read} bytes)")
