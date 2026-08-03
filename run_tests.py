"""
Run every test file and report a summary.

Usage: python3 run_tests.py
"""

import glob
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def discover():
	paths = sorted(glob.glob(os.path.join(ROOT, "tests", "test_*.py")))
	paths += sorted(glob.glob(os.path.join(ROOT, "test_*.py")))
	return paths


def main():
	passed, failed = [], []
	for path in discover():
		name = os.path.relpath(path, ROOT)
		result = subprocess.run(
			[sys.executable, path],
			capture_output=True, text=True, cwd=ROOT
		)
		if result.returncode == 0:
			passed.append(name)
			print(f"  ok   {name}")
		else:
			failed.append((name, result.stdout, result.stderr))
			print(f"  FAIL {name}")

	print(f"\n{len(passed)} passed, {len(failed)} failed")
	for name, out, err in failed:
		print(f"\n--- {name} ---")
		tail = (err or out).strip().split("\n")
		print("\n".join(tail[-8:]))

	return 1 if failed else 0


if __name__ == "__main__":
	sys.exit(main())
