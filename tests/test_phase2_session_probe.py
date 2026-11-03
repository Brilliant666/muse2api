"""Optional marker observer on fake existing Runtime; no Store/browser/network."""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"tools"))
import phase2_server as server


class Sink:
    def __init__(self):self.events=[]
    def emit(self,event,**fields):self.events.append({"event":event,**fields})
    def stats(self):return {"enqueued":len(self.events),"written":len(self.events),"dropped":0,"writer_errors":0,"queued":0}


class Connection:
    is_closed=False
    _streams={}
    _receiver=None
    def __init__(self):
        self.requests=[];self.remote_status="running";self.response_session="synthetic-session"
        self.block=None;self.cancelled=0;self.close_calls=0;self.unknown_schema=False;self.response_status=200
    async def request(self,method,path,**kwargs):
        self.requests.append((method,path,kwargs))
        try:
            if self.block is not None:await self.block.wait()
        except asyncio.CancelledError:
            self.cancelled+=1;raise
        result={"session_id":self.response_session,"status":self.remote_status,
                "can_send_messages":self.remote_status=="completed","can_send_actions":True}
        data={"result":result if not self.unknown_schema else {"session_id":self.response_session,"session":{"status":self.remote_status}}}
        def check_status():
            if self.response_status >= 400:
                error=RuntimeError("synthetic session rejection");error.status=self.response_status;raise error
        return NS(status=self.response_status,json=lambda:data,raise_for_status=check_status)
    async def close(self):self.close_calls+=1


class SessionProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scratch=tempfile.TemporaryDirectory(prefix="phase2-session-probe-fake-")
        self.root=Path(self.scratch.name);self.controllers=[]
    async def asyncTearDown(self):
        for item in self.controllers:
            if item.session_probe is not None:await item.session_probe.close()
            item.restore()
        self.scratch.cleanup()
    def controller(self,name="probe",enabled=True,stop=None):
        conn=Connection()
        op={"id":"synthetic-operation","account_id":"synthetic-account","session_id":"synthetic-session",
            "vm_id":"synthetic-vm","prompt":"Acceptance marker PHASE2_D11_001. Synthetic prompt.","state":"running"}
        rt=NS(account_id=op["account_id"],revision=1,conn=conn,session=NS(vm_id=op["vm_id"]),http=NS(_client=object()))
        engine=NS(_active={op["id"]:op},_runtimes={op["account_id"]:rt},_account_locks={},_tasks=set(),stop=stop or (lambda:None))
        sink=Sink();kwargs={"session_probe_marker":"PHASE2_D11_001","probe_directory":self.root/name} if enabled else {}
        item=server.Instrumentation(NS(engine=engine),sink,{},**kwargs);self.controllers.append(item)
        return item,engine,op,rt,conn,sink

    async def test_disabled_and_nonmatching_markers_make_zero_rpc(self):
        item,engine,op,rt,conn,sink=self.controller(enabled=False)
        item.runtime_sample(0);await asyncio.sleep(0)
        self.assertIsNone(item.session_probe);self.assertEqual(conn.requests,[])
        self.assertEqual(list(self.root.iterdir()),[])
        item,engine,op,rt,conn,sink=self.controller("enabled")
        op["prompt"]="PHASE2_D11_001_EXTRA must not match a full marker."
        item.session_probe.tick(now=0);await asyncio.sleep(0)
        self.assertEqual(conn.requests,[])
        for marker in ("PHASE2_D11","PHASE2_D11_001/unsafe","PHASE2_D11_001;other"):
            with self.assertRaises(ValueError):server.SessionProbe(item,marker,self.root/"invalid")
        self.assertFalse((self.root/"invalid").exists())

    async def test_active_to_after_exact_binding_spacing_and_twelve_plus_one_cap(self):
        item,engine,op,rt,conn,sink=self.controller();probe=item.session_probe
        op.update(phase="download",delivery="completed")
        probe.tick(now=0);await probe.task
        probe.tick(now=4);self.assertEqual(len(conn.requests),1)
        for index in range(1,20):
            probe.tick(now=index*5)
            if probe.task is not None:await probe.task
        self.assertEqual(len(conn.requests),12)
        op["state"]="completed";engine._active.clear();conn.remote_status="completed"
        probe.tick(now=110);await probe.task
        for index in range(120,160,5):probe.tick(now=index)
        self.assertEqual(len(conn.requests),13)
        self.assertTrue(all(method=="GET" and path=="/api/session/list/synthetic-session" and kw["timeout"]<=10 for method,path,kw in conn.requests))
        captures=list((self.root/"probe").glob("*.json"));self.assertEqual(len(captures),13)
        after=json.loads((self.root/"probe/after-01.json").read_text())
        self.assertEqual(after["binding"]["operation_id"],op["id"])
        self.assertEqual(after["local_at_dispatch"],{"state":"completed","phase":"download","delivery":"completed"})
        self.assertEqual(after["local_at_return"],after["local_at_dispatch"])
        self.assertEqual(after["response"]["result"]["status"],"completed")
        events=[row for row in sink.events if row["event"]=="session_probe"]
        self.assertEqual((events[0]["remote_status"],events[-1]["remote_status"]),("running","completed"))
        self.assertTrue(all(row["remote_completed_is_not_cpu_idle"] for row in events))
        self.assertTrue(all(row["local_phase"]=="download" and row["delivery"]=="completed" for row in events))
        self.assertEqual(sum(row["phase"]=="after" for row in events),1)

    async def test_foreign_vm_no_rpc_and_foreign_response_session_never_success(self):
        item,engine,op,rt,conn,sink=self.controller("wrong-vm")
        rt.session.vm_id="foreign-vm";item.session_probe.tick(now=0);await asyncio.sleep(0)
        self.assertEqual(conn.requests,[])
        self.assertTrue(any(row.get("error_type")=="BindingMismatch" for row in sink.events))
        item,engine,op,rt,conn,sink=self.controller("wrong-session")
        conn.response_session="foreign-session";item.session_probe.tick(now=0);await item.session_probe.task
        self.assertEqual(len(conn.requests),1)
        self.assertFalse(any(row["event"]=="session_probe" for row in sink.events))
        self.assertTrue(any(row["event"]=="session_probe_error" for row in sink.events))
        self.assertTrue((self.root/"wrong-session/active-01.json").is_file())
        item,engine,op,rt,conn,sink=self.controller("unknown-schema")
        conn.unknown_schema=True;item.session_probe.tick(now=0);await item.session_probe.task
        self.assertFalse(any(row["event"]=="session_probe" for row in sink.events))
        self.assertTrue(any(row["event"]=="session_probe_unverified" for row in sink.events))
        item,engine,op,rt,conn,sink=self.controller("rate-limit")
        conn.response_status=429;item.session_probe.tick(now=0);await item.session_probe.task
        self.assertTrue(any(row["event"]=="session_probe_error" and row.get("upstream_status")==429 for row in sink.events))
        raw=json.loads((self.root/"rate-limit/active-01.json").read_text())
        error=json.loads((self.root/"rate-limit/active-01-error.json").read_text())
        self.assertEqual((raw["response_code"],error["upstream_status"]),(429,429))

    async def test_engine_stop_cancels_probe_before_original_loop_stop(self):
        observed=[]
        def original_stop():
            probe=item.session_probe
            observed.append((probe.closed,probe.task.done(),probe.task.cancelled(),conn.cancelled))
        item,engine,op,rt,conn,sink=self.controller(stop=original_stop)
        engine._loop=asyncio.get_running_loop();conn.block=asyncio.Event()
        item.session_probe.tick(now=0);await asyncio.sleep(0)
        self.assertFalse(item.session_probe.task.done())
        await asyncio.to_thread(engine.stop)
        self.assertEqual(observed,[(True,True,True,1)])
        # Later Sampler.close's shared helper is idempotent, with no extra RPC.
        await asyncio.to_thread(item.close_session_probe)
        self.assertEqual((len(conn.requests),conn.cancelled,len(observed)),(1,1,1))
        self.assertEqual(conn.close_calls,0)

    async def test_single_inflight_cancel_cleans_only_own_probe(self):
        item,engine,op,rt,conn,sink=self.controller();probe=item.session_probe
        conn.block=asyncio.Event();other_release=asyncio.Event()
        other=asyncio.create_task(other_release.wait())
        try:
            probe.tick(now=0);await asyncio.sleep(0)
            probe.tick(now=5);probe.tick(now=10)
            self.assertEqual(len(conn.requests),1)
            await probe.close()
            self.assertTrue(probe.task.done());self.assertEqual(conn.cancelled,1)
            self.assertFalse(other.done());self.assertEqual(conn.close_calls,0)
            self.assertEqual(engine._active,{op["id"]:op})
            probe.tick(now=20);self.assertEqual(len(conn.requests),1)
        finally:
            other_release.set();await other


if __name__=="__main__":unittest.main(verbosity=2)
