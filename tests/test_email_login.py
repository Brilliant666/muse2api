"""Pure login-queue regressions: synthetic driver, fresh data, no browser/network."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from email_login import LoginManager


SECRET_CODE = "987654"
SECRET_COOKIE = "synthetic-private-cookie-never-persist"
SECRET_PRINCIPAL = "synthetic-private-principal-never-persist"


def authenticated():
    return {"status": "AUTHENTICATED", "cookies": {"session": SECRET_COOKIE},
            "expires": {"session": 12345}, "principal_id": SECRET_PRINCIPAL}


class Driver:
    def __init__(self):
        self.begins, self.verifies = [], []
        self.begin_results, self.verify_results = [], []
        self.begin_entered, self.verify_entered = threading.Event(), threading.Event()
        self.begin_release, self.verify_release = threading.Event(), threading.Event()
        self.begin_release.set()
        self.verify_release.set()
        self.sessions_closed = self.closed = 0

    def begin(self, email):
        self.begins.append(email)
        self.begin_entered.set()
        if not self.begin_release.wait(4):
            raise RuntimeError("test-driver-begin-timeout")
        result = self.begin_results.pop(0) if self.begin_results else {"status": "WAITING_FOR_CODE", "code_length": 6}
        if isinstance(result, Exception):
            raise result
        return result

    def verify(self, code):
        self.verifies.append(code)
        self.verify_entered.set()
        if not self.verify_release.wait(4):
            raise RuntimeError("test-driver-verify-timeout")
        result = self.verify_results.pop(0) if self.verify_results else authenticated()
        if isinstance(result, Exception):
            raise result
        return result

    def close_session(self):
        self.sessions_closed += 1

    def close(self):
        self.closed += 1


class LoginQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="muse-email-login-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.driver, self.saved = Driver(), []
        self.managers = []
        self.addCleanup(self.cleanup_managers)

    def cleanup_managers(self):
        self.driver.begin_release.set()
        self.driver.verify_release.set()
        for manager in self.managers:
            manager.close()

    def manager(self, **kwargs):
        result = LoginManager(self.temp.name, kwargs.pop("driver_factory", lambda: self.driver),
                              kwargs.pop("save_account", lambda email, value: self.saved.append((email, value))), **kwargs)
        self.managers.append(result)
        return result

    def wait(self, manager, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = manager.snapshot()
            if predicate(result):
                return result
            time.sleep(0.005)
        self.fail("login queue did not reach expected safe state: " + repr(manager.snapshot()))

    def waiting(self, manager, index=0):
        return self.wait(manager, lambda state: state["jobs"][index]["status"] == "WAITING_FOR_CODE")["jobs"][index]

    def test_lazy_driver_and_nonblocking_create(self):
        calls = []
        self.driver.begin_release.clear()
        manager = self.manager(driver_factory=lambda: calls.append(True) or self.driver)
        self.assertEqual([], calls)
        self.assertIsNone(manager.snapshot()["available"])
        start = time.monotonic()
        state = manager.create(["One@Example.test"])
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual("QUEUED", state["jobs"][0]["status"])
        self.assertTrue(self.driver.begin_entered.wait(1))
        self.assertEqual(["one@example.test"], self.driver.begins)
        self.driver.begin_release.set()
        self.waiting(manager)
        self.assertTrue(manager.snapshot()["available"])

    def test_double_submit_is_atomic_and_success_is_saved_once(self):
        manager = self.manager()
        job = manager.create(["one@example.test"])["jobs"][0]
        self.waiting(manager)
        self.driver.verify_release.clear()
        barrier = threading.Barrier(3)
        outcomes = []

        def submit():
            barrier.wait()
            try:
                outcomes.append(manager.submit_code(job["id"], SECRET_CODE))
            except ValueError as error:
                outcomes.append(str(error))

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(1)
        self.assertEqual(1, sum(isinstance(value, dict) for value in outcomes))
        self.assertIn("login_job_not_waiting", outcomes)
        self.assertTrue(self.driver.verify_entered.wait(1))
        self.assertEqual(1, manager.snapshot()["jobs"][0]["attempts"])
        self.driver.verify_release.set()
        self.wait(manager, lambda s: s["jobs"][0]["status"] == "AUTHENTICATED")
        self.assertEqual([SECRET_CODE], self.driver.verifies)
        self.assertEqual(1, len(self.driver.begins))
        self.assertEqual(1, len(self.saved))
        self.assertEqual(SECRET_PRINCIPAL, self.saved[0][1]["principal_id"])

    def test_invalid_code_max_three_attempts_without_resend(self):
        self.driver.verify_results = [{"status": "INVALID_CODE", "message": SECRET_COOKIE}] * 3
        manager = self.manager()
        job = manager.create(["one@example.test"])["jobs"][0]
        for number in range(3):
            self.waiting(manager)
            manager.submit_code(job["id"], SECRET_CODE)
            status = "WAITING_FOR_CODE" if number < 2 else "FAILED"
            state = self.wait(manager, lambda s: s["jobs"][0]["status"] == status)
            self.assertEqual(number + 1, state["jobs"][0]["attempts"])
        self.assertEqual("CODE_ATTEMPTS_EXHAUSTED", state["jobs"][0]["error_code"])
        self.assertEqual(1, len(self.driver.begins))
        self.assertEqual(3, len(self.driver.verifies))
        self.assertEqual([], self.saved)
        with self.assertRaisesRegex(ValueError, "login_job_not_waiting"):
            manager.submit_code(job["id"], SECRET_CODE)

    def test_restart_interrupts_all_pending_jobs_without_factory_or_resend(self):
        rows = [dict(id=uuid.uuid4().hex, email=f"old{index}@example.test", status=status,
                     created_at=1, updated_at=2, deadline_at=3, begin_count=1, attempts=1,
                     code_length=6, error_code=None)
                for index, status in enumerate(("QUEUED", "STARTING", "WAITING_FOR_CODE", "VERIFYING", "SAVING"))]
        path = Path(self.temp.name) / "email-login-jobs.json"
        path.write_text(json.dumps({"version": 1, "jobs": rows}), encoding="utf-8")
        calls = []
        manager = self.manager(driver_factory=lambda: calls.append(True) or self.driver)
        self.assertEqual({"INTERRUPTED"}, {row["status"] for row in manager.snapshot()["jobs"]})
        time.sleep(0.03)
        self.assertEqual([], calls)
        self.assertEqual([], self.driver.begins)
        self.assertEqual({"PROCESS_RESTARTED"}, {row["error_code"] for row in json.loads(path.read_text())["jobs"]})

    def test_no_otp_cookies_principal_or_exception_text_persisted(self):
        manager = self.manager()
        job = manager.create(["one@example.test"])["jobs"][0]
        self.waiting(manager)
        self.driver.verify_release.clear()
        manager.submit_code(job["id"], SECRET_CODE)
        self.assertTrue(self.driver.verify_entered.wait(1))
        payload = (Path(self.temp.name) / "email-login-jobs.json").read_text()
        self.assertNotIn(SECRET_CODE, payload)
        self.driver.verify_release.set()
        self.wait(manager, lambda s: s["jobs"][0]["status"] == "AUTHENTICATED")
        combined = json.dumps(manager.snapshot()) + "".join(path.read_text() for path in Path(self.temp.name).iterdir())
        for private in (SECRET_CODE, SECRET_COOKIE, SECRET_PRINCIPAL):
            self.assertNotIn(private, combined)
        self.assertEqual(set(("id", "email", "status", "created_at", "updated_at", "deadline_at", "begin_count", "attempts", "code_length", "error_code")), set(json.loads(payload)["jobs"][0]))

    def test_failure_isolation_cancel_queued_and_active_verify(self):
        self.driver.begin_results = [{"status": "BLOCKED", "message": SECRET_COOKIE}]
        manager = self.manager()
        jobs = manager.create(["blocked@example.test", "cancel@example.test", "last@example.test"])["jobs"]
        self.waiting(manager, 1)
        self.driver.verify_release.clear()
        manager.submit_code(jobs[1]["id"], SECRET_CODE)
        self.assertTrue(self.driver.verify_entered.wait(1))
        manager.cancel(jobs[1]["id"])
        self.driver.verify_release.set()
        self.waiting(manager, 2)
        self.assertEqual([], self.saved)
        manager.cancel(jobs[2]["id"])
        state = self.wait(manager, lambda s: s["active_job_id"] is None)
        self.assertEqual(["BLOCKED", "CANCELLED", "CANCELLED"], [row["status"] for row in state["jobs"]])
        self.assertEqual(3, self.driver.sessions_closed)
        self.assertNotIn(SECRET_COOKIE, json.dumps(state))

    def test_begin_and_verify_exceptions_are_safe_and_isolated(self):
        self.driver.begin_results = [RuntimeError(SECRET_COOKIE)]
        self.driver.verify_results = [RuntimeError(SECRET_COOKIE)]
        manager = self.manager()
        jobs = manager.create(["beginfail@example.test", "verifyfail@example.test", "next@example.test"])["jobs"]
        self.waiting(manager, 1)
        self.assertEqual("UNKNOWN", manager.snapshot()["jobs"][0]["status"])
        manager.submit_code(jobs[1]["id"], SECRET_CODE)
        self.waiting(manager, 2)
        state = manager.snapshot()
        self.assertEqual("UNKNOWN", state["jobs"][1]["status"])
        self.assertEqual(3, len(self.driver.begins))
        self.assertEqual(1, len(self.driver.verifies))
        self.assertEqual([], self.saved)
        self.assertNotIn(SECRET_COOKIE, json.dumps(state))

    def test_queued_cancel_never_begins_and_expiry_advances(self):
        manager = self.manager(otp_timeout=0.08)
        jobs = manager.create(["expire@example.test", "skip@example.test", "next@example.test"])["jobs"]
        self.waiting(manager)
        manager.cancel(jobs[1]["id"])
        self.waiting(manager, 2)
        self.assertEqual(["expire@example.test", "next@example.test"], self.driver.begins)
        self.assertEqual("EXPIRED", manager.snapshot()["jobs"][0]["status"])
        self.assertEqual(0, manager.snapshot()["jobs"][1]["begin_count"])

    def test_callback_failure_is_not_retried_and_next_job_runs(self):
        def save(email, result):
            if email.startswith("fail"):
                raise RuntimeError(SECRET_COOKIE)
            self.saved.append((email, result))

        manager = self.manager(save_account=save)
        jobs = manager.create(["fail@example.test", "ok@example.test"])["jobs"]
        self.waiting(manager)
        manager.submit_code(jobs[0]["id"], SECRET_CODE)
        self.waiting(manager, 1)
        first = manager.snapshot()["jobs"][0]
        self.assertEqual(("FAILED", "SAVE_ACCOUNT_FAILED"), (first["status"], first["error_code"]))
        manager.submit_code(jobs[1]["id"], SECRET_CODE)
        self.wait(manager, lambda s: s["jobs"][1]["status"] == "AUTHENTICATED")
        self.assertEqual(2, len(self.driver.begins))
        self.assertEqual(2, len(self.driver.verifies))
        self.assertEqual(1, len(self.saved))
        self.assertNotIn(SECRET_COOKIE, json.dumps(manager.snapshot()))

    def test_browser_unavailable_safe_and_no_repeated_factory(self):
        calls = []

        def unavailable():
            calls.append(True)
            raise RuntimeError(SECRET_COOKIE)

        manager = self.manager(driver_factory=unavailable)
        manager.create(["one@example.test", "two@example.test"])
        state = self.wait(manager, lambda s: all(row["status"] == "FAILED" for row in s["jobs"]))
        self.assertFalse(state["available"])
        self.assertEqual([True], calls)
        self.assertEqual({"BROWSER_UNAVAILABLE"}, {row["error_code"] for row in state["jobs"]})
        self.assertNotIn(SECRET_COOKIE, json.dumps(state))

    def test_cancel_write_failure_preserves_waiting_and_pending_verification(self):
        manager = self.manager()
        jobs = manager.create(["one@example.test", "next@example.test"])["jobs"]
        self.waiting(manager)
        with mock.patch.object(manager, "_persist", side_effect=OSError(SECRET_COOKIE)):
            with self.assertRaisesRegex(RuntimeError, "login_state_write_failed"):
                manager.cancel(jobs[0]["id"])
        self.assertEqual("WAITING_FOR_CODE", manager.snapshot()["jobs"][0]["status"])
        # Keep the worker outside the lock until a queued verification's cancel
        # transaction has failed, then prove that verification still completes.
        with manager._cv:
            manager.submit_code(jobs[0]["id"], SECRET_CODE)
            with mock.patch.object(manager, "_persist", side_effect=OSError(SECRET_COOKIE)):
                with self.assertRaisesRegex(RuntimeError, "login_state_write_failed"):
                    manager.cancel(jobs[0]["id"])
        self.waiting(manager, 1)
        self.assertEqual("AUTHENTICATED", manager.snapshot()["jobs"][0]["status"])
        self.assertEqual([SECRET_CODE], self.driver.verifies)
        self.assertEqual(1, len(self.saved))
        self.assertNotIn(SECRET_COOKIE, json.dumps(manager.snapshot()))

    def test_close_write_failure_still_wakes_worker_and_closes_driver(self):
        manager = self.manager()
        manager.create(["one@example.test"])
        self.waiting(manager)
        with mock.patch.object(manager, "_persist", side_effect=OSError(SECRET_COOKIE)):
            manager.close()
        self.assertFalse(manager._thread.is_alive())
        self.assertEqual(1, self.driver.sessions_closed)
        self.assertEqual(1, self.driver.closed)
        self.assertTrue(manager.snapshot()["closed"])
        self.assertEqual("INTERRUPTED", manager.snapshot()["jobs"][0]["status"])
        self.assertEqual("LOGIN_STATE_WRITE_FAILED", manager.snapshot()["error_code"])
        self.assertNotIn(SECRET_COOKIE, json.dumps(manager.snapshot()))

    def test_close_requests_driver_stop_and_suppresses_late_authentication(self):
        stopped = threading.Event()

        def request_stop():
            stopped.set()
            self.driver.verify_release.set()

        self.driver.request_stop = request_stop
        self.driver.verify_release.clear()
        manager = self.manager()
        job = manager.create(["one@example.test"])["jobs"][0]
        self.waiting(manager)
        manager.submit_code(job["id"], SECRET_CODE)
        self.assertTrue(self.driver.verify_entered.wait(1))
        manager.close()
        self.assertTrue(stopped.is_set())
        self.assertFalse(manager._thread.is_alive())
        self.assertEqual([], self.saved)
        self.assertEqual(1, self.driver.sessions_closed)
        self.assertEqual(1, self.driver.closed)

    def test_input_validation_is_atomic_and_queue_limit_is_ten(self):
        manager = self.manager()
        for invalid in (None, "one@example.test", [], ["bad"], ["https://x@domain.test"], ["x/y@domain.test"], [r"x\y@domain.test"], ["<x>@domain.test"], ["a@b.test", " A@B.TEST "], ["line\n@example.test"], [None]):
            with self.assertRaises(ValueError):
                manager.create(invalid)
        self.assertEqual([], manager.snapshot()["jobs"])
        manager.create([f"job{index}+alias@example.test" for index in range(10)])
        self.waiting(manager)
        with self.assertRaisesRegex(ValueError, "login_queue_full"):
            manager.create(["overflow@example.test"])
        with self.assertRaisesRegex(ValueError, "invalid_code_format"):
            manager.submit_code(manager.snapshot()["jobs"][0]["id"], "12345")
        with self.assertRaisesRegex(ValueError, "login_job_not_found"):
            manager.cancel("not-a-job")
        self.assertEqual(0, manager.snapshot()["jobs"][0]["attempts"])
        manager.close()
        self.assertEqual({"INTERRUPTED"}, {row["status"] for row in manager.snapshot()["jobs"]})
        self.assertEqual(1, self.driver.closed)
        with self.assertRaisesRegex(ValueError, "login_manager_closed"):
            manager.create(["closed@example.test"])
        for values in ({"max_jobs": 11}, {"max_jobs": True}, {"otp_timeout": 0}):
            with self.assertRaises(ValueError):
                self.manager(**values)


if __name__ == "__main__":
    unittest.main()
