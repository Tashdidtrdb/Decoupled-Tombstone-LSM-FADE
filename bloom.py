import hashlib
import math

# Default memory budget per key. Monkey [8] shows a uniform allocation across all
# levels is suboptimal, so callers may pass a different bits-per-element per level.
# 10 bits/element gives roughly a 1% false positive rate at the optimal k.
DEFAULT_BITS_PER_ELEMENT = 10


def optimal_num_hashes(bits_per_element):
	"""
	k = (m/n) * ln2 minimises the false positive rate for a given bits-per-element.
	Clamped to at least 1 so a tiny budget still produces a usable filter.
	"""
	return max(1, int(round(bits_per_element * math.log(2))))


class BloomFilter:
	"""
	Classic Bloom filter over a bit array.

	Membership is probabilistic in one direction only: contains() may return True
	for a key that was never added (false positive), but never returns False for a
	key that was added. That asymmetry is what makes it safe as a lookup guard --
	a negative answer is definitive, so the caller can skip the file entirely.

	Bits are packed into a Python bytearray rather than a list of booleans so the
	reported memory footprint reflects a real filter.
	"""

	def __init__(self, expected_entries, bits_per_element=DEFAULT_BITS_PER_ELEMENT):
		self.expected_entries = max(1, expected_entries)
		self.bits_per_element = bits_per_element
		self.num_bits = max(8, self.expected_entries * bits_per_element)
		self.num_hashes = optimal_num_hashes(bits_per_element)
		self.bits = bytearray((self.num_bits + 7) // 8)
		self.num_added = 0

	def _bit_positions(self, key):
		"""
		Kirsch-Mitzenmacher double hashing: derive k indexes from two independent
		hashes instead of computing k separate digests. g_i(x) = h1(x) + i*h2(x).
		"""
		digest = hashlib.sha256(str(key).encode()).digest()
		h1 = int.from_bytes(digest[:8], "big")
		h2 = int.from_bytes(digest[8:16], "big") | 1  # odd, so it stays coprime-ish
		for i in range(self.num_hashes):
			yield (h1 + i * h2) % self.num_bits

	def add(self, key):
		for pos in self._bit_positions(key):
			self.bits[pos // 8] |= 1 << (pos % 8)
		self.num_added += 1

	def contains(self, key):
		"""False means definitely absent. True means probably present."""
		for pos in self._bit_positions(key):
			if not (self.bits[pos // 8] >> (pos % 8)) & 1:
				return False
		return True

	def memory_bytes(self):
		return len(self.bits)

	def estimated_fpr(self):
		"""
		(1 - e^(-k*n/m))^k -- the standard false positive estimate for the number
		of keys actually inserted, which may differ from expected_entries.
		"""
		if self.num_added == 0:
			return 0.0
		exponent = -self.num_hashes * self.num_added / self.num_bits
		return (1 - math.exp(exponent)) ** self.num_hashes

	@staticmethod
	def build(keys, bits_per_element=DEFAULT_BITS_PER_ELEMENT):
		bf = BloomFilter(len(keys), bits_per_element)
		for key in keys:
			bf.add(key)
		return bf
