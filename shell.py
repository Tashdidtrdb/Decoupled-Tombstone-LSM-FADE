"""
Interactive shell for the decoupled tombstone LSM engine.

Exists to make a physical difference visible. Every compliance mode answers
get() on a deleted key with "not found" -- vanilla is not wrong, it hides the
record behind a tombstone. The difference is whether the record is still on
disk, so this shell exposes the files, not just the API.

Run:  python3 shell.py
Help: type `help` at the prompt
"""

import argparse
import os
import shutil
import sys
import traceback

from lsm import LSM, MODE_DECOUPLED, MODE_FADE, MODE_VANILLA
from sst import DELETE, PUT
from stats import Stats

MODES = (MODE_VANILLA, MODE_FADE, MODE_DECOUPLED)

# Keys used by `tick` to advance the logical clock. Far outside any key a demo
# would use, so they can never be confused with real data.
TICK_KEY_BASE = 900000

# Demo-scale defaults: small enough that a handful of typed commands triggers
# real flushes, compactions and purges. Not the engine's production defaults.
DEFAULTS = {
	"memtable_size": 2,
	"num_levels": 3,
	"l0_capacity_bytes": 400,
	"target_file_records": 4,
	"deadline": 8,
}


class Shell:
	def __init__(self, data_dir, mode, **engine_kw):
		self.data_dir = data_dir
		self.engine_kw = engine_kw
		self.mode = mode
		self.tick_counter = 0
		# keys whose most recent user operation was a delete; `leaked` checks
		# these against what is physically on disk
		self.deleted_keys = set()
		self.db = None
		self._build_engine()

	# ------------------------------------------------------------------ engine

	def _build_engine(self):
		shutil.rmtree(self.data_dir, ignore_errors=True)
		self.stats = Stats()
		deadline = self.engine_kw["deadline"] if self.mode != MODE_VANILLA else None
		self.db = LSM(
			self.data_dir,
			memtable_size=self.engine_kw["memtable_size"],
			num_levels=self.engine_kw["num_levels"],
			l0_capacity_bytes=self.engine_kw["l0_capacity_bytes"],
			target_file_records=self.engine_kw["target_file_records"],
			stats=self.stats,
			deadline=deadline,
			mode=self.mode,
		)
		self.deleted_keys = set()
		self.tick_counter = 0

	def _is_empty(self):
		if self.db.data_memtable.data or self.db.tombstone_memtable.data:
			return False
		for levels in (self.db.data_levels, self.db.tombstone_levels):
			if any(level for level in levels):
				return False
		return True

	# ---------------------------------------------------------------- helpers

	def _all_ssts(self):
		"""(hierarchy, level_index, sst) for every file on disk."""
		for name, levels in (("data", self.db.data_levels),
				("tombstone", self.db.tombstone_levels)):
			for level_idx, level in enumerate(levels):
				for sst in level:
					yield name, level_idx, sst

	def _short(self, path):
		return os.path.basename(path)

	# --------------------------------------------------------------- commands

	def cmd_put(self, args):
		if len(args) < 2:
			return self._err("usage: put <key> <value>")
		key = self._as_key(args[0])
		if key is None:
			return
		value = " ".join(args[1:])
		self.db.put(key, value)
		self.deleted_keys.discard(key)
		print(f"  put {key} = {value!r}   (seqnum {self.db.seqnum})")

	def cmd_get(self, args):
		if len(args) != 1:
			return self._err("usage: get <key>")
		key = self._as_key(args[0])
		if key is None:
			return
		value = self.db.get(key)
		if value is None:
			print(f"  get {key} -> (not found)")
		else:
			print(f"  get {key} -> {value!r}")

	def cmd_delete(self, args):
		if len(args) != 1:
			return self._err("usage: delete <key>")
		key = self._as_key(args[0])
		if key is None:
			return
		self.db.delete(key)
		self.deleted_keys.add(key)
		print(f"  delete {key}   (seqnum {self.db.seqnum})")

	def cmd_flush(self, args):
		self.db.flush()
		print("  flushed both memtables to disk")

	def cmd_tick(self, args):
		count = 5
		if args:
			try:
				count = int(args[0])
			except ValueError:
				return self._err("usage: tick [n]")
		if count < 1:
			return self._err("tick count must be positive")
		start = TICK_KEY_BASE + self.tick_counter
		for i in range(count):
			self.db.put(start + i, "tick")
		self.tick_counter += count
		print(f"  advanced the clock by {count} writes "
			f"(keys {start}..{start + count - 1}, outside the demo key range)")
		print(f"  logical clock is now at seqnum {self.db.seqnum}")

	def cmd_leaked(self, args):
		"""
		Deleted keys whose PUT records are still physically on disk.

		This is the demo's central claim. A key counts as leaked only if the user
		deleted it and never re-inserted it, so a resurrected key is never
		reported.
		"""
		findings = []
		for hierarchy, level_idx, sst in self._all_ssts():
			if hierarchy != "data":
				continue
			for rec in sst.records:
				if rec.type == PUT and rec.key in self.deleted_keys:
					findings.append((rec.key, rec.value, level_idx, sst.filepath))

		if not self.deleted_keys:
			print("  no keys have been deleted yet")
			return
		if not findings:
			print(f"  no leaked records: all {len(self.deleted_keys)} deleted "
				f"key(s) are physically erased from disk")
			return

		print(f"  {len(findings)} LEAKED record(s) -- deleted, but still on disk:")
		rows = [[str(k), repr(v), f"L{lvl}", self._short(p)] for k, v, lvl, p in findings]
		self._table(rows, ["key", "value", "level", "file"])
		print("  get() reports these as deleted, but the data is still readable")
		print("  from the file on disk.")

	def cmd_find(self, args):
		"""Every physical record for one key, across both hierarchies."""
		if len(args) != 1:
			return self._err("usage: find <key>")
		key = self._as_key(args[0])
		if key is None:
			return

		rows = []
		for name, memtable in (("data", self.db.data_memtable),
				("tombstone", self.db.tombstone_memtable)):
			rec = memtable.data.get(key)
			if rec is not None:
				rows.append([name, "memtable", "-", rec.type, str(rec.seqnum)])

		for hierarchy, level_idx, sst in self._all_ssts():
			if not (sst.min_key <= key <= sst.max_key):
				continue
			for rec in sst.records:
				if rec.key == key:
					rows.append([hierarchy, self._short(sst.filepath),
						f"L{level_idx}", rec.type, str(rec.seqnum)])

		if not rows:
			print(f"  key {key}: no records anywhere (memtables or disk)")
			return
		print(f"  key {key}: {len(rows)} physical record(s)")
		self._table(rows, ["hierarchy", "location", "level", "type", "seqnum"])
		value = self.db.get(key)
		print(f"  get({key}) -> {'(not found)' if value is None else repr(value)} "
			f"(highest seqnum wins)")

	def cmd_files(self, args):
		rows = []
		for hierarchy, level_idx, sst in self._all_ssts():
			rows.append([
				hierarchy,
				f"L{level_idx}",
				self._short(sst.filepath),
				f"{sst.min_key}..{sst.max_key}",
				str(len(sst.records)),
				str(sst.size_bytes),
			])
		if not rows:
			print("  no files on disk (try `flush`)")
			return
		self._table(rows, ["hierarchy", "level", "file", "key range", "recs", "bytes"])
		buffered = len(self.db.data_memtable.data), len(self.db.tombstone_memtable.data)
		print(f"  memtables: {buffered[0]} data, {buffered[1]} tombstone (not yet on disk)")

	def cmd_dump(self, args):
		if not args:
			return self._err("usage: dump <file>")
		# rejoin for the same reason as replay: paths may contain spaces
		wanted = " ".join(args)
		for _, _, sst in self._all_ssts():
			if self._short(sst.filepath) == wanted or sst.filepath == wanted:
				rows = [[str(r.key), r.type, repr(r.value), str(r.seqnum)]
					for r in sst.records]
				self._table(rows, ["key", "type", "value", "seqnum"])
				return
		self._err(f"no such file: {wanted}   (try `files`)")

	def cmd_stats(self, args):
		s = self.stats
		print(f"  mode                  : {self.mode}")
		print(f"  logical clock (seqnum): {self.db.seqnum}")
		print(f"  bytes ingested        : {s.bytes_ingested}")
		print(f"  bytes written         : {s.bytes_written}")
		print(f"  write amplification   : {s.waf():.2f}")
		print(f"  compliance bytes      : {s.compliance_bytes_written}")
		print(f"  compliance WAF        : {s.compliance_waf():.2f}")
		print(f"  compliance compactions: {s.compliance_compaction_count}")
		print(f"  tombstones retired    : {s.tombstones_retired}")
		if s.compliance_compaction_count and not s.compliance_bytes_written:
			print("  (0 compliance bytes: the scrubbed file held only deleted records,")
			print("   so it was removed rather than rewritten -- the cheapest outcome)")

	def cmd_mode(self, args):
		if not args:
			print(f"  current mode: {self.mode}")
			print(f"  available   : {', '.join(MODES)}")
			return
		requested = args[0].lower()
		if requested not in MODES:
			return self._err(f"unknown mode {requested!r}; expected one of {', '.join(MODES)}")
		forced = "--force" in args

		if requested == self.mode and not forced:
			print(f"  already in {self.mode} mode (use `mode {requested} --force` to reset)")
			return
		if not self._is_empty() and not forced:
			answer = input("  switching mode wipes the store. continue? [y/N] ").strip().lower()
			if answer not in ("y", "yes"):
				print("  cancelled")
				return

		self.mode = requested
		self._build_engine()
		print(f"  mode is now {self.mode}; the store is empty -- re-enter your data")

	def cmd_config(self, args):
		print(f"  mode                : {self.mode}")
		print(f"  data dir            : {self.data_dir}")
		print(f"  memtable size       : {self.engine_kw['memtable_size']} entries "
			f"(split across data and tombstone buffers)")
		print(f"  levels              : {self.engine_kw['num_levels']}")
		print(f"  L0 capacity         : {self.engine_kw['l0_capacity_bytes']} bytes")
		print(f"  records per SST     : {self.engine_kw['target_file_records']}")
		deadline = self.engine_kw["deadline"] if self.mode != MODE_VANILLA else None
		print(f"  delete deadline     : {deadline if deadline else '(none -- vanilla)'}")
		print("  NOTE: the deadline is measured in write operations, not seconds.")
		print("        Time only advances when you write; `tick` does that for you.")

	def cmd_reset(self, args):
		self._build_engine()
		print(f"  reset: empty store, still in {self.mode} mode")

	def cmd_replay(self, args):
		if not args:
			return self._err("usage: replay <file>")
		# rejoin: paths may contain spaces, and the dispatcher splits on them
		path = " ".join(args)
		if not os.path.exists(path):
			return self._err(f"no such file: {path}")
		with open(path) as handle:
			for raw in handle:
				line = raw.split("#", 1)[0].strip()
				if not line:
					continue
				print(f"\n> {line}")
				self.execute(line)

	def cmd_help(self, args):
		print("""
  data
    put <key> <value>      insert or update
    get <key>              look up (same answer in every mode)
    delete <key>           write a tombstone
    flush                  force memtables to disk
    tick [n]               advance the logical clock by n writes (default 5)

  inspection
    leaked                 deleted keys whose data is STILL on disk
    find <key>             every physical record for a key
    files                  all SST files with key ranges
    dump <file>            raw contents of one SST
    stats                  write amplification and compliance counters

  control
    mode [name] [--force]  vanilla | fade | decoupled  (wipes the store)
    config                 current parameters
    reset                  empty the store, keep the mode
    replay <file>          run commands from a file
    help                   this text
    quit                   exit
""")

	# ---------------------------------------------------------------- plumbing

	COMMANDS = {
		"put": "cmd_put", "get": "cmd_get", "delete": "cmd_delete",
		"del": "cmd_delete", "flush": "cmd_flush", "tick": "cmd_tick",
		"leaked": "cmd_leaked", "find": "cmd_find", "files": "cmd_files",
		"dump": "cmd_dump", "stats": "cmd_stats", "mode": "cmd_mode",
		"config": "cmd_config", "reset": "cmd_reset", "replay": "cmd_replay",
		"help": "cmd_help", "?": "cmd_help",
	}

	def execute(self, line):
		parts = line.split()
		if not parts:
			return True
		name = parts[0].lower()
		if name in ("quit", "exit"):
			return False
		handler = self.COMMANDS.get(name)
		if handler is None:
			self._err(f"unknown command {name!r} -- type `help`")
			return True
		try:
			getattr(self, handler)(parts[1:])
		except Exception:
			# never traceback during a presentation
			print("  error running that command:")
			print("   ", traceback.format_exc().strip().split("\n")[-1])
		return True

	def _as_key(self, text):
		try:
			return int(text)
		except ValueError:
			self._err(f"key must be an integer, got {text!r}")
			return None

	def _err(self, message):
		print(f"  ! {message}")

	def _table(self, rows, headers):
		widths = [max(len(str(r[i])) for r in [headers] + rows)
			for i in range(len(headers))]
		print("    " + "  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
		print("    " + "  ".join("-" * w for w in widths))
		for row in rows:
			print("    " + "  ".join(str(row[i]).ljust(widths[i]) for i in range(len(row))))


def main(argv=None):
	parser = argparse.ArgumentParser(description="Interactive shell for the LSM engine")
	parser.add_argument("--mode", default=MODE_VANILLA, choices=MODES)
	parser.add_argument("--deadline", type=int, default=DEFAULTS["deadline"],
		help="delete deadline in WRITE OPERATIONS, not seconds")
	parser.add_argument("--memtable-size", type=int, default=DEFAULTS["memtable_size"])
	parser.add_argument("--levels", type=int, default=DEFAULTS["num_levels"])
	parser.add_argument("--l0-capacity", type=int, default=DEFAULTS["l0_capacity_bytes"])
	parser.add_argument("--target-file-records", type=int,
		default=DEFAULTS["target_file_records"])
	parser.add_argument("--data-dir", default="/tmp/lsm_shell")
	parser.add_argument("--script", help="run a command file, then exit")
	args = parser.parse_args(argv)

	shell = Shell(
		args.data_dir,
		args.mode,
		memtable_size=args.memtable_size,
		num_levels=args.levels,
		l0_capacity_bytes=args.l0_capacity,
		target_file_records=args.target_file_records,
		deadline=args.deadline,
	)

	if args.script:
		shell.cmd_replay([args.script])
		return 0

	print("Decoupled Tombstone LSM -- interactive shell")
	print(f"mode: {shell.mode}   (type `help` for commands, `quit` to exit)")
	print("note: the deadline counts WRITE OPERATIONS, not seconds -- use `tick`")
	print()

	while True:
		try:
			line = input("> ")
		except (EOFError, KeyboardInterrupt):
			print()
			break
		if not shell.execute(line):
			break
	return 0


if __name__ == "__main__":
	sys.exit(main())
