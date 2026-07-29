import os
import shutil
import json

from lsm import LSM


TEST_DIR = "/tmp/dual_lsm_test"

shutil.rmtree(TEST_DIR, ignore_errors=True)

db = LSM(
    TEST_DIR,
    memtable_size=2,
    num_levels=4,
    l0_capacity_bytes=10**8,
    deadline=100
)

db.put(1, "apple")
db.put(2, "banana")

db.delete(1)
db.delete(3)

db.flush()

print("\nCreated files:")

for filename in sorted(os.listdir(TEST_DIR)):
    print(filename)

    filepath = os.path.join(TEST_DIR, filename)

    with open(filepath, "r") as file:
        records = json.load(file)

    record_types = [record["type"] for record in records]
    print("  Record types:", record_types)