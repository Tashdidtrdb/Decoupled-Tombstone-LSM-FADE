# Implementation Overview — Decoupled Tombstone LSM
# Status: Phase A + B + C (baseline) + Phase 3 (decoupled design) complete

This document describes the whole codebase as it stands after Phase 3. It supersedes
the midterm `Implementation/OVERVIEW.md`, which described only the baseline engine.

---

## What this project is about

A minimal LSM (Log-Structured Merge-tree) key-value engine built to evaluate three
compaction strategies for meeting a **delete deadline** — a guarantee that a deleted
record is physically erased from disk within D operations of the delete:

1. **Vanilla LSM** — standard leveled compaction; tombstones propagate lazily, no deadline.
2. **FADE** — same, plus eager compaction on tombstone TTL expiry. Selects data files by
   **key-range overlap** and rewrites all of them (the "wide merge").
3. **Decoupled** — tombstones live in their own files with their own compaction schedule.
   On TTL expiry, per-file Bloom filters identify the specific data files that hold an
   invalidated key, and only those are rewritten (the "targeted rewrite").

The headline metric is **compliance WAF**: bytes rewritten purely to honour the deadline,
divided by bytes ingested. Secondary metrics are total WAF, average lookup I/O
(files touched per `get()`), and **deadline violations** (records still on disk past
their deadline — must be zero for any deadline mode).

### Deliberate simplifications (course setting)

- **Language:** Python, not the proposed C++. WAF is a byte count, so language is irrelevant.
- **Clock:** logical op-counter (`seqnum`) instead of wall-clock. Deadline D means "within
  D write ops", not seconds.
- **Concurrency:** single-threaded, synchronous compaction. No background threads or RCU.
- **SST storage:** records stay in memory as Python objects after flush. Files are written
  and bytes counted correctly, but there is no lazy on-disk loading (deferred: task 17).

---

## Directory structure

```
bloom.py             -- Bloom filter (bit array, double hashing, tunable bits/element)
sst.py               -- Record, BlockHandle, SSTable (data model, file I/O, block index)
memtable.py          -- MemTable (in-memory write buffer)
tombstone_index.py   -- per-level Bloom filters over tombstone keys
planner.py           -- targeted purge planner: which files hold an invalidated key
lsm.py               -- LSM engine (dual pipelines, compaction, purge, three modes)
stats.py             -- WAF, compliance, and read-path counters
workload.py          -- original simple workload generator (kept; superseded by ycsb.py)
ycsb.py              -- YCSB-style generator (load phase, mixes, key distributions)
benchmark.py         -- harness: replay one workload across modes, collect results
evaluation.py        -- the four experiments that produce the paper's results
run.py               -- original midterm driver (Vanilla vs FADE)
run_tests.py         -- runs every test file, prints a summary

tests/               -- 21 test files, see "Testing" below
```

**Run the evaluation:** `python3 evaluation.py`
**Run all tests:** `python3 run_tests.py`

---

## Component details

### bloom.py — BloomFilter  *(Phase 3, task 12)*

Classic Bloom filter over a packed `bytearray`. Membership is probabilistic in one
direction only: `contains()` may return True for an absent key (false positive) but
**never False for a key that was added**. That asymmetry is what makes it safe as both a
lookup guard and a purge-targeting mechanism — a negative is definitive.

- **Double hashing** (Kirsch-Mitzenmacher): k bit positions derived from two hashes,
  `g_i(x) = h1(x) + i*h2(x)`, rather than k separate digests.
- **Tunable bits-per-element**, default 10 (~1% FPR). `optimal_num_hashes()` computes
  `k = (m/n) * ln2`. This is the knob Monkey [8] and Mnemosyne [6] are about.

Measured against theory: 4 bits → 0.150 FPR (predicted 0.147), 10 bits → 0.009 (0.008),
16 bits → 0.0004 (0.0005). Zero false negatives across 5000 keys.

### sst.py — Record, BlockHandle, SSTable

`Record` is the atomic unit: `key` (int), `value` (str, empty for deletes), `seqnum`
(global monotonic counter — highest wins for the same key), `type` ("PUT" or "DELETE").

`SSTable` is an immutable sorted file. Constructed from pre-sorted records. Stores
`min_key`, `max_key`, `size_bytes`, and `oldest_tombstone_time`.

**Phase 3 additions:**
- **`bloom`** — a filter over the file's keys, built at construction. `may_contain(key)`
  does the range check first (free), then consults the filter. Rejected 994/1000 absent
  in-range keys in testing.
