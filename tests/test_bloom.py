import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bloom import BloomFilter, optimal_num_hashes


def test_no_false_negatives():
	# the safety property the lookup path depends on: a key that was added must
	# never report absent, otherwise get() would skip a file that holds the key
	keys = list(range(5000))
	bf = BloomFilter.build(keys)
	missing = [k for k in keys if not bf.contains(k)]
	assert not missing, f"{len(missing)} false negatives"
	print(f"bloom pass -- no false negatives across {len(keys)} keys")


def test_false_positive_rate_matches_theory():
	keys = list(range(2000))
	bf = BloomFilter.build(keys)
	absent = range(10 ** 6, 10 ** 6 + 20000)
	observed = sum(1 for k in absent if bf.contains(k)) / 20000
	predicted = bf.estimated_fpr()
	# generous tolerance: this is a statistical property, not an exact one
	assert abs(observed - predicted) < 0.02, f"observed {observed}, predicted {predicted}"
	assert observed < 0.05, f"FPR {observed} too high for 10 bits/element"
	print(f"bloom pass -- FPR observed {observed:.4f} vs predicted {predicted:.4f}")


def test_more_bits_lowers_fpr():
	# the Monkey premise: spending more bits per element buys a lower FPR, which
	# is what makes non-uniform allocation across levels worth doing
	keys = list(range(2000))
	absent = range(10 ** 6, 10 ** 6 + 10000)
	rates = []
	for bpe in [4, 8, 16]:
		bf = BloomFilter.build(keys, bits_per_element=bpe)
		rates.append(sum(1 for k in absent if bf.contains(k)) / 10000)
	assert rates[0] > rates[1] > rates[2], f"FPR did not fall monotonically: {rates}"
	print(f"bloom pass -- FPR falls with budget: 4b={rates[0]:.3f} 8b={rates[1]:.3f} 16b={rates[2]:.3f}")


def test_optimal_k():
	assert optimal_num_hashes(10) == 7
	assert optimal_num_hashes(1) >= 1  # never degenerate to zero hashes
	print("bloom pass -- optimal k matches (m/n)*ln2")


def test_empty_and_single():
	bf = BloomFilter.build([])
	assert bf.estimated_fpr() == 0.0
	bf2 = BloomFilter.build([42])
	assert bf2.contains(42)
	print("bloom pass -- empty and single-key filters behave")


if __name__ == "__main__":
	test_no_false_negatives()
	test_false_positive_rate_matches_theory()
	test_more_bits_lowers_fpr()
	test_optimal_k()
	test_empty_and_single()
