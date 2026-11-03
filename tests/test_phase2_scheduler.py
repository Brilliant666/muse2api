"""Public text-route scheduler regressions; synthetic accounts and no external I/O.

Run with this worktree's venv. The actual ASGI routes, scheduler, backend and JSON
Store participate; only protocol network/media boundaries are deterministic.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "tests/protocol/network_guard/sitecustomize.py"
spec = importlib.util.spec_from_file_location("phase2_scheduler_network_guard", GUARD)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
sys.path.insert(0, str(ROOT / "tests/protocol"))
from test_app_contract import AppContractTests, SyntheticOperationError


class TextSchedulerTests(AppContractTests):
    def setUp(self):
        super().setUp()
        self.cfg.max_concurrency = 3
        self.module.GEN_LOCK = self.module.ConcurrencyLock(3)
        self.accounts.extend(self.store.add_account({"hatch_sess": "synthetic-" + name}, name)
                             for name in ("account-c", "account-d", "account-e"))

    def exercise_free_account(self, endpoint, stream, *, held_failure=False):
        held = threading.Event()
        release = threading.Event()
        healthy_finished = threading.Event()
        holder_account = []
        selected_accounts = []
        output = {}
        original_service = self.protocol.service

        def service(settings, pool):
            original = original_service(settings, pool)
            original_run = original.run

            async def run(prompt, **kwargs):
                selected_accounts.append((prompt, settings.account_id))
                if "held-worker-marker" in prompt:
                    holder_account.append(settings.account_id)
                    held.set()
                    while not release.is_set():
                        await asyncio.sleep(0.005)
                    if held_failure:
                        self.protocol.operations.append({"kind": "chat", "account": settings.account_id,
                                                         "prompt": prompt})
                        raise SyntheticOperationError(delivery="accepted")
                return await original_run(prompt, **kwargs)

            original.run = run
            return original

        self.backend.service_factory = service

        def payload(marker):
            body = {"stream": stream, "timeout": 4}
            if endpoint == "/v1/responses":
                body["input"] = marker
            else:
                body["messages"] = [{"role": "user", "content": marker}]
            return body

        def request_held():
            try:
                output["held"] = self.request("POST", endpoint, payload=payload("held-worker-marker"))
            except BaseException as exc:
                output["held_error"] = exc

        def request_healthy():
            try:
                output["healthy"] = self.request("POST", endpoint, payload=payload("healthy-free-marker"))
            except BaseException as exc:
                output["healthy_error"] = exc
            finally:
                healthy_finished.set()

        holder = threading.Thread(target=request_held, name="phase2-held-text", daemon=True)
        follower = threading.Thread(target=request_healthy, name="phase2-free-text", daemon=True)
        holder.start()
        try:
            self.assertTrue(held.wait(2), "first real ASGI request did not enter the synthetic protocol")
            self.assertEqual(holder_account, [self.accounts[0]["id"]])
            # A was selected before B-E completed newer work. It remains busy,
            # but is now oldest in the LRU timestamps when the next route runs.
            now = time.time()
            for index, account in enumerate(self.accounts):
                self.store.update_account(account["id"], last_used=now - (10 if index == 0 else 1))
            follower.start()
            self.assertTrue(healthy_finished.wait(1.5),
                "public text request waited for busy account A although B-E were free and capacity remained")
            self.assertNotIn("healthy_error", output)
            self.assertEqual(output["healthy"].status_code, 200, output["healthy"].text)
            self.assertEqual(len(selected_accounts), 2)
            self.assertNotEqual(selected_accounts[0][1], selected_accounts[1][1],
                "the new text request must acquire an idle account atomically")
            self.assertFalse(release.is_set(), "held task was released before idle-account assertion")
            self.assertEqual(self.module.ACCOUNT_POOL.busy_accounts, {self.accounts[0]["id"]})
        finally:
            release.set()
            holder.join(5)
            if follower.ident is not None:
                follower.join(5)
        self.assertFalse(holder.is_alive())
        self.assertFalse(follower.is_alive())
        self.assertNotIn("held_error", output)
        self.assertFalse(self.module.ACCOUNT_POOL.busy_accounts)
        self.assertFalse(self.module.GEN_LOCK.locked())
        self.assertEqual(len(self.store.protocol_operations), 2)
        self.assertEqual(output["held"].status_code, 502 if held_failure else 200)
        self.assertEqual({op["account_id"] for op in self.store.protocol_operations.values()},
                         {account_id for _, account_id in selected_accounts})
        self.assertEqual(sorted(a.get("use_count", 0) for a in self.store.accounts), [0, 0, 0, 1, 1],
                         "HTTP admission must not count a selection before lease ownership")

    def test_chat_sync_uses_free_account_while_oldest_is_busy(self):
        self.exercise_free_account("/v1/chat/completions", False)

    def test_chat_stream_uses_free_account_while_oldest_is_busy(self):
        self.exercise_free_account("/v1/chat/completions", True)

    def test_responses_sync_uses_free_account_while_oldest_is_busy(self):
        self.exercise_free_account("/v1/responses", False)

    def test_responses_stream_uses_free_account_while_oldest_is_busy(self):
        self.exercise_free_account("/v1/responses", True)


    def test_unknown_account_does_not_block_healthy_text_requests(self):
        self.exercise_free_account("/v1/chat/completions", False, held_failure=True)
        failed_account = self.store.get_account(self.accounts[0]["id"])
        self.assertTrue(failed_account.get("protocol_blocked"))
        blocked_operation = self.store.get_protocol_operation(failed_account["protocol_blocked"])
        self.assertEqual(blocked_operation["state"], "unknown")
        response = self.request("POST", "/v1/chat/completions", payload={
            "messages": [{"role": "user", "content": "healthy-after-unknown"}], "timeout": 4})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.protocol.operations), 3, "an unknown turn must not be resubmitted")
        self.assertNotEqual(self.protocol.operations[-1]["account"], failed_account["id"])
        self.assertEqual(self.store.get_protocol_operation(blocked_operation["id"])["state"], "unknown")
        self.assertEqual(len(self.store.protocol_operations), 3)
        self.assertFalse(self.module.ACCOUNT_POOL.busy_accounts)

    def test_no_account_keeps_http_admission_error_without_a_submission(self):
        for account in self.accounts:
            self.store.update_account(account["id"], enabled=False)
        for endpoint in ("/v1/chat/completions", "/v1/responses"):
            for stream in (False, True):
                body = {"stream": stream, "timeout": 4}
                body.update({"input": "none"} if endpoint == "/v1/responses" else
                            {"messages": [{"role": "user", "content": "none"}]})
                with self.subTest(endpoint=endpoint, stream=stream):
                    self.assertEqual(self.request("POST", endpoint, payload=body).status_code, 400)
        self.assertFalse(self.store.protocol_operations)
        self.assertFalse(self.protocol.operations)
        self.assertFalse(self.module.ACCOUNT_POOL.busy_accounts)
        self.assertFalse(self.module.GEN_LOCK.locked())


NAMES = [name for name in TextSchedulerTests.__dict__ if name.startswith("test_")]


def load_tests(loader, tests, pattern):
    # Reuse the existing contract fixture without silently rerunning inherited
    # suites or claiming their historical cases as new phase-two validation.
    return unittest.TestSuite(TextSchedulerTests(name) for name in NAMES)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(load_tests(None, None, None))
    raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)