- **`block_index`** — a list of `BlockHandle`s, each recording the byte offset and key
  range of one block (default 32 records). `find_block()` binary-searches it;
  `read_block()` seeks to that offset and reads only those bytes.
- **File format changed to line-delimited JSON** (one record per line). A single JSON
  array cannot be seeked into. Files came out 1.3% *smaller* and WAF moved <2% uniformly.
- Filters and indexes are **never serialized** — both are rebuilt on `load()`. Writing
  them would inflate `bytes_written` and corrupt WAF.

Block index effect: a lookup reads ~2.4 KB of a 24 KB file, **90.1% fewer bytes** than
whole-file scans.

### memtable.py — MemTable

In-memory write buffer, a plain dict (key → Record), newest version per key only.
`flush(filepath)` sorts by key, writes an SSTable, clears itself. The LSM engine owns
file naming and passes the path in.

### tombstone_index.py — TombstoneIndex  *(Phase 3, task 13)*

One Bloom filter **per level** covering every tombstone key resident there, with
**non-uniform bit allocation**: `[14, 12, 10, 8]` bits from L0 down. Delete density is
highest where tombstones are newest, so shallow levels get a richer budget — this is the
proposal's Section VII commitment. Measured: L0 FPR 0.0012 vs L3 0.0216, an 18× better
rate where probes are most likely.

Filters are **rebuilt** whenever a level's file set changes. Bloom filters cannot retract
individual keys, so rebuilding the affected level is the only correct option.

> **Important correction to the proposal.** Section V says a *positive* tombstone-filter
> match should short-circuit the lookup and return "not found". Implemented literally that
> is a **correctness bug**: a key can be deleted and re-inserted, so a filter hit proves a
> tombstone exists *somewhere*, not that it is the newest record. The sound form is the
> mirror image — a **miss at every level** proves no tombstone exists, so the lookup skips
> the whole tombstone hierarchy with zero I/O. A hit means the files must still be searched
> and the seqnum comparison decides. Locked in by
> `tests/test_tombstone_lookup.py::test_reinserted_key_is_visible`.

### planner.py — plan_purge  *(Phase 3, task 14)*

Decides *which* data files must be rewritten to erase a set of tombstoned keys. Pure
policy, no I/O.

FADE selects by key-range overlap, dragging in every file whose range spans the tombstone —
most holding none of the deleted keys. `plan_purge()` probes each key against per-file
Bloom filters, selecting a file only when it plausibly holds a key being erased.
`scope_if_range_based()` computes what FADE *would* have rewritten, so the comparison is
measured rather than asserted.

**Safety direction:** a false positive costs a wasted rewrite; a false negative would leave
deleted data on disk. Filters never report absent for a key they hold, so no file needing
scrubbing is skipped.

Measured: **one key selects 1.1 of 32 data files** on average — the proposal's "single,
specific data SST file" claim, quantified.

### lsm.py — LSM engine

Owns both memtables, both SST hierarchies, the seqnum counter, and all schedulers.

**Dual pipelines.** `put()` → `data_memtable` → `data_levels`. `delete()` →
`tombstone_memtable` → `tombstone_levels`. Two independent hierarchies, two schedules.

**Key constructor parameters:**
- `memtable_size` — **total** buffer budget, split across both memtables via
  `tombstone_memtable_share` (default 0.5). Giving each the full budget would hand the
  decoupled engine 2× vanilla's memory and make WAF incomparable.
- `deadline` — D in ops. `None` means vanilla.
- `mode` — `"vanilla"`, `"fade"`, or `"decoupled"`. Defaults from `deadline` for backwards
  compatibility.
- `target_file_records` — max records per compaction output file (default 50).
  `None` restores single-file output.
- `tombstone_filter_bits` — per-level bits-per-element for the tombstone index.

**`get()` search order:**
1. Both memtables (zero files touched).
2. Consult `tombstone_index` **once**. A miss at every level skips the entire tombstone
   hierarchy. A hit adds it to the search.
3. For each candidate SST: range check → Bloom filter → `find_block()` → `read_block()`.
4. Highest seqnum across everything wins. DELETE → `None`, PUT → value.

Unlike the midterm engine, this cannot stop at the first match: two independent pipelines
mean the newest record could be in either.

**`_compact(level_idx, src_file=None)`** — capacity-driven data compaction.
- Merges `src_file`, its overlapping L0 siblings, and overlapping destination files.
- Sorts by (key asc, seqnum desc), dedupes by highest seqnum, drops tombstones only at
  the bottom level.
