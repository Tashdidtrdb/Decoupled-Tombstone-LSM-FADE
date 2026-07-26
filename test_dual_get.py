import shutil
from lsm import LSM


TEST_DIR = "/tmp/dual_get_test"

shutil.rmtree(TEST_DIR, ignore_errors=True)

db = LSM(
	TEST_DIR,
	memtable_size=1,
	num_levels=4,
	l0_capacity_bytes=10**8,
	deadline=100
)

# PUT followed by DELETE
db.put(1, "old value")
db.delete(1)

assert db.get(1) is None
print("Test 1 passed: newer DELETE hides older PUT")

# DELETE followed by newer PUT
db.delete(2)
db.put(2, "new value")

assert db.get(2) == "new value"
print("Test 2 passed: newer PUT overrides older DELETE")

# Existing value
db.put(3, "hello")

assert db.get(3) == "hello"
print("Test 3 passed: normal PUT lookup works")

# Missing key
assert db.get(99) is None
print("Test 4 passed: missing key returns None")