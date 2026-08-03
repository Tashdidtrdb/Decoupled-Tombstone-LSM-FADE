"""
YCSB-style workload generator.

Follows the Yahoo! Cloud Serving Benchmark's structure: a load phase that
populates the key space, then a run phase that mixes operations according to a
named distribution. Extended with deletes, which core YCSB does not model but
which are the entire subject of this project.

The key distributions matter more than the operation mix here. Delete locality
is what decides how wide a compliance compaction has to reach, so a faithful
Zipfian is worth more to this evaluation than extra operation types.
"""

import random

# Standard YCSB mixes, plus delete-heavy variants for this project.
WORKLOADS = {
	# YCSB-A: 50/50 read/update
	"A": {"read": 0.50, "update": 0.50, "insert": 0.00, "delete": 0.00},
	# YCSB-B: 95/5 read-mostly
	"B": {"read": 0.95, "update": 0.05, "insert": 0.00, "delete": 0.00},
	# YCSB-C: read-only
	"C": {"read": 1.00, "update": 0.00, "insert": 0.00, "delete": 0.00},
	# YCSB-D: read-latest, mostly reads with new inserts
	"D": {"read": 0.95, "update": 0.00, "insert": 0.05, "delete": 0.00},
	# Delete-aware mixes. "X" is the project's default: a realistic update-heavy
	# workload with a meaningful delete stream.
	"X": {"read": 0.30, "update": 0.45, "insert": 0.00, "delete": 0.25},
	"X-light": {"read": 0.35, "update": 0.60, "insert": 0.00, "delete": 0.05},
	"X-heavy": {"read": 0.20, "update": 0.40, "insert": 0.00, "delete": 0.40},
}


class ZipfianGenerator:
	"""
	Zipfian key generator over a bounded key space.

	Uses the standard YCSB formulation rather than numpy's zipf(). numpy draws
	from an unbounded distribution, so bounding it means clamping, which piles
	the entire tail onto the last key -- at zipf_param=1.2 that single key
	absorbs over 20% of all operations, which is an artefact rather than skew.
	This computes the bounded distribution directly, so every key keeps its
	proper share.
	"""

	def __init__(self, num_keys, constant=0.99, rng=None):
		self.num_keys = num_keys
		self.constant = constant
		self.rng = rng or random.Random()
		self.zeta_n = self._zeta(num_keys, constant)
		self.zeta_2 = self._zeta(2, constant)
		self.alpha = 1.0 / (1.0 - constant)
		self.eta = (
			(1.0 - (2.0 / num_keys) ** (1.0 - constant))
			/ (1.0 - self.zeta_2 / self.zeta_n)
		)

	@staticmethod
	def _zeta(n, theta):
		return sum(1.0 / (i ** theta) for i in range(1, n + 1))

	def next_key(self):
		u = self.rng.random()
		uz = u * self.zeta_n
		if uz < 1.0:
			return 0
		if uz < 1.0 + 0.5 ** self.constant:
			return 1
		return int(self.num_keys * ((self.eta * u - self.eta + 1.0) ** self.alpha))


class UniformGenerator:
	def __init__(self, num_keys, rng=None):
		self.num_keys = num_keys
		self.rng = rng or random.Random()

	def next_key(self):
		return self.rng.randrange(self.num_keys)


class LatestGenerator:
	"""
	Skewed toward recently written keys -- YCSB's "latest" distribution. Models
	workloads where new records are hot, so deletes cluster on a moving window
	rather than a fixed hot set.
	"""

	def __init__(self, num_keys, constant=0.99, rng=None):
		self.num_keys = num_keys
		self.zipf = ZipfianGenerator(num_keys, constant, rng)

	def next_key(self):
		# invert so rank 0 maps to the most recent key
		return self.num_keys - 1 - self.zipf.next_key()


def make_key_generator(distribution, num_keys, rng, zipf_constant=0.99):
	if distribution == "uniform":
		return UniformGenerator(num_keys, rng)
	if distribution == "zipfian":
		return ZipfianGenerator(num_keys, zipf_constant, rng)
	if distribution == "latest":
		return LatestGenerator(num_keys, zipf_constant, rng)
	raise ValueError(f"unknown distribution: {distribution}")


def generate_ycsb(
	workload="X",
	num_ops=10000,
	key_space=1000,
	distribution="zipfian",
	value_size=20,
	zipf_constant=0.99,
	seed=42,
	load_phase=True,
):
	"""
	Produce a (load, run) operation list.

	The load phase inserts every key once so the run phase operates on a
	populated store. Without it, deletes land on keys that were never written
	and no compliance work is triggered, which would understate every cost this
	project measures.
	"""
	if workload not in WORKLOADS:
		raise ValueError(f"unknown workload {workload!r}, expected one of {sorted(WORKLOADS)}")
	mix = WORKLOADS[workload]
	rng = random.Random(seed)
	keygen = make_key_generator(distribution, key_space, rng, zipf_constant)

	ops = []
	if load_phase:
		for key in range(key_space):
			ops.append(("put", key, _value(key, value_size)))

	thresholds = []
	cumulative = 0.0
	for name in ("read", "update", "insert", "delete"):
		cumulative += mix[name]
		thresholds.append((cumulative, name))

	next_insert_key = key_space
	for i in range(num_ops):
		r = rng.random()
		action = thresholds[-1][1]
		for limit, name in thresholds:
			if r < limit:
				action = name
				break

		if action == "read":
			ops.append(("get", keygen.next_key(), None))
		elif action == "update":
			ops.append(("put", keygen.next_key(), _value(i, value_size)))
		elif action == "insert":
			ops.append(("put", next_insert_key, _value(i, value_size)))
			next_insert_key += 1
		else:
			ops.append(("delete", keygen.next_key(), None))

	return ops


def _value(seed_int, size):
	# deterministic payload of the requested size; content is irrelevant to WAF,
	# only its length matters
	body = f"v{seed_int}"
	if len(body) >= size:
		return body[:size]
	return body + "x" * (size - len(body))


def describe(ops):
	"""Summarise an operation list for benchmark output."""
	counts = {"put": 0, "get": 0, "delete": 0}
	keys = set()
	for op, key, _ in ops:
		counts[op] += 1
		keys.add(key)
	return {
		"total": len(ops),
		"puts": counts["put"],
		"gets": counts["get"],
		"deletes": counts["delete"],
		"distinct_keys": len(keys),
	}