- **Splits output into key-disjoint files** of `target_file_records` each, rather than one
  file spanning everything. Without this, one output file covers the union of all input
  ranges and every later targeted rewrite degenerates into a full rewrite.
- Destination files are selected against the **full merged key range**, not `src_file`
  alone — L0 siblings widen that range, and a destination file inside the widened span
  but outside `src_file` would otherwise survive while the output covers its keys, leaving
  two files at the same level claiming the same key.

**`_purge_expired_tombstones()`** — the decoupled compliance path.
- Plans **per key** (narrow targeting) but groups rewrites **per file** (each file touched
  once). Planning granularity controls blast radius; rewrite grouping controls how often
  you pay for it. Purging per key rewrote a 20-key file 20 times and blew WAF up 8×.
- `_scrub_file()` drops records older than their tombstone and rewrites what remains.
- Retires a tombstone once no record for its key remains anywhere — **including the
  memtable**, since a purge only scrubs disk and an unflushed record would otherwise reach
  disk with nothing left to shadow it.

**`_fade_merge()`** — the FADE control. Selects data files by key-range overlap and
rewrites all of them. Erases the same records as the targeted path, so both modes meet the
same deadline and only cost differs.

**TTL scheduling.** `per_level_ttl = D / num_levels`, measured from when a file entered its
current level. Crossing all L levels costs at most D ops, making D an end-to-end guarantee.
`level_ttl` is the cumulative view of the same policy, reported for analysis only.

**Tombstone memtable staleness.** The TTL clock starts when a tombstone reaches *disk*, but
the deadline starts at `delete()`. A tombstone sat unflushed for 644 ops against a 500-op
deadline — invisible to the scheduler. `_tombstone_memtable_is_stale()` forces a flush once
the oldest buffered tombstone has waited `per_level_ttl`, checked from both `put()` and
`delete()` since puts advance the clock too.

### stats.py — Stats

Counters passed by reference.

- **Write amplification:** `bytes_ingested` (puts charge the value payload; deletes charge
  `TOMBSTONE_INGEST_BYTES = 8` — deletes are user work and must appear in the denominator),
  `bytes_written`, `waf()`.
- **Compliance:** `compliance_bytes_written`, `compliance_compaction_count`,
  `compliance_waf()`. Separates deadline-driven work from capacity-driven work — the
  headline metric.
- **Read path:** `filter_skips`, `filter_false_positives`, `block_reads`,
  `block_bytes_read`, `tombstone_hierarchy_skips`, `observed_fpr()`.
- **Purge:** `purge_plans`, `purge_files_probed`, `purge_filter_rejections`,
  `tombstones_retired`.

### ycsb.py — YCSB-style generator  *(Phase 3, task 15)*

Load phase (inserts every key once) then a run phase mixing operations by a named
workload. Mixes: YCSB **A/B/C/D** plus delete-aware **X** (25% deletes, the project
default), **X-light** (5%), **X-heavy** (40%). Distributions: `uniform`, `zipfian`,
`latest`.

> **Flaw fixed from the midterm generator.** `workload.py` used `numpy.zipf()`, which is
> **unbounded**, so bounding it to the key space meant clamping — piling the entire tail
> onto the last key. At `zipf_param=1.2` that single key absorbed **22% of all
> operations**: an artefact, not skew, and every midterm skew result was affected.
> `ZipfianGenerator` computes the bounded distribution directly (standard YCSB
> formulation), so key 0 gets 13.3% decaying smoothly and the last key drops to **0.01%**.

### benchmark.py — harness  *(Phase 3, task 15)*

`run_engine()` replays ops against one configuration; `compare_modes()` runs all three.
Returns `Result` objects with WAF, compliance metrics, timing, file counts, and:

- **`unerased`** — deleted records still on disk. A *progress* indicator: some are
  legitimately pending because their deadline has not arrived.
- **`violations`** — deleted records whose deadline **has** passed. The compliance verdict;
  must be zero.

> **Why violations, not unerased.** FADE's wide merge incidentally erases records whose
> deadlines have not yet arrived, so it always shows a lower `unerased` count — but both
> modes retired *exactly* 241 tombstones in testing. Over-erasing is not better compliance.
> Comparing raw counts would have understated the decoupled design.

`verify=True` checks every key against a reference dict.

### evaluation.py — the four experiments  *(Phase 3, task 16)*

1. **Headline** — Vanilla vs FADE vs Decoupled at a fixed configuration.
2. **Delete ratio** — how compliance cost scales with delete volume (5% / 25% / 40%).
3. **Key distribution** — where the wide merge hurts most (zipfian / latest / uniform).
4. **Output file size** — the targeting vs file-count trade-off curve.

