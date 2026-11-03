"""Controlled history route/identity/lifecycle checks. No Store or network."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tools"), str(ROOT)]
import phase2_history_probe as probe

PROTO = NS(lb_host="hatch.metaaivm.com", ws_path="/v1/noise",
           routes={"chat.history": NS(path="/chat/history", http_method="GET")})
PROMPT = "Acceptance marker PHASE2_L2_011. A simple blue circle on white."
OP = {"id":"synthetic-op", "account_id":"synthetic-account", "session_id":"synthetic-session",
      "vm_id":"synthetic-vm", "message_id":None, "prompt":PROMPT, "state":"completed", "delivery":"completed"}
RECORD = {"case_id":"L2-011", "operation_id":OP["id"], "account_id":OP["account_id"],
          "session_id":OP["session_id"], "vm_id":OP["vm_id"], "message_id":None,
          "marker":"PHASE2_L2_011", "account_alias":"pool-02", "operation_binding_verified":True}


def history(prompt=PROMPT, session=OP["session_id"], complete=False):
    events=[{"event_name":"message.user", "message_id":"synthetic-user", "payload":{"text":prompt,"session_id":session}},
            {"event_name":"message.assistant", "message_id":"synthetic-assistant", "payload":{"text":"synthetic assistant text", "active":not complete, "status":"completed" if complete else "running", "session_id":session}}]
    if complete: events.append({"event_name":"turn.completed", "payload":{"completed":True,"status":"completed"}})
    return {"result":{"events":events}}


class Reply:
    status_code=200
    def __init__(self,data): self.data=data
    def json(self): return copy.deepcopy(self.data)
    def raise_for_status(self): pass


class Native:
    def __init__(self,state="RUNNING"):
        self.info={"status":"assigned","vm_id":OP["vm_id"],"vm_state":state}
        self.requests=[]; self.websockets=[]
    async def request(self, method, url, **kwargs):
        self.requests.append((method,url,kwargs)); return Reply(self.info)
    async def ws_connect(self,url,**kwargs):
        self.websockets.append(url); return object()
    async def close(self): pass


class HTTP:
    def __init__(self,state="RUNNING"):
        self.native=Native(state); self._client=self.native; self.bootstrap_forces=[]
    @property
    def client(self): return self._client
    async def session_info(self):
        return (await self.client.request("GET","https://muse.ai/api/session",params={"vm_state":"live"})).json()
    async def bootstrap(self,force=False):
        self.bootstrap_forces.append(force)
        await self.client.request("POST","https://muse.ai/api/hatch/vm/wake",json={"vm_id":OP["vm_id"],"retry_count":1,"connect_attempt_id":"synthetic-connect"})
        self.native.info["vm_state"]="RUNNING"
        await self.client.request("POST","https://muse.ai/api/hatch/token",json={"vmName":OP["vm_id"],"vmAddress":"synthetic-endpoint"})
        return NS(vm_id=OP["vm_id"])


class Connection:
    def __init__(self,data): self.data=data; self.frames=[]
    async def _send_stream_frames(self,stream_id,**kwargs): self.frames.append(kwargs)
    async def request(self,method,path,params,**kwargs):
        await self._send_stream_frames(1,method=method,path=path+"?"+urlencode(params),body=b"",json_body=False,service="daemon")
        return Reply(self.data)


class Backend:
    def __init__(self,data=None,state="RUNNING",vm=OP["vm_id"]):
        self.http=HTTP(state); self.conn=Connection(data if data is not None else history())
        async def connect():
            self.connect_count+=1
            return object(),NS(vm_id=vm),self.conn
        self.rt=NS(http=self.http,settings=NS(ws_fallback=True),pool=NS(connect=connect),expected_vm=None)
        self.connect_count=0; self.owned=0
    @asynccontextmanager
    async def _owned_runtime(self,aid,allow_blocked=False):
        assert aid==OP["account_id"] and allow_blocked is True
        self.owned+=1; yield self.rt
    def recover_operation(self,*args): raise AssertionError("Completed recover is forbidden")
    def run(self,*args): raise AssertionError("Generation is forbidden")


class HistoryProbeTests(unittest.TestCase):
    def test_frame_guard_only_one_original_session_get(self):
        async def check():
            conn=Connection(history())
            with probe.FrameGuard(Connection,"/chat/history",OP["session_id"]) as gate:
                for method,path,body in [("POST","/chat/stream",b"x"),("GET","/model",b""),
                        ("GET","/chat/history?session_id=foreign&limit=60",b""),
                        ("GET","/chat/history?session_id=synthetic-session&limit=61",b"")]:
                    with self.assertRaises(probe.ProbeError):
                        await conn._send_stream_frames(1,method=method,path=path,body=body,json_body=False,service="daemon")
                self.assertEqual(conn.frames,[])
                await conn.request("GET","/chat/history",{"session_id":OP["session_id"],"limit":60})
                with self.assertRaises(probe.ProbeError): await conn.request("GET","/chat/history",{"session_id":OP["session_id"],"limit":60})
                self.assertEqual(len(conn.frames),1);self.assertEqual(len(gate.calls),1)
        asyncio.run(check())

    def test_http_guard_allowlist_and_original_vm(self):
        async def check():
            native=Native();gate=probe.HTTPGuard(native,OP["vm_id"],PROTO)
            await gate.request("GET","https://muse.ai/api/session",params={"vm_state":"live"})
            for method,url,kwargs in [("POST","https://muse.ai/api/chat",{"json":{}}),
                ("POST","https://muse.ai/api/hatch/vm/wake",{"json":{"vm_id":"foreign"}}),
                ("POST","https://muse.ai/api/hatch/token",{"json":{"vmName":"foreign"}}),
                ("GET","https://other.invalid/api/session",{"params":{"vm_state":"live"}})]:
                with self.assertRaises(probe.ProbeError):await gate.request(method,url,**kwargs)
            self.assertEqual(len(native.requests),1)
            self.assertFalse(native.requests[0][2]["allow_redirects"])
            good="wss://hatch.metaaivm.com/v1/noise?vm_id=synthetic-vm&auth_token=synthetic-token"
            await gate.ws_connect(good)
            with self.assertRaises(probe.ProbeError):await gate.ws_connect(good.replace("synthetic-vm","foreign"))
            self.assertEqual(len(native.websockets),1)
        asyncio.run(check())

    def test_binding_rejects_changed_operation_vm_or_session(self):
        self.assertEqual(probe.check_bindings(RECORD,OP,dict(OP)),OP)
        for field in ("id","account_id","session_id","vm_id","prompt"):
            changed={**OP,field:"foreign"}
            with self.assertRaises(probe.ProbeError):probe.check_bindings(RECORD,OP,changed)
        with self.assertRaises(probe.ProbeError):probe.check_bindings({**RECORD,"operation_binding_verified":False},OP,OP)

    def test_running_completed_operation_reads_history_without_recover_or_generation(self):
        with tempfile.TemporaryDirectory(prefix="muse-history-fake-") as tmp:
            backend=Backend()
            with probe.FrameGuard(Connection,"/chat/history",OP["session_id"]) as gate:
                result=asyncio.run(probe.inspect_history(backend,RECORD,OP,PROTO,Path(tmp),gate))
            self.assertEqual(result["status"],"READ_VERIFIED")
            self.assertTrue(result["original_prompt_matches"])
            self.assertFalse(result["original_message_id_observed"]) # null is allowed
            self.assertEqual(result["wake_status"],"NOT_OBSERVED")
            self.assertEqual(backend.http.bootstrap_forces,[])
            self.assertEqual((backend.owned,backend.connect_count,len(backend.conn.frames)),(1,1,1))
            self.assertEqual(result["generation_requests"],0)
            self.assertFalse(result["recover_called"])
            self.assertTrue(result["original_turn_active"])
            self.assertFalse(result["original_turn_done"])
            self.assertTrue((Path(tmp)/"history.json").is_file())
            self.assertEqual(result["media_reference_count"],0)

    def test_explicit_sleep_uses_force_bootstrap_running_and_original_history(self):
        with tempfile.TemporaryDirectory(prefix="muse-history-wake-fake-") as tmp:
            backend=Backend(state="SUSPENDED")
            with probe.FrameGuard(Connection,"/chat/history",OP["session_id"]) as gate:
                result=asyncio.run(probe.inspect_history(backend,RECORD,OP,PROTO,Path(tmp),gate))
            self.assertEqual(backend.http.bootstrap_forces,[True])
            self.assertEqual(result["wake_status"],"PASS")
            self.assertEqual(result["wake_request_count"],1)
            self.assertEqual(result["after_vm_state"],"RUNNING")
            self.assertEqual(result["generation_requests"],0)
            self.assertEqual(len(backend.conn.frames),1)
            self.assertTrue((Path(tmp)/"session-after-bootstrap.json").is_file())

    def test_observed_sleep_followed_by_failure_or_reassignment_is_not_not_observed(self):
        for reassigned in (False,True):
            with self.subTest(reassigned=reassigned), tempfile.TemporaryDirectory(prefix="muse-history-wake-failure-fake-") as tmp:
                backend=Backend(state="SUSPENDED",vm="foreign" if reassigned else OP["vm_id"])
                if not reassigned:
                    async def failed_history(*args,**kwargs): raise ConnectionError("synthetic history read failure")
                    backend.conn.request=failed_history
                with probe.FrameGuard(Connection,"/chat/history",OP["session_id"]) as gate:
                    with self.assertRaises((probe.ProbeError,ConnectionError)):
                        asyncio.run(probe.inspect_history(backend,RECORD,OP,PROTO,Path(tmp),gate))
                result=json.loads((Path(tmp)/"read-result.json").read_text())
                self.assertEqual(result["wake_status"],"VM_REASSIGNED" if reassigned else "FAIL")
                self.assertEqual(result["wake_request_count"],1)
                self.assertEqual(result["generation_requests"],0)
                self.assertEqual(backend.http.bootstrap_forces,[True])

    def test_vm_and_foreign_session_or_prompt_rejected(self):
        for backend in (Backend(vm="foreign"),Backend(data=history(session="foreign")),Backend(data=history(prompt="different prompt"))):
            with tempfile.TemporaryDirectory(prefix="muse-history-reject-fake-") as tmp:
                with probe.FrameGuard(Connection,"/chat/history",OP["session_id"]) as gate:
                    with self.assertRaises(probe.ProbeError):asyncio.run(probe.inspect_history(backend,RECORD,OP,PROTO,Path(tmp),gate))
                self.assertFalse(any(frame["method"]=="POST" for frame in backend.conn.frames))
        for state in ("RUNNING","STARTING","WAKING",""):
            self.assertNotIn(state,probe.SLEEP_STATES)

    def test_raw_lifecycle_kept_without_inferring_terminal_from_text(self):
        active=probe.summarize(history(),OP,RECORD)
        done=probe.summarize(history(complete=True),OP,RECORD)
        self.assertTrue(active["original_turn_active"]);self.assertFalse(active["original_turn_done"])
        self.assertFalse(done["original_turn_active"]);self.assertTrue(done["original_turn_done"])
        self.assertTrue(any(x["value"] is True for x in done["raw_lifecycle_observations"]))
        self.assertEqual(done["original_turn_event_names"].get("turn.completed"),1)
        self.assertNotIn("synthetic assistant text",json.dumps(done))


if __name__=="__main__":unittest.main(verbosity=2)
