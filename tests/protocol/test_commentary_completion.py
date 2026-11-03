"""Actual fixed SDK, explicit protocol commentary, no live network/Store."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from _paths import PROTOCOL_DIR, require_protocol_source
require_protocol_source()
sys.path.insert(0,str(PROTOCOL_DIR))
from muse2api.chat import _turn_lifecycle
from muse2api.gateway import ProtocolOperationError, RpcResponse
from test_media_completion import Clock, service

PROMPT="Reply with the synthetic final marker."
FINAL="SYNTHETIC_FINAL_MARKER"
COMMENTARY="Synthetic progress differs from the final answer."


class CommentaryPeer:
    def __init__(self,clock,*,metadata=None,only_commentary=False,final_only=False,
                 legacy=False,with_media=False,active_tool=False):
        self.clock=clock
        self.metadata=metadata if metadata is not None else {
            "message_kind":"commentary","recipient":"commentary","public_commentary":True,
            "root_message_execution_id":"synthetic-execution"}
        self.only_commentary,self.final_only=only_commentary,final_only
        self.legacy,self.with_media,self.active_tool=legacy,with_media,active_tool
        self.submits,self.reads=0,0
        self.posts=[]
        self.is_closed=False
    async def ack(self,method,path,body,**kwargs):
        self.submits+=1;self.posts.append(path);self.prompt=body["message"]
        return {"message_id":"synthetic-user"}
    async def request(self,method,path,params=None,**kwargs):
        if method=="POST":
            self.posts.append(path)
            return RpcResponse(200,[],b"{}")
        self.reads+=1
        events=[{"event_name":"message.user","message_id":"synthetic-user","text":self.prompt}]
        if not self.final_only:
            events.append({"event_name":"message.assistant","message_id":"synthetic-commentary",
                "payload":{"status":"completed","text":COMMENTARY,"metadata":self.metadata}})
            events.append({"event_name":"delta.message_done","payload":{}})
        if self.active_tool:
            events.append({"event_name":"tool.progress","payload":{"id":"synthetic-tool",
                "status":"completed" if self.clock.now>=4 else "running"}})
        if self.final_only or (not self.only_commentary and self.clock.now>=2):
            metadata={} if self.legacy else {
                "message_kind":"message","root_message_execution_id":"synthetic-execution"}
            events.append({"event_name":"message.assistant","message_id":"synthetic-final",
                "payload":{"status":"completed","text":FINAL,"metadata":metadata}})
            if self.with_media:
                events.append({"event_name":"delta.presentation","payload":{"kind":"image",
                    "images":[{"path":"workspace/synthetic-final.png","mime":"image/png"}]}})
        return RpcResponse(200,[],json.dumps({"chat_events":events}).encode())


class CommentaryCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scratch=tempfile.TemporaryDirectory(prefix="commentary-completion-offline-")
        self.root=Path(self.scratch.name)
        self.clock=Clock()
        self.time_patch=patch("muse2api.chat.time",self.clock)
        self.sleep_patch=patch("muse2api.chat.asyncio.sleep",self.clock.sleep)
        self.time_patch.start();self.sleep_patch.start()
    async def asyncTearDown(self):
        self.sleep_patch.stop();self.time_patch.stop();self.scratch.cleanup()

    async def test_each_explicit_commentary_field_waits_for_message_kind_reply(self):
        variants=[{"message_kind":"commentary"},{"recipient":"commentary"},{"public_commentary":True}]
        for n,metadata in enumerate(variants):
            with self.subTest(field=next(iter(metadata))):
                self.clock.now=0
                peer=CommentaryPeer(self.clock,metadata=metadata)
                result=await service(peer,self.root/str(n)).run(PROMPT,timeout=8,session_key_value="synthetic")
                self.assertEqual(result.text,FINAL)
                self.assertEqual(result.message_id,"synthetic-final")
                self.assertGreaterEqual(self.clock.now,2)
                self.assertEqual(peer.submits,1)

    async def test_nonprefix_commentary_never_reaches_chat_or_responses_sse_callback(self):
        peer=CommentaryPeer(self.clock)
        chunks=[]
        result=await service(peer,self.root).run(PROMPT,on_delta=chunks.append,
            timeout=8,session_key_value="synthetic-sse")
        self.assertEqual("".join(chunks),FINAL)
        self.assertNotIn(COMMENTARY,"".join(chunks))
        self.assertEqual(result.text,FINAL)
        self.assertEqual(result.message_id,"synthetic-final")
        self.assertEqual(peer.submits,1)
        self.assertTrue(any(e.get("message_id")=="synthetic-commentary" for e in result.events))

    async def test_only_commentary_times_out_accepted_and_emits_nothing(self):
        peer=CommentaryPeer(self.clock,only_commentary=True)
        chunks=[]
        with self.assertRaises(ProtocolOperationError) as caught:
            await service(peer,self.root).run(PROMPT,on_delta=chunks.append,
                timeout=6,session_key_value="synthetic-pending")
        self.assertEqual(caught.exception.code,"timeout")
        self.assertEqual(caught.exception.delivery,"accepted")
        self.assertFalse(caught.exception.retryable)
        self.assertTrue(caught.exception.session_id)
        self.assertEqual(chunks,[])
        self.assertGreaterEqual(self.clock.now,6)
        self.assertEqual(peer.submits,1)
        self.assertEqual(peer.posts.count("/chat/stream"),1)

    async def test_explicit_message_kind_can_complete_without_turn_completed(self):
        peer=CommentaryPeer(self.clock,final_only=True)
        result=await service(peer,self.root).run(PROMPT,timeout=6,session_key_value="synthetic-final")
        self.assertEqual(result.text,FINAL)
        self.assertEqual(result.message_id,"synthetic-final")
        self.assertLess(self.clock.now,1)

    async def test_legacy_without_metadata_keeps_completed_answer_behavior(self):
        peer=CommentaryPeer(self.clock,final_only=True,legacy=True)
        result=await service(peer,self.root).run(PROMPT,timeout=6,session_key_value="synthetic-legacy")
        self.assertEqual(result.text,FINAL)
        self.assertLess(self.clock.now,1)

    async def test_raw_commentary_events_and_media_are_retained(self):
        peer=CommentaryPeer(self.clock,with_media=True)
        result=await service(peer,self.root).run_with_media(PROMPT,timeout=8,
            session_key_value="synthetic-media")
        self.assertEqual(result.text,FINAL)
        self.assertEqual(result.message_id,"synthetic-final")
        self.assertEqual([r.path for r in result.media],["workspace/synthetic-final.png"])
        self.assertTrue(any(e.get("message_id")=="synthetic-commentary" for e in result.events))
        self.assertEqual(peer.submits,1)

    async def test_active_tool_still_blocks_completion_after_real_answer(self):
        peer=CommentaryPeer(self.clock,active_tool=True)
        result=await service(peer,self.root).run(PROMPT,timeout=8,session_key_value="synthetic-tool")
        self.assertEqual(result.text,FINAL)
        self.assertGreaterEqual(self.clock.now,4)
        self.assertEqual(peer.submits,1)

    async def test_commentary_done_and_message_done_do_not_end_wait_for_answer(self):
        events=[
            {"event_name":"message.assistant","payload":{"status":"completed",
                "metadata":{"message_kind":"commentary","public_commentary":True}}},
            {"event_name":"delta.message_done","payload":{}},
        ]
        active_or_pending,answer_done,turn_done=_turn_lifecycle(events)
        self.assertTrue(active_or_pending)
        self.assertFalse(answer_done)
        self.assertFalse(turn_done)


if __name__=="__main__":
    unittest.main()