---

## Results

**Experiment 1** (9000 ops, 1000 keys, zipfian, 25% deletes, D=500):

| config | WAF | compliance WAF | compl. compactions | bytes/event | violations | lookup I/O | files |
|---|---|---|---|---|---|---|---|
| Vanilla | 5.55 | 0.00 | 0 | 0 | — | 4.24 | 36 |
| FADE | 54.61 | 49.92 | 1534 | 3532 | 0 | 7.02 | 65 |
| **Decoupled** | **23.82** | **18.92** | **603** | **3405** | **0** | 7.03 | 68 |

**62% less compliance data, 61% fewer compliance compactions, zero deadline violations.**

**Experiment 2 — delete ratio:** saving 72% / 62% / 59% at 5% / 25% / 40% deletes.

**Experiment 3 — key distribution:**

| distribution | vanilla WAF | FADE compl. WAF | decoupled compl. WAF | saving |
|---|---|---|---|---|
| zipfian | 5.55 | 49.92 | 18.92 | 62% |
| latest | 5.92 | 50.99 | 19.36 | 62% |
| uniform | 9.44 | 72.23 | 24.91 | **66%** |

FADE degrades 49.9 → 72.2 under uniform keys while decoupled only goes 18.9 → 24.9. This is
the motivating argument, measured: range-based selection is worst when deletes spread
across the key space.

**Experiment 4 — file size trade-off** (uniform keys, decoupled):

| records/file | compliance WAF | bytes/event | files on disk |
|---|---|---|---|
| unsplit | 28.19 | 8499 | 51 |
| 200 | 27.70 | 6241 | 54 |
| 100 | 27.12 | 4879 | 57 |
| 50 | 24.91 | 3346 | 64 |
| 25 | 20.51 | 2213 | 78 |

**An honest cost.** Honouring a deadline nearly doubles the file count (36 → 68) and raises
lookup I/O from 4.24 to 7.03 files per get. Decoupled and FADE are effectively tied here
(7.03 vs 7.02), so this is the price of compliance generally, not of the decoupled design.
The proposal's Section VII predicted read-path overhead as the main risk; this is it,
quantified.

---

## Key correctness decisions

**Tombstone drop rule.** In capacity compaction, tombstones are dropped only at the bottom
level. Dropping earlier would let a deeper data record become visible again.

**L0 within-level overlap.** L0 is the only level where files can share keys. Compacting
any file out of L0 pulls in overlapping L0 siblings, or a tombstone could move to L1 while
its PUT stays behind in another L0 file.

**Destination selection against the merged range.** See `_compact()` above — a pre-existing
bug that splitting output made visible.

**L1+ files never overlap.** `get()` assumes at most one file per level below L0 can hold a
key. Verified across 54 configurations.

**Plan per key, rewrite per file.** The single most important tuning decision in Phase 3.

**Retirement must consider the memtable.** See `_purge_expired_tombstones()` above.

---

## Testing

21 test files, **23 test modules pass, 0 fail** (`python3 run_tests.py`).

Baseline (midterm): `test_a1`–`test_a4` (SSTable, MemTable, get, compaction), `test_b1`,
`test_b3` (TTL, FADE trigger), `test_c1` (workload generator).

Phase 3: `test_bloom`, `test_sst_bloom`, `test_block_index`, `test_read_path` (task 12);
`test_tombstone_index`, `test_tombstone_lookup` (task 13); `test_compaction_split`
(partitioning); `test_planner`, `test_purge`, `test_modes` (task 14); `test_ycsb`,
`test_benchmark` (task 15); `test_evaluation` (task 16); `test_engine_sweep` (integration).

**`test_engine_sweep.py`** is the broad guard: 54 configurations (delete ratio × skew × L0
capacity × deadline), each replayed against a reference dict, plus invariants that all
three modes expose identical visible state and L1+ files stay key-disjoint. Most
regressions during development surfaced here first.

**`test_evaluation.py`** asserts the *shape* of each published conclusion rather than exact
numbers, so it fails if a change inverts a claim in the paper.

---

## Not implemented

- **Task 17 — lazy SST record loading.** Records stay in memory after flush. The block
  index already computes real byte offsets and `read_block()` seeks correctly, so the
  machinery is in place; what remains is dropping `self.records` after `write()` and
  loading on demand.
- **Concurrency.** Single-threaded throughout. The proposal's RCU-style snapshot isolation
  is not modelled.
- **Prefix-key compression** on tombstone files (proposal Section VII mitigation).
