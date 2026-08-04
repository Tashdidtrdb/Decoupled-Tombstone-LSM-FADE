# Decoupled Tombstone Storage for Efficient Delete Persistence in LSM-Trees

COMP 8157 — Advanced Database Topics, University of Windsor
Mirza Zaimur Rahman · Mohammed Adil · Rabindra Pangeni · Varun Kumar Cheemalapati

---

## The problem

LSM-tree engines delete by writing a *tombstone* — a marker that shadows the old record.
The record itself stays on disk until some later compaction happens to rewrite the file
containing it, which can take hours or days. `get()` correctly reports the key as gone
while the data is still readable from the file, which is a problem when a regulation such
as GDPR requires erasure within a fixed deadline.

FADE (Lethe, SIGMOD 2020) fixes the timing by compacting eagerly when a tombstone nears its
deadline — but it selects data files by **key-range overlap** and rewrites all of them,
most of which contain none of the deleted keys.

**This project** stores tombstones in their own files with their own compaction schedule,
and uses per-file Bloom filters to rewrite only the files that actually hold an invalidated
record. Same deadline, far less write amplification.

**Result: 62% less compliance data written than FADE, both meeting the deadline on every
record. Lazy deletion, by comparison, leaves 225 records overdue.**

---

## Requirements

- Python 3.8+
- `numpy` (used only by the original midterm workload generator)

```bash
pip install numpy
```

No other dependencies.

---

## Running it

### 1. Tests

```bash
python3 run_tests.py
```

Expect `24 passed, 0 failed`. Includes a 54-configuration correctness sweep that replays
whole workloads against a reference dictionary.

### 2. The evaluation (produces the report's numbers)

```bash
python3 evaluation.py
```

Roughly two minutes. Four experiments: the three-way headline comparison, delete-ratio
scaling, key-distribution sensitivity, and the output-file-size trade-off curve.

### 3. The interactive shell

```bash
python3 shell.py
```

Type `help` for commands. See the demo below.

### 4. The scripted demo

```bash
python3 shell.py --script demo.txt
```

---

## Demo: data that is deleted but still on disk

The point of this demo is that **every mode answers `get()` the same way**. Vanilla is not
broken — it hides the record behind a tombstone, which is correct. The difference is
whether the record is still physically present, so the shell inspects the files directly.

Start the shell, then:

```
mode vanilla --force
put 1 alice
put 2 bob
put 3 carol
put 4 dave
delete 2
get 2          # -> (not found)   the API says it is gone
leaked         # -> key 2 'bob'   but it is still in a file on disk
tick 5
leaked         # -> still there   and it stays there
```

Now the same commands under the decoupled design:

```
mode decoupled --force
put 1 alice
put 2 bob
put 3 carol
put 4 dave
delete 2
get 2          # -> (not found)   same answer from the API
leaked         # -> key 2 'bob'   still present: the deadline has not passed yet
tick 5
leaked         # -> nothing       deadline passed, physically erased
find 2         # -> no records anywhere
stats          # what that cost
```

`leaked` lists keys the user deleted whose data is still readable on disk. `tick` advances
the clock (see the note below). `find` shows every physical copy of a key across both
hierarchies.

---

## An important note on the clock

**The deadline is measured in write operations, not seconds.** `--deadline 8` means "within
8 writes", and no time passes while the store is idle. This is a deliberate simplification
carried over from the proposal: the headline metric is a byte count, so a logical clock
keeps benchmark runs reproducible in a way wall-clock timing would not.

The `tick` command exists because of this — it issues throwaway writes (on keys from 900000
up, well clear of any demo data) so you can advance the clock on demand.

A wall-clock version is scoped in `CHECKPOINT.md` as future work. It would also require
either a background thread or an explicit `maintain()` call to drive expiry during idle
periods, which the deliberately single-threaded design avoids.

---

## Shell reference

| command | what it does |
|---|---|
| `put <key> <value>` | insert or update (keys are integers) |
| `get <key>` | look up — identical answer in every mode |
| `delete <key>` | write a tombstone |
| `flush` | force memtables to disk |
| `tick [n]` | advance the clock by n writes (default 5) |
| `leaked` | deleted keys whose data is **still on disk** |
| `find <key>` | every physical record for a key |
| `files` | all SST files with key ranges and sizes |
| `dump <file>` | raw contents of one SST |
| `stats` | write amplification and compliance counters |
| `mode [name] [--force]` | `vanilla`, `fade`, `decoupled` — wipes the store |
| `config` | current parameters |
| `reset` | empty the store, keep the mode |
| `replay <file>` | run commands from a file |
| `help` / `quit` | |

Launch flags: `--mode`, `--deadline`, `--memtable-size`, `--levels`, `--l0-capacity`,
`--target-file-records`, `--data-dir`, `--script`.

Shell defaults are deliberately tiny (memtable of 2, deadline of 8) so a handful of typed
commands triggers real flushes, compactions and purges. The evaluation uses realistic
sizes.

---

## The three modes

| mode | deadline | how it erases |
|---|---|---|
| `vanilla` | none | never, except incidentally during capacity compaction |
| `fade` | yes | rewrites **every data file whose key range overlaps** the expiring tombstone |
| `decoupled` | yes | rewrites **only the files that actually hold** an invalidated record |

All three return identical results from `get()`. They differ in cost and in what remains
on disk.

---

## Results summary

9000 operations, 1000 keys, Zipfian, 25% deletes, deadline 500:

| config | WAF | compliance WAF | compliance compactions | deadline violations |
|---|---|---|---|---|
| Vanilla | 5.55 | 0.00 | 0 | **225** |
| FADE | 54.61 | 49.92 | 1534 | 0 |
| **Decoupled** | **23.82** | **18.92** | **603** | **0** |

All three are scored against the same D=500. Vanilla writes nothing to meet the deadline
and misses it on 225 records; both deadline modes miss none. That is the trade the project
is about — the two right-hand columns cannot be read separately, since a design that is
cheap because it skips erasures is not cheaper, it is non-compliant.

The saving holds across delete ratios (72% / 62% / 59% at 5% / 25% / 40%) and is largest
under uniform keys (66%), which is the worst case for range-based file selection.
Violations scale with delete volume as expected: 42 / 225 / 323 records overdue at
5% / 25% / 40% deletes.

Full numbers, including the honest read-path cost, are in `OVERVIEW.md` and reproducible
with `python3 evaluation.py`.

---

## Code map

| file | purpose |
|---|---|
| `lsm.py` | the engine: dual pipelines, compaction, targeted purge, three modes |
| `sst.py` | records, SSTables, Bloom filters, block indexes |
| `bloom.py` | Bloom filter with tunable bits-per-element |
| `tombstone_index.py` | per-level Bloom filters over tombstone keys |
| `planner.py` | decides which data files hold an invalidated key |
| `stats.py` | write amplification and compliance counters |
| `ycsb.py` | YCSB-style workload generator |
| `benchmark.py` | replay a workload across modes |
| `evaluation.py` | the four experiments |
| `shell.py` | interactive demo shell |
| `tests/` | 24 test modules |

`OVERVIEW.md` describes the architecture in detail. `CHECKPOINT.md` records project status,
design decisions, and the findings that shaped the implementation.
