import json
import os

PUT = "PUT"
DELETE = "DELETE"

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
	def __init__(self, records, filepath):
		# must pass records already sorted by key
		self.records = records
		self.filepath = filepath
		self.min_key = records[0].key
		self.max_key = records[-1].key
		self.size_bytes = 0
		# seqnum of the oldest tombstone in this file, float("inf") if none
		tombstone_seqnums = [r.seqnum for r in records if r.type == DELETE]
		self.oldest_tombstone_time = min(tombstone_seqnums) if tombstone_seqnums else float("inf")

	def write(self):
		data = [r.to_dict() for r in self.records]
		with open(self.filepath, "w") as f:
			json.dump(data, f)
		self.size_bytes = os.path.getsize(self.filepath)

	@staticmethod
	def load(filepath):
		with open(filepath, "r") as f:
			data = json.load(f)
		records = [Record.from_dict(d) for d in data]
		sst = SSTable(records, filepath)
		sst.size_bytes = os.path.getsize(filepath)
		return sst

	def overlaps(self, other):
		# check if the key ranges of two SSTables intersect
		return self.min_key <= other.max_key and self.max_key >= other.min_key
