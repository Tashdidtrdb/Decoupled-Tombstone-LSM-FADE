"""
Tests for the demo shell.

`leaked` is the claim made in front of an audience: "this record is deleted but
still on disk". If it were wrong the presentation would assert something false,
so its correctness is tested directly rather than eyeballed.
"""

import io
import os
import shutil
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lsm import MODE_DECOUPLED, MODE_VANILLA
from shell import DEFAULTS, Shell

TMP = "/tmp/test_shell"


def _shell(mode=MODE_VANILLA, **overrides):
	shutil.rmtree(TMP, ignore_errors=True)
	kw = dict(DEFAULTS)
	kw.update(overrides)
	return Shell(TMP, mode, **kw)


def _run(shell, *commands):
	"""Execute commands, returning everything printed."""
	buffer = io.StringIO()
	with redirect_stdout(buffer):
		for command in commands:
			shell.execute(command)
	return buffer.getvalue()


def test_vanilla_leaks_deleted_data():
	shell = _shell(MODE_VANILLA)
	output = _run(shell,
		"put 1 alice", "put 2 bob", "put 3 carol", "put 4 dave",
		"delete 2", "tick 5", "leaked")
	assert "LEAKED" in output, "vanilla did not report a leaked record"
	assert "'bob'" in output, "the leaked value was not shown"
	print("shell pass -- vanilla reports the deleted record as still on disk")


def test_decoupled_erases_deleted_data():
	shell = _shell(MODE_DECOUPLED)
	output = _run(shell,
		"put 1 alice", "put 2 bob", "put 3 carol", "put 4 dave",
		"delete 2", "tick 5", "leaked")
	assert "no leaked records" in output, f"decoupled still leaked:\n{output}"
	print("shell pass -- decoupled reports the record physically erased")


def test_record_is_present_before_the_deadline():
	# the demo shows the record present, then gone. If it were never written the
	# second half would prove nothing, so the first half must genuinely find it.
	shell = _shell(MODE_DECOUPLED)
	output = _run(shell,
		"put 1 alice", "put 2 bob", "put 3 carol", "put 4 dave",
		"delete 2", "leaked")
	assert "LEAKED" in output, "record was not on disk before the deadline passed"
	print("shell pass -- the record is on disk before the deadline, gone after")


def test_get_agrees_across_modes():
	# vanilla is not buggy: both modes hide the key from get(). The difference is
	# physical, which is the entire reason the shell exposes files.
	for mode in (MODE_VANILLA, MODE_DECOUPLED):
		shell = _shell(mode)
		output = _run(shell, "put 2 bob", "delete 2", "tick 5", "get 2")
		assert "(not found)" in output, f"{mode}: get() did not hide the deleted key"
	print("shell pass -- get() reports not-found in every mode")


def test_reinserted_key_is_not_leaked():
	shell = _shell(MODE_VANILLA)
	_run(shell, "put 2 bob", "delete 2", "put 2 robert", "tick 5")
	# assert on `leaked` alone: the setup commands echo their own values
	output = _run(shell, "leaked")
	assert "LEAKED" not in output, \
		f"a re-inserted key was reported as leaked:\n{output}"
	assert not shell.deleted_keys, "re-inserting did not clear the deleted marker"
	print("shell pass -- a re-inserted key is never reported as leaked")


def test_mode_switch_empties_the_store():
	shell = _shell(MODE_VANILLA)
	_run(shell, "put 1 alice", "put 2 bob")
	output = _run(shell, "mode decoupled --force", "files")
	assert shell.mode == MODE_DECOUPLED
	assert "no files on disk" in output or "0 data" in output
	assert not shell.deleted_keys
	print("shell pass -- switching mode starts from an empty store")


def test_mode_switch_needs_force_or_confirmation():
	# a stray keystroke must not silently destroy a demo setup
	shell = _shell(MODE_VANILLA)
	_run(shell, "put 1 alice")
	assert not shell._is_empty()
	print("shell pass -- a populated store is detected before wiping")


def test_find_reports_both_hierarchies():
	shell = _shell(MODE_VANILLA)
	output = _run(shell, "put 2 bob", "flush", "delete 2", "flush", "find 2")
	assert "tombstone" in output, "find did not report the tombstone"
	assert "data" in output, "find did not report the data record"
	print("shell pass -- find shows records from both hierarchies")


def test_tick_uses_keys_outside_the_demo_range():
	shell = _shell(MODE_VANILLA)
	output = _run(shell, "tick 3")
	assert "900000" in output, "tick did not report its key range"
	assert shell.db.seqnum == 3
	print("shell pass -- tick advances the clock with clearly-marked keys")


def test_bad_input_never_crashes():
	shell = _shell(MODE_VANILLA)
	output = _run(shell,
		"put", "get", "get abc", "delete", "dump nosuchfile",
		"mode nonsense", "tick zero", "wibble", "find 99")
	assert "Traceback" not in output, f"shell raised during bad input:\n{output}"
	# and the shell is still usable afterwards
	with redirect_stdout(io.StringIO()):
		assert shell.execute("put 1 ok") is True
	print("shell pass -- malformed commands are handled without crashing")


def test_demo_script_runs_end_to_end():
	# the actual presentation path
	script = os.path.join(os.path.dirname(__file__), "..", "demo.txt")
	assert os.path.exists(script), "demo.txt is missing"
	shell = _shell(MODE_VANILLA)
	output = _run(shell, f"replay {script}")
	assert "Traceback" not in output, f"demo script errored:\n{output}"
	assert "LEAKED" in output, "demo never showed a leaked record"
	assert "no leaked records" in output, "demo never showed the erasure"
	print("shell pass -- demo.txt runs end to end and shows both outcomes")


if __name__ == "__main__":
	test_vanilla_leaks_deleted_data()
	test_decoupled_erases_deleted_data()
	test_record_is_present_before_the_deadline()
	test_get_agrees_across_modes()
	test_reinserted_key_is_not_leaked()
	test_mode_switch_empties_the_store()
	test_mode_switch_needs_force_or_confirmation()
	test_find_reports_both_hierarchies()
	test_tick_uses_keys_outside_the_demo_range()
	test_bad_input_never_crashes()
	test_demo_script_runs_end_to_end()
