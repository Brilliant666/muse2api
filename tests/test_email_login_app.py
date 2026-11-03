"""In-process ASGI + fresh JSON Store + fake login driver; no HTTP or browser."""
from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HOME = tempfile.TemporaryDirectory(prefix="muse-email-app-import-")
os.environ.update(MUSE2API_HOME=HOME.name, MUSE2API_PROFILE_ROOT=HOME.name,
                  MUSE2API_KEY="email-test-key", MUSE_BACKEND="protocol")
import app
from config import Config
from store import Store
from email_login import LoginManager


async def request(method, path, body=None, authorization="Bearer email-test-key"):
    payload = json.dumps(body).encode() if body is not None else b""
    headers = [(b"content-type", b"application/json")]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "server": ("test", 80),
             "client": ("test", 1), "headers": headers}
    messages = []
    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}
    async def send(message):
        messages.append(message)
    await app.app(scope, receive, send)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    return status, json.loads(b"".join(m.get("body", b"") for m in messages))


class FakeManager:
    def __init__(self):
        self.calls = []
        self.closed = False
    def snapshot(self):
        return {"jobs": [{"id": "job-one", "email": "one@example.test", "status": "WAITING_FOR_CODE"}],
                "available": None, "active_job_id": "job-one", "max_jobs": 10}
    def create(self, emails):
        self.calls.append(("create", emails))
        return self.snapshot()
    def submit_code(self, job_id, code):
        if job_id == "missing":
            raise ValueError("login_job_not_found")
        if code == "987654":
            raise RuntimeError("untrusted-private-error-marker")
        self.calls.append(("code", job_id, code))
        return self.snapshot()
    def cancel(self, job_id):
        self.calls.append(("cancel", job_id))
        return self.snapshot()
    def close(self):
        self.closed = True


class FakeDriver:
    def __init__(self):
        self.calls = []
    def begin(self, email):
        self.calls.append(("begin", email))
        return {"status": "WAITING_FOR_CODE", "code_length": 6}
    def verify(self, code):
        self.calls.append(("verify", code))
        return authenticated()
    def close_session(self):
        self.calls.append(("close_session",))
    def close(self):
        self.calls.append(("close",))


def authenticated(principal="synthetic-principal-one", cookie="synthetic-cookie-one", matched=True):
    return {"status": "AUTHENTICATED", "principal_id": principal,
            "cookies": {"hatch_sess": cookie}, "expires": {},
            "challenge_email_matched": matched}


class EmailLoginAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="muse-email-app-case-")
        self.cfg = Config(base_dir=self.temp.name, backend="protocol", api_key="email-test-key")
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg)
        self.manager = FakeManager()
        self.patches = [mock.patch.object(app, "CFG", self.cfg),
                        mock.patch.object(app, "store", self.store),
                        mock.patch.object(app, "ACCOUNT_POOL", app.AccountPoolManager(self.store)),
                        mock.patch.object(app, "_EMAIL_LOGIN_MANAGER", self.manager),
                        mock.patch.object(app, "_email_login_available", return_value=True),
                        mock.patch("socket.create_connection", side_effect=AssertionError("network forbidden"))]
        for item in self.patches:
            item.start()
    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()
    def call(self, method, path, body=None, **kw):
        return asyncio.run(request(method, path, body, **kw))

    def test_all_routes_require_existing_bearer_auth(self):
        for method, path, body in [
            ("GET", "/admin/email-login/jobs", None),
            ("POST", "/admin/email-login/jobs", {"emails": ["one@example.test"]}),
            ("POST", "/admin/email-login/jobs/job-one/code", {"code": "123456"}),
            ("POST", "/admin/email-login/jobs/job-one/cancel", None),
        ]:
            for auth in (None, "Bearer wrong-key"):
                self.assertEqual(self.call(method, path, body, authorization=auth)[0], 401)
        self.assertEqual(self.manager.calls, [])

    def test_routes_forward_only_explicit_queue_actions_and_hide_code(self):
        base = "/admin/email-login/jobs"
        self.assertEqual(self.call("GET", base)[1]["available"], True)
        self.assertEqual(self.call("POST", base, {"emails": ["one@example.test"]})[0], 200)
        status, result = self.call("POST", base + "/job-one/code", {"code": "123456"})
        self.assertEqual(status, 200)
        self.assertNotIn("123456", json.dumps(result))
        self.assertEqual(self.call("POST", base + "/job-one/cancel")[0], 200)
        self.assertEqual(self.manager.calls, [("create", ["one@example.test"]),
                                            ("code", "job-one", "123456"), ("cancel", "job-one")])
        self.assertEqual(self.call("POST", base + "/missing/code", {"code": "123456"})[0], 404)
        status, failed = self.call("POST", base + "/job-one/code", {"code": "987654"})
        self.assertEqual(status, 409)
        self.assertNotIn("untrusted-private-error-marker", json.dumps(failed))
        self.assertNotIn("987654", json.dumps(failed))

    def test_validation_limits_and_unavailable_helper_preserve_cookie_import(self):
        base = "/admin/email-login/jobs"
        self.assertEqual(self.call("POST", base, {"emails": ["one@example.test"] * 11})[0], 422)
        status, invalid = self.call("POST", base + "/job-one/code", {"code": "private-invalid-code"})
        self.assertEqual(status, 422)
        self.assertNotIn("private-invalid-code", json.dumps(invalid))
        self.assertEqual(self.call("POST", base, {"emails": ["one@example.test"], "inbox_url": "https://forbidden.test"})[0], 422)
        with mock.patch.object(app, "_email_login_available", return_value=False):
            self.assertFalse(self.call("GET", base)[1]["available"])
            self.assertEqual(self.call("POST", base, {"emails": ["one@example.test"]})[0], 503)
            status, result = self.call("POST", "/admin/accounts", {"cookies": {"hatch_sess": "fake-old-import"}})
            self.assertEqual((status, result["count"]), (200, 1))
        self.assertEqual(self.manager.calls, [])

    def test_new_identity_atomic_import_and_same_identity_revision_cas(self):
        with mock.patch.object(self.store, "update_account", wraps=self.store.update_account) as update:
            imported = app._save_email_login_account("One@Example.Test", authenticated())
            update.assert_not_called()  # identity metadata and Cookie share the first write
        self.assertFalse(imported["generation_tested"])
        first = self.store.get_account(imported["id"])
        self.assertEqual(first["login_email"], "one@example.test")
        self.assertEqual(len(first["login_identity_hash"]), 64)
        self.assertIsNone(first["ok"])
        self.assertNotIn("synthetic-principal-one", Path(self.cfg.accounts_file).read_text())
        stable = app._email_identity_hash("synthetic-principal-one")
        self.assertEqual(stable, first["login_identity_hash"])
        updated = app._save_email_login_account("one@example.test", authenticated(cookie="synthetic-cookie-two"))
        self.assertEqual(updated["id"], imported["id"])
        self.assertEqual(len(self.store.accounts), 1)
        latest = self.store.get_account(imported["id"])
        self.assertEqual(latest["credential_revision"], first["credential_revision"] + 1)
        self.assertIsNone(latest["ok"])
        self.assertIn("生成能力尚未测试", latest["note"])
        self.assertFalse(self.store.persist_protocol_cookies(imported["id"], first["credential_revision"], {"hatch_sess": "stale-cookie"}))
        self.assertEqual(self.store.get_account(imported["id"])["cookies"]["hatch_sess"], "synthetic-cookie-two")

    def test_identity_conflict_or_unverified_challenge_cannot_change_accounts(self):
        app._save_email_login_account("one@example.test", authenticated())
        before = Path(self.cfg.accounts_file).read_bytes()
        for email, result in [
            ("one@example.test", authenticated(principal="different-principal")),
            ("two@example.test", authenticated()),
            ("one@example.test", authenticated(matched=False)),
            ("one@example.test", {**authenticated(), "principal_id": ""}),
        ]:
            with self.assertRaises(ValueError):
                app._save_email_login_account(email, result)
            self.assertEqual(Path(self.cfg.accounts_file).read_bytes(), before)

    def test_busy_quarantined_and_active_operation_never_replaced(self):
        aid = app._save_email_login_account("one@example.test", authenticated())["id"]
        app.ACCOUNT_POOL.busy_accounts.add(aid)
        with self.assertRaisesRegex(ValueError, "BUSY_OR_QUARANTINED"):
            app._save_email_login_account("one@example.test", authenticated(cookie="new-cookie"))
        app.ACCOUNT_POOL.busy_accounts.clear()
        self.store.update_account(aid, protocol_blocked="synthetic-unknown")
        before = Path(self.cfg.accounts_file).read_bytes()
        with self.assertRaisesRegex(ValueError, "BUSY_OR_QUARANTINED"):
            app._save_email_login_account("one@example.test", authenticated(cookie="new-cookie"))
        self.assertEqual(Path(self.cfg.accounts_file).read_bytes(), before)
        self.store.update_account(aid, protocol_blocked="")
        self.store.begin_protocol_operation("synthetic-operation", aid, "chat", "synthetic prompt")
        with self.assertRaisesRegex(ValueError, "BUSY_OR_QUARANTINED"):
            app._save_email_login_account("one@example.test", authenticated(cookie="new-cookie"))
        self.assertEqual(self.store.get_account(aid)["cookies"]["hatch_sess"], "synthetic-cookie-one")
        self.assertEqual(self.store.get_protocol_operation("synthetic-operation")["state"], "preparing")

    def test_real_manager_and_fake_driver_complete_without_backend_calls(self):
        driver = FakeDriver()
        manager = LoginManager(self.cfg.data_dir, lambda: driver, app._save_email_login_account)
        try:
            with mock.patch.object(app, "_EMAIL_LOGIN_MANAGER", manager):
                status, created = self.call("POST", "/admin/email-login/jobs", {"emails": ["one@example.test"]})
                self.assertEqual(status, 200)
                job_id = created["jobs"][0]["id"]
                deadline = time.monotonic() + 3
                while manager.snapshot()["jobs"][0]["status"] != "WAITING_FOR_CODE" and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(manager.snapshot()["jobs"][0]["status"], "WAITING_FOR_CODE")
                self.assertEqual(self.call("POST", "/admin/email-login/jobs/" + job_id + "/code", {"code": "123456"})[0], 200)
                while manager.snapshot()["jobs"][0]["status"] not in {"AUTHENTICATED", "FAILED"} and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(manager.snapshot()["jobs"][0]["status"], "AUTHENTICATED")
                public = json.dumps(self.call("GET", "/admin/email-login/jobs")[1])
                for secret in ("123456", "synthetic-cookie-one", "synthetic-principal-one"):
                    self.assertNotIn(secret, public)
                    self.assertNotIn(secret, (Path(self.cfg.data_dir) / "email-login-jobs.json").read_text())
                self.assertEqual(len(self.store.accounts), 1)
                self.assertEqual(self.store.protocol_operations, {})
                self.assertEqual(self.store.tasks, {})
                self.assertEqual(sum(call[0] == "begin" for call in driver.calls), 1)
        finally:
            manager.close()

    def test_lifecycle_builds_idle_manager_and_closes_it(self):
        # Startup's existing engine start is stubbed; the login factory stays lazy.
        with mock.patch.object(app, "_EMAIL_LOGIN_MANAGER", None), \
                mock.patch("email_login_browser.EmailLoginBrowser") as browser, \
                mock.patch.object(app.engine, "start"), \
                mock.patch.object(app.engine, "stop"), \
                mock.patch.object(app, "_keepalive_loop", new_callable=mock.AsyncMock):
            asyncio.run(app._startup())
            manager = app._EMAIL_LOGIN_MANAGER
            self.assertEqual(manager.snapshot()["jobs"], [])
            browser.assert_not_called()
            app._shutdown()
            self.assertTrue(manager.snapshot()["closed"])


class EmailLoginAdminDomTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for DOM behavior check")
    def test_queue_escapes_rows_keeps_in_progress_code_and_clears_before_submit(self):
        script = r"""
const fs=require('fs'), vm=require('vm'), assert=require('assert/strict');
const html=fs.readFileSync(process.argv[1],'utf8');
const start=html.indexOf('const EMAIL_LOGIN_TERMINAL =');
const end=html.indexOf('/* ================= 总览刷新',start);
assert(start>0&&end>start);
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.value='';this.textContent='';this.style={};this.disabled=false;}
 appendChild(node){node.parent=this;this.children.push(node);return node;}
 append(...nodes){nodes.forEach(node=>this.appendChild(node));}
 replaceChildren(...nodes){this.children=[];this.append(...nodes);}
 querySelector(selector){return selector==='[data-empty]'?this.children.find(node=>node.dataset.empty):undefined;}
 setAttribute(){}
 remove(){this.parent.children=this.parent.children.filter(node=>node!==this);}
 set innerHTML(value){throw Error('unsafe DOM HTML interpolation');}
}
const nodes=new Map(), get=id=>{if(!nodes.has(id))nodes.set(id,new Element('div'));return nodes.get(id);};
let codeCleared=false, calls=[];
const context={$:get,document:{createElement:tag=>new Element(tag)},t:value=>value,toast(){},
 api:async(path,opts)=>{calls.push([path,opts]);const input=get('emailLoginJobs').children[0].children[2].children[0].children[0];codeCleared=input.value==='';return {available:true,jobs:[{id:'job</script>',email:'<img src=x onerror=evil()>',status:'VERIFYING'}]};}};
vm.createContext(context);vm.runInContext(html.slice(start,end),context);
(async()=>{
 const snapshot={available:true,jobs:[{id:'job</script>',email:'<img src=x onerror=evil()>',status:'WAITING_FOR_CODE'}]};
 context.renderEmailLoginJobs(snapshot);
 let row=get('emailLoginJobs').children[0];
 assert.equal(row.children[0].textContent,snapshot.jobs[0].email);
 const form=row.children[2].children[0],input=form.children[0];
 assert.equal(input.type,'password');input.value='123456';
 context.renderEmailLoginJobs(snapshot);
 assert.equal(get('emailLoginJobs').children[0].children[2].children[0].children[0],input);
 assert.equal(input.value,'123456');
 await form.onsubmit({preventDefault(){}});
 assert(codeCleared);assert.equal(input.value,'');
 assert.equal(calls.length,1);assert(calls[0][0].includes(encodeURIComponent('job</script>')));
 assert.equal(JSON.parse(calls[0][1].body).code,'123456');
 assert(!html.slice(start,end).includes('localStorage'));
 assert(html.includes('id="newCookie"')); // original Cookie importer retained
 console.log('PASS email-login DOM: escaped text, stable polling, clear-before-submit, no OTP storage');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which("node"), "-e", script, str(ROOT / "admin.html")],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS email-login DOM", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
