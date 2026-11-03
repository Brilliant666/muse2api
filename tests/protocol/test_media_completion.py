"""Actual pinned SDK + delayed history peer; no network/credentials/app Store."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from _paths import PROTOCOL_DIR, require_protocol_source
require_protocol_source()
sys.path.insert(0,str(PROTOCOL_DIR))
from muse2api.chat import ChatService, _turn_lifecycle
from muse2api.gateway import ProtocolOperationError, RpcResponse
from muse2api.protocol import ProtocolConfig

_REAL_SLEEP = asyncio.sleep
PROMPT = "Create one synthetic blue circle image."


class Clock:
    def __init__(self):
        self.now=0.0
    def monotonic(self):
        return self.now
    def time(self):
        return 1700000000+self.now
    async def sleep(self,seconds):
        self.now+=seconds
        await _REAL_SLEEP(0)


class Peer:
    def __init__(self,clock,*,late_media=False,weak_refusal=False,strong_terminal=False,ready_media=False):
        self.clock=clock
        self.late_media,self.weak_refusal=late_media,weak_refusal
        self.strong_terminal,self.ready_media=strong_terminal,ready_media
        self.submits,self.reads=0,0
        self.posts=[]
        self.is_closed=False
    async def ack(self,method,path,body,**kwargs):
        self.submits+=1
        self.posts.append(path)
        self.prompt=body["message"]
        return {"message_id":"synthetic-user-1"}
    async def request(self,method,path,params=None,**kwargs):
        if method=="POST":
            self.posts.append(path)
            return RpcResponse(200,[],b"{}")
        self.reads+=1
        text="cannot generate yet; the render is still being prepared" if self.weak_refusal else "Preparing the requested image"
        events=[
            {"event_name":"message.user","message_id":"synthetic-user-1","text":self.prompt},
            {"event_name":"message.assistant","message_id":"synthetic-assistant-1",
             "payload":{"status":"completed","metadata":{"streaming":False},"text":text}},
        ]
        if self.ready_media or (self.late_media and self.clock.now>=6):
            events.extend([
                {"event_name":"message.assistant","message_id":"synthetic-assistant-2",
                 "payload":{"status":"completed","metadata":{"streaming":False},"text":"Image ready"}},
                {"event_name":"delta.presentation","payload":{"kind":"image","images":[
                    {"path":"workspace/synthetic-circle.png","mime":"image/png"}]}},
            ])
        if self.strong_terminal:
            events.append({"event_name":"turn.completed","payload":{}})
        return RpcResponse(200,[],json.dumps({"chat_events":events}).encode())


def service(peer,directory):
    @asynccontextmanager
    async def connection():
        yield SimpleNamespace(name="synthetic-account"),None,peer
    settings=SimpleNamespace(state_dir=directory,approval_policy="off",approval_ask="off",
                             timezone="UTC",chat_timeout=12,session_mode="thread")
    return ChatService(settings,SimpleNamespace(connection=connection),ProtocolConfig.load())


class MediaCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scratch=tempfile.TemporaryDirectory(prefix="media-completion-offline-")
        self.root=Path(self.scratch.name)
        self.clock=Clock()
        self.time_patch=patch("muse2api.chat.time",self.clock)
        self.sleep_patch=patch("muse2api.chat.asyncio.sleep",self.clock.sleep)
        self.time_patch.start();self.sleep_patch.start()
    async def asyncTearDown(self):
        self.sleep_patch.stop();self.time_patch.stop();self.scratch.cleanup()

    async def test_completed_progress_without_turn_end_waits_for_late_media(self):
        peer=Peer(self.clock,late_media=True)
        result=await service(peer,self.root).run_with_media(PROMPT,timeout=12,session_key_value="synthetic-image")
        self.assertEqual([m.path for m in result.media],["workspace/synthetic-circle.png"])
        self.assertGreaterEqual(self.clock.now,6)
        self.assertEqual(peer.submits,1)
        self.assertEqual(peer.posts.count("/chat/stream"),1)

    async def test_weak_refusal_message_without_turn_end_waits_for_late_media(self):
        peer=Peer(self.clock,late_media=True,weak_refusal=True)
        result=await service(peer,self.root).run_with_media(PROMPT,timeout=12,
            refusal_markers=("cannot generate",),session_key_value="synthetic-refusal")
        self.assertEqual([m.path for m in result.media],["workspace/synthetic-circle.png"])
        self.assertGreaterEqual(self.clock.now,6)
        self.assertEqual(peer.submits,1)

    async def test_no_media_or_explicit_turn_end_times_out_accepted_without_resubmit(self):
        peer=Peer(self.clock)
        with self.assertRaises(ProtocolOperationError) as caught:
            await service(peer,self.root).run_with_media(PROMPT,timeout=8,session_key_value="synthetic-timeout")
        self.assertEqual(caught.exception.code,"timeout")
        self.assertEqual(caught.exception.delivery,"accepted")
        self.assertFalse(caught.exception.retryable)
        self.assertTrue(caught.exception.session_id)
        self.assertGreaterEqual(self.clock.now,8)
        self.assertEqual(peer.submits,1)
        self.assertEqual(peer.posts.count("/chat/stream"),1)

    async def test_explicit_terminal_without_media_can_return_completed_empty_media(self):
        peer=Peer(self.clock,strong_terminal=True)
        result=await service(peer,self.root).run_with_media(PROMPT,timeout=8,session_key_value="synthetic-terminal")
        self.assertEqual(result.media,[])
        self.assertTrue(result.text)
        self.assertEqual(peer.submits,1)
        self.assertLess(self.clock.now,1)

    async def test_existing_media_does_not_require_new_turn_done_event(self):
        peer=Peer(self.clock,ready_media=True)
        result=await service(peer,self.root).run_with_media(PROMPT,timeout=8,session_key_value="synthetic-ready")
        self.assertEqual([m.path for m in result.media],["workspace/synthetic-circle.png"])
        self.assertEqual(peer.submits,1)
        self.assertLess(self.clock.now,6)

    async def test_plain_text_message_completion_behavior_is_unchanged(self):
        peer=Peer(self.clock)
        result=await service(peer,self.root).run(PROMPT,timeout=8,session_key_value="synthetic-text")
        self.assertEqual(result.text,"Preparing the requested image")
        self.assertEqual(peer.submits,1)
        self.assertLess(self.clock.now,1)

    async def test_completed_assistant_is_message_boundary_not_explicit_turn_end(self):
        events=[{"event_name":"message.assistant","payload":{"status":"completed","metadata":{"streaming":False}}}]
        self.assertEqual(_turn_lifecycle(events),(False,True,False))


if __name__=="__main__":
    unittest.main()
