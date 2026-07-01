import random
import numpy as np

def generate(
	num_ops=10000,
	key_space=1000,
	delete_ratio=0.2,
	get_ratio=0.3,
	skew="uniform",
	zipf_param=1.2,
	seed=42
):
	# put_ratio fills whatever is left after delete and get
	put_ratio = 1.0 - delete_ratio - get_ratio
	assert put_ratio > 0, "delete_ratio + get_ratio must be less than 1.0"

	rng = random.Random(seed)
	np_rng = np.random.default_rng(seed)

	def next_key():
		if skew == "zipf":
			# numpy zipf returns values starting at 1, clamp to key_space
			k = int(np_rng.zipf(zipf_param))
			return min(k, key_space) - 1
		return rng.randint(0, key_space - 1)

	ops = []
	for i in range(num_ops):
		r = rng.random()
		key = next_key()
		if r < put_ratio:
			ops.append(("put", key, f"v{i}"))
		elif r < put_ratio + delete_ratio:
			ops.append(("delete", key, None))
		else:
			ops.append(("get", key, None))

	return ops
