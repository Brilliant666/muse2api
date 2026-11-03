"""JSON Store crash-boundary regressions; synthetic accounts and no network."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import store as store_module


def configuration(root):
    return SimpleNamespace(data_dir=str(root), accounts_file=str(root / "accounts.json"),
        tasks_file=str(root / "tasks.json"),
        protocol_operations_file=str(root / "protocol_operations.json"))


def crash_worker(root, account_id, operation_id):
    target = store_module.Store(configuration(Path(root)))
    assert target.get_protocol_operation(operation_id)["account_id"] == account_id
    original = store_module._write
    writes = 0

    def exit_after_first_write(path, value):
        nonlocal writes
        original(path, value)
        writes += 1
        if writes == 1:
            os._exit(87)

    store_module._write = exit_after_first_write
    target.update_protocol_operation(operation_id, state="completed", delivery="completed",
                                     result={"text": "existing original result"})
    raise AssertionError("crash boundary was not reached")


class StoreCrashConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="muse-phase2-store-crash-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.cfg = configuration(self.root)
        self.store = store_module.Store(self.cfg)
        self.account = self.store.add_account({"hatch_sess": "synthetic-no-upstream-account"})
        self.aid = self.account["id"]
        self.rid = "synthetic-original-operation"
        self.store.begin_protocol_operation(self.rid, self.aid, "chat", "synthetic original prompt")
        self.store.update_protocol_operation(self.rid, state="unknown", delivery="accepted",
            session_id="synthetic-original-session", message_id="synthetic-original-message",
            vm_id="synthetic-original-vm")

    def restart(self):
        restarted = store_module.Store(self.cfg)
        restarted.recover_interrupted_protocol_operations()
        return restarted

    def test_process_exit_between_completion_writes_preserves_safety(self):
        process = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--crash-worker",
            str(self.root), self.aid, self.rid], timeout=15, capture_output=True, text=True)
        self.assertEqual(process.returncode, 87, process.stderr)
        restarted = self.restart()
        operation = restarted.get_protocol_operation(self.rid)
        blocked = restarted.get_account(self.aid).get("protocol_blocked")
        self.assertTrue(operation["state"] == "completed" or blocked == self.rid,
            f"unconfirmed operation must remain quarantined after crash: state={operation['state']}, blocked={blocked}")
        if operation["state"] == "completed":
            self.assertFalse(blocked, "durably completed result must clear stale quarantine during startup")
        self.assertEqual(set(restarted.protocol_operations), {self.rid})
        self.assertEqual(operation["session_id"], "synthetic-original-session")
        self.assertEqual(operation["message_id"], "synthetic-original-message")

    def test_startup_rebuilds_quarantine_for_existing_unknown(self):
        self.store.accounts[0].pop("protocol_blocked", None)
        store_module._write(self.cfg.accounts_file, self.store.accounts)
        before = Path(self.cfg.protocol_operations_file).read_bytes()
        restarted = self.restart()
        self.assertEqual(restarted.get_account(self.aid).get("protocol_blocked"), self.rid)
        self.assertEqual(Path(self.cfg.protocol_operations_file).read_bytes(), before)
        self.assertIsNone(restarted.pick_account(record_use=False))

    def test_startup_clears_quarantine_only_for_known_completed_operation(self):
        self.store.protocol_operations[self.rid].update(state="completed", delivery="completed",
                                                       result={"text": "existing result"})
        store_module._write(self.cfg.protocol_operations_file, self.store.protocol_operations)
        restarted = self.restart()
        self.assertFalse(restarted.get_account(self.aid).get("protocol_blocked"))
        self.assertEqual(restarted.pick_account(record_use=False)["id"], self.aid)

    def test_completing_one_unknown_does_not_release_another(self):
        other = "synthetic-second-unknown"
        self.store.begin_protocol_operation(other, self.aid, "chat", "second synthetic prompt")
        self.store.update_protocol_operation(other, state="unknown", delivery="accepted")
        self.store.update_protocol_operation(other, state="completed", delivery="completed")
        self.assertEqual(self.store.get_account(self.aid).get("protocol_blocked"), self.rid)
        restarted = self.restart()
        self.assertEqual(restarted.get_account(self.aid).get("protocol_blocked"), self.rid)
        self.assertIsNone(restarted.pick_account(record_use=False))

    def test_failed_completion_write_does_not_publish_completion_or_release(self):
        original = store_module._write

        def fail_operation_file(path, value):
            if path == self.cfg.protocol_operations_file:
                raise OSError("synthetic operation persistence failure")
            return original(path, value)

        with mock.patch.object(store_module, "_write", side_effect=fail_operation_file):
            with self.assertRaises(OSError):
                self.store.update_protocol_operation(self.rid, state="completed", delivery="completed")
        self.assertEqual(self.store.get_protocol_operation(self.rid)["state"], "unknown")
        self.assertEqual(self.store.get_account(self.aid).get("protocol_blocked"), self.rid)
        restarted = self.restart()
        self.assertEqual(restarted.get_protocol_operation(self.rid)["state"], "unknown")
        self.assertEqual(restarted.get_account(self.aid).get("protocol_blocked"), self.rid)

    def test_failed_unknown_write_quarantines_only_its_account(self):
        affected = self.store.add_account({"hatch_sess": "synthetic-affected-account"})
        healthy = self.store.add_account({"hatch_sess": "synthetic-unaffected-account"})
        request_id = "synthetic-accepted-before-write-failure"
        self.store.begin_protocol_operation(request_id, affected["id"], "chat", "original prompt")
        self.store.update_protocol_operation(request_id, state="running", delivery="accepted")
        original = store_module._write

        def fail_operation_file(path, value):
            if path == self.cfg.protocol_operations_file:
                raise OSError("synthetic uncertainty persistence failure")
            return original(path, value)

        with mock.patch.object(store_module, "_write", side_effect=fail_operation_file):
            with self.assertRaises(OSError):
                self.store.update_protocol_operation(request_id, state="unknown", delivery="accepted")
        self.assertEqual(self.store.get_account(affected["id"]).get("protocol_blocked"), request_id)
        self.assertEqual(self.store.pick_account(record_use=False)["id"], healthy["id"])
        restarted = self.restart()
        self.assertEqual(restarted.get_protocol_operation(request_id)["state"], "unknown")
        self.assertEqual(restarted.get_account(affected["id"]).get("protocol_blocked"), request_id)
        self.assertEqual(restarted.pick_account(record_use=False)["id"], healthy["id"])

    def test_missing_operation_reference_stays_fail_closed(self):
        self.store.protocol_operations.clear()
        store_module._write(self.cfg.protocol_operations_file, self.store.protocol_operations)
        restarted = self.restart()
        self.assertEqual(restarted.get_account(self.aid).get("protocol_blocked"), self.rid)
        self.assertIsNone(restarted.pick_account(record_use=False))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--crash-worker":
        crash_worker(*sys.argv[2:])
    else:
        unittest.main()
