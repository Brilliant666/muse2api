"""Controlled Linux-preflight boundaries only; no real Store/HTTP/browser."""
from __future__ import annotations

from contextlib import asynccontextmanager, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools import phase2_linux_preflight as tool

SALT=b"synthetic-private-round-salt-only!"
UID="1000000000000001"


def html(uid=UID):
    rows=[{"viewerId":uid,"isAuthenticated":True},
          {"prelude":{"user":{"kind":"user","identifier":uid}}}]
    flight="".join(f"{i+17:x}:"+json.dumps(row)+"\n" for i,row in enumerate(rows))
    return "<script>self.__next_f.push("+json.dumps([1,flight])+")</script>"


def evidence():
    _,digest=tool.identity_from_html(html(),SALT)
    row={"account_id":"account-1","alias":"pool-01","enabled":True,"identity_verified":True,
        "upstream_identity_sha256":digest,"identity_hash_scheme":"round-private HMAC-SHA256",
        "quota_current":True,"cookies_persisted":True,"checked_at":"2026-11-03T00:00:00Z",
        "quota":{"weekly_used_pct":0,"weekly_remaining_pct":100}}
    report={"status":"PASS","upstream_identity_verified":True,"all_current_quotas":True,
            "all_have_weekly_remaining":True,"all_cookies_persisted":True,
            "identity_observed_platform":"Windows isolated browser","accounts":[row]}
    return [{"account_id":"account-1","alias":"pool-01"}],report,row


class Native:
    def __init__(self):
        self.calls=[]
        self.closed=False
        self.cookies=SimpleNamespace(jar=[])
        self.home_html=html()
        self.home_status=200
        self.content_type="text/html; charset=utf-8"
        self.session={"status":"assigned","vm_state":"RUNNING","vm_id":"synthetic-private-vm"}
    async def request(self,method,url,**kwargs):
        self.calls.append((method,url,kwargs))
        is_session=url.endswith("/api/session")
        return SimpleNamespace(status_code=200 if is_session else self.home_status,url=url,
            headers={"content-type":"application/json" if is_session else self.content_type},
            text=json.dumps(self.session) if is_session else self.home_html,
            json=lambda:self.session)
    async def close(self):
        self.closed=True


class HTTP:
    def __init__(self,native):
        self._client=native
        self.merges=0
        self.cas_fail=False
    @property
    def client(self):
        return self._client
    def _merge_cookies(self):
        self.merges+=1
        if self.cas_fail:
            raise RuntimeError("synthetic CAS revision changed")
    async def session_info(self):
        try:
            response=await self.client.request("GET","https://muse.ai/api/session",
                params={"vm_state":"live"},json=None,timeout=60)
        finally:
            self._merge_cookies()
        return response.json()


class Backend:
    def __init__(self,native):
        self.runtime=SimpleNamespace(http=HTTP(native),
            settings=SimpleNamespace(impersonate="chrome",user_agent=None),conn=None)
        self._runtimes={"account-1":self.runtime}
        self.acquires=self.releases=self.closes=0
        self.close_fail=False
    @asynccontextmanager
    async def _owned_runtime(self,aid):
        if aid!="account-1": raise AssertionError("foreign identity")
        self.acquires+=1
        try: yield self.runtime
        finally:self.releases+=1
    async def _close_runtime(self,runtime):
        self.closes+=1
        if self.close_fail:raise RuntimeError("synthetic cleanup failure")
        await runtime.http.client.close()


class LinuxPreflightPureTests(unittest.TestCase):
    def setUp(self):
        self.scratch=tempfile.TemporaryDirectory(prefix="linux-preflight-fake-")
        self.root=Path(self.scratch.name)
    def tearDown(self):
        self.scratch.cleanup()
    def test_import_does_not_construct_store_config_or_access_network(self):
        spec=importlib.util.spec_from_file_location("offline_linux_preflight",ROOT/"tools/phase2_linux_preflight.py")
        module=importlib.util.module_from_spec(spec)
        before=dict(os.environ)
        with patch.dict(sys.modules,{"config":None,"store":None,"protocol_backend":None}):
            spec.loader.exec_module(module)
        self.assertEqual(dict(os.environ),before)
    def test_windows_pass_same_alias_id_and_hmac_is_required(self):
        mapping,report,row=evidence()
        self.assertEqual(tool.validate_windows_evidence(mapping,report),{"account-1":row})
        for key in ("status","upstream_identity_verified","all_current_quotas","all_cookies_persisted"):
            changed={**report,key:False}
            with self.assertRaises(tool.PreflightValidationError):
                tool.validate_windows_evidence(mapping,changed)
        report["accounts"]=[{**row,"alias":"foreign"}]
        with self.assertRaises(tool.PreflightValidationError):
            tool.validate_windows_evidence(mapping,report)
    def test_windows_cookies_or_vm_are_not_identity_substitutes(self):
        mapping,report,row=evidence()
        report["accounts"]=[{**row,"identity_verified":False,"cookies":"fake","vm":"fake"}]
        with self.assertRaises(tool.PreflightValidationError):
            tool.validate_windows_evidence(mapping,report)
    def test_preflight_lock_is_exclusive_and_cleans_only_own_lock(self):
        with tool.exclusive_preflight(self.root):
            self.assertTrue((self.root/"linux-preflight.lock").exists())
            with self.assertRaises(FileExistsError):
                with tool.exclusive_preflight(self.root):pass
            self.assertTrue((self.root/"linux-preflight.lock").exists())
        self.assertFalse((self.root/"linux-preflight.lock").exists())
    def test_cli_rejects_non_linux_before_store_or_private_input_reads(self):
        output=io.StringIO()
        with patch.object(tool.sys,"platform","win32"),redirect_stdout(output):
            self.assertEqual(tool.main(["--private-root",str(self.root)]),2)
        self.assertEqual(json.loads(output.getvalue()),
            {"alias":None,"status":"BLOCKED","error_type":"PreflightValidationError"})
    def test_cfg_must_select_private_protocol_store(self):
        cfg=SimpleNamespace(backend="protocol",base_dir=str(self.root),
            data_dir=str(self.root/"data"),media_dir=str(self.root/"data/media"))
        tool.validate_linux_cfg(cfg,self.root)
        cfg.backend="browser"
        with self.assertRaises(tool.PreflightValidationError):tool.validate_linux_cfg(cfg,self.root)
        cfg.backend="protocol";cfg.data_dir=str(self.root.parent/"foreign")
        with self.assertRaises(tool.PreflightValidationError):tool.validate_linux_cfg(cfg,self.root)


class LinuxPreflightAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scratch=tempfile.TemporaryDirectory(prefix="linux-preflight-http-fake-")
        self.root=Path(self.scratch.name)
        self.native=Native()
        self.backend=Backend(self.native)
        self.mapping,self.windows,self.expected=evidence()
    async def asyncTearDown(self):
        self.scratch.cleanup()
    async def inspect(self):
        return await tool.inspect_account(self.backend,self.mapping[0],self.expected,SALT,self.root,lambda _:{"Accept":"*/*"})
    async def test_get_gate_rejects_every_post_unknown_url_body_and_query(self):
        gate=tool.GetOnlyClient(self.native)
        cases=[("POST","https://muse.ai/api/hatch/vm/wake",{}),
               ("POST","https://muse.ai/chat/stream",{}),
               ("GET","https://evil.test/",{}),
               ("GET","https://muse.ai/?generate=1",{}),
               ("GET","https://muse.ai/",{"json":{"message":"fake"}}),
               ("GET","https://muse.ai/api/session",{"params":{"other":"value"}})]
        for method,url,kwargs in cases:
            with self.assertRaises(tool.GetOnlyError):await gate.request(method,url,**kwargs)
        self.assertEqual(self.native.calls,[])
        self.assertEqual(gate.blocked_attempts,len(cases))
    async def test_get_gate_overrides_redirects_and_bounds_timeout(self):
        gate=tool.GetOnlyClient(self.native)
        await gate.request("GET","https://muse.ai/",allow_redirects=True,timeout=100)
        kwargs=self.native.calls[0][2]
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["max_redirects"],0)
        self.assertEqual(kwargs["timeout"],20)
    async def test_two_native_gets_match_windows_identity_and_close_everything(self):
        result=await self.inspect()
        self.assertEqual(result["status"],"PASS")
        self.assertTrue(result["windows_identity_match"])
        self.assertTrue(result["cookies_persisted"])
        self.assertEqual(self.backend.runtime.http.merges,2)
        self.assertEqual([(m,u) for m,u,_ in self.native.calls],
            [("GET","https://muse.ai/api/session"),("GET","https://muse.ai/")])
        self.assertEqual(self.backend.releases,1)
        self.assertEqual(self.backend.closes,1)
        self.assertTrue(self.native.closed)
        self.assertEqual(self.backend._runtimes,{})
        self.assertFalse(result["noise_gateway_tested"])
        self.assertFalse(result["linux_quota_queried"])
        self.assertEqual(result["quota_evidence"]["platform"],"windows")
        self.assertNotIn("quota_current",result)
        self.assertTrue((self.root/"identity-bootstrap.html").exists())
    async def test_unassigned_vm_remains_unassigned_no_wake_or_running_claim(self):
        self.native.session={"status":"pending","vm_state":None}
        result=await self.inspect()
        self.assertEqual(result["status"],"PASS")
        self.assertEqual(result["workspace_status"],"pending")
        self.assertIsNone(result["vm_state"])
        self.assertFalse(result["noise_gateway_tested"])
        self.assertTrue(all(m=="GET" for m,_,_ in self.native.calls))
    async def test_principal_mismatch_blocks_and_still_closes(self):
        self.native.home_html=html("1000000000000002")
        result=await self.inspect()
        self.assertEqual(result["status"],"BLOCKED")
        self.assertEqual(result["error_type"],"IdentityMismatch")
        self.assertFalse(result["identity_verified"])
        self.assertTrue(self.native.closed)
    async def test_redirect_response_never_becomes_identity_pass(self):
        self.native.home_status=302
        result=await self.inspect()
        self.assertEqual(result["status"],"BLOCKED")
        self.assertEqual(result["error_type"],"GetOnlyError")
        self.assertFalse(result["identity_verified"])
        self.assertTrue(self.native.closed)
    async def test_cookie_cas_failure_blocks_without_further_requests(self):
        self.backend.runtime.http.cas_fail=True
        result=await self.inspect()
        self.assertEqual(result["status"],"BLOCKED")
        self.assertFalse(result["cookies_persisted"])
        self.assertEqual(len(self.native.calls),1)
        self.assertTrue(self.native.closed)
    async def test_cleanup_failure_keeps_runtime_for_backend_final_shutdown(self):
        self.backend.close_fail=True
        result=await self.inspect()
        self.assertEqual(result["status"],"BLOCKED")
        self.assertEqual(result["cleanup_error_type"],"RuntimeError")
        self.assertIn("account-1",self.backend._runtimes)


if __name__=="__main__":
    unittest.main()
