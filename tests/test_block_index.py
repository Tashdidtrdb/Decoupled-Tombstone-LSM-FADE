import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sst import Record, SSTable, PUT, DELETE

TMP = "/tmp/test_block_index"
os.makedirs(TMP, exist_ok=True)


def _sst(n_keys, name, block_size=32, step=1):
	records = [Record(k, "v" * 20, k, PUT) for k in range(0, n_keys * step, step)]
	sst = SSTable(records, os.path.join(TMP, name), block_size=block_size)
	sst.write()
	return sst


def test_block_count_and_coverage():
	sst = _sst(200, "cover.json", block_size=32)
	assert len(sst.block_index) == 7, len(sst.block_index)  # ceil(200/32)
	# blocks must tile the file with no gaps and no overlap
	assert sst.block_index[0].start_offset == 0
	for a, b in zip(sst.block_index, sst.block_index[1:]):
		assert a.end_offset == b.start_offset, "gap or overlap between blocks"
		assert a.last_key < b.first_key, "block key ranges overlap"
	assert sst.block_index[-1].end_offset == sst.size_bytes
	print(f"block pass -- {len(sst.block_index)} blocks tile the file exactly")


def test_every_key_locatable():
	sst = _sst(200, "locate.json")
	for k in range(200):
		block = sst.find_block(k)
		assert block is not None, f"no block for key {k}"
		assert any(r.key == k for r in sst.read_block(block)), f"key {k} not in its block"
	print("block pass -- all 200 keys found via find_block + read_block")


def test_absent_key_returns_no_block():
	# gaps between keys: find_block must not claim a block for a missing key
	sst = _sst(100, "sparse.json", step=10)  # keys 0,10,20,...
	assert sst.find_block(5000) is None, "out-of-range key got a block"
	print("block pass -- out-of-range key yields no block")


def test_read_block_reads_only_that_block():
	# the point of the index: one block is a fraction of the file
	sst = _sst(320, "partial.json", block_size=32)
	block = sst.find_block(160)
	assert block.num_bytes < sst.size_bytes / 5, "block is not much smaller than the file"
	records = sst.read_block(block)
	assert len(records) == 32
	assert all(block.first_key <= r.key <= block.last_key for r in records)
	print(f"block pass -- read {block.num_bytes}B of a {sst.size_bytes}B file for one lookup")


def test_load_rebuilds_identical_index():
	sst = _sst(150, "roundtrip.json")
	reloaded = SSTable.load(sst.filepath)
	assert [r.key for r in reloaded.records] == [r.key for r in sst.records]
	assert len(reloaded.block_index) == len(sst.block_index)
	for a, b in zip(sst.block_index, reloaded.block_index):
		assert a.start_offset == b.start_offset and a.end_offset == b.end_offset
		assert a.first_key == b.first_key and a.last_key == b.last_key
	print("block pass -- load() rebuilds byte-identical offsets")


def test_single_and_partial_blocks():
	one = _sst(1, "one.json")
	assert len(one.block_index) == 1
	assert one.read_block(one.find_block(0))[0].key == 0
	partial = _sst(33, "partial2.json", block_size=32)  # 32 + 1 leftover
	assert len(partial.block_index) == 2
	assert len(partial.read_block(partial.block_index[1])) == 1
	print("block pass -- single-record and partial trailing blocks handled")


def test_tombstone_sst_indexed():
	records = [Record(k, "", k, DELETE) for k in range(0, 100, 5)]
	sst = SSTable(records, os.path.join(TMP, "tomb.json"), block_size=8)
	sst.write()
	for r in records:
		assert any(x.key == r.key for x in sst.read_block(sst.find_block(r.key)))
	print("block pass -- tombstone SSTs are block-indexed too")


if __name__ == "__main__":
	test_block_count_and_coverage()
	test_every_key_locatable()
	test_absent_key_returns_no_block()
	test_read_block_reads_only_that_block()
	test_load_rebuilds_identical_index()
	test_single_and_partial_blocks()
	test_tombstone_sst_indexed()
