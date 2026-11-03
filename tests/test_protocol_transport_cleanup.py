"""Offline regression: a post-handshake Store failure must retire the channel.

Only bootstrap, handshake peers and the HTTP client constructor are synthetic.
StoreOwnedHTTP.connect and its Cookie merge/persistence control flow are real.
No application Store, account, external request or generation is used.
"""
from __future__ import annotations

import ipaddress
import socket
import sys
from http.cookiejar import Cookie, CookieJar as HTTPCookieJar
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from protocol_backend import BackendOperationError, _default_transport
from muse2api.cookies import CookieJar
from muse2api.gateway import HatchConnection
from muse2api.http import MuseHTTP


class Channel:
    def __init__(self):
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1


class Client:
    def __init__(self):
        jar = HTTPCookieJar()
        jar.set_cookie(Cookie(0, 'hatch_sess', 'synthetic-session', None, False,
            'muse.ai', True, False, '/', True, False, int(time.time()) + 3600,
            False, None, None, {}, False))
        self.cookies = SimpleNamespace(jar=jar)
        self.close_calls = 0

    async def request(self, *_args, **_kwargs):
        raise AssertionError('this regression must not issue HTTP requests')

    async def close(self):
        self.close_calls += 1


class TransportCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        original_connect = socket.socket.connect
        def local_only(sock, address):
            if sock.family in {socket.AF_INET, socket.AF_INET6}:
                try:
                    permitted = ipaddress.ip_address(address[0]).is_loopback
                except (ValueError, TypeError):
                    permitted = False
                if not permitted:
                    raise AssertionError('external network forbidden')
            return original_connect(sock, address)
        guard = patch.object(socket.socket, 'connect', local_only)
        guard.start()
        self.addCleanup(guard.stop)
        self.client = Client()
        self.settings = SimpleNamespace(cvm_rv_key=None, attestation_cmd=None,
            impersonate='chrome', user_agent=None, ws_fallback=False)
        self.session = SimpleNamespace(vm_id='synthetic-vm')
        self.channels = []

    async def asyncTearDown(self):
        # Explicit fixture cleanup does not hide whether connect retired peers.
        for channel in self.channels:
            if not channel.close_calls:
                await channel.close()
        if hasattr(self, 'http'):
            await self.http.close()

    def transport(self, persist):
        client = self.client
        def initialize(http, settings, cookies):
            http.settings, http.proxy = settings, None
            http.jar, http._client = CookieJar(cookies), client
        with patch.object(MuseHTTP, '__init__', initialize):
            self.http = _default_transport(self.settings,
                {'hatch_sess': 'synthetic-session'}, persist)
        self.http.bootstrap = AsyncMock(return_value=self.session)
        return self.http

    async def new_channel(self, *_args, **_kwargs):
        channel = Channel()
        self.channels.append(channel)
        return channel

    async def test_store_oserror_closes_every_successful_handshake(self):
        error = OSError('synthetic Store write failure')
        def cannot_persist(*_args):
            raise error
        http = self.transport(cannot_persist)
        with patch.object(HatchConnection, 'connect', self.new_channel):
            with self.assertRaises(OSError) as caught:
                await http.connect()
        self.assertIs(caught.exception, error)
        self.assertEqual(len(self.channels), 2)
        self.assertEqual([c.close_calls for c in self.channels], [1, 1])
        self.assertEqual(http.bootstrap.await_count, 2)

    async def test_stale_credentials_close_without_retry(self):
        def stale(*_args):
            raise BackendOperationError('synthetic stale credentials',
                                        code='credential_revision_changed')
        http = self.transport(stale)
        with patch.object(HatchConnection, 'connect', self.new_channel):
            with self.assertRaises(BackendOperationError):
                await http.connect()
        self.assertEqual(len(self.channels), 1)
        self.assertEqual(self.channels[0].close_calls, 1)
        self.assertEqual(http.bootstrap.await_count, 1)

    async def test_transient_handshake_retry_preserves_returned_channel(self):
        http = self.transport(lambda *_args: None)
        handshake_calls = 0
        async def handshake(*args, **kwargs):
            nonlocal handshake_calls
            handshake_calls += 1
            if handshake_calls == 1:
                raise ConnectionResetError('synthetic pre-channel handshake failure')
            return await self.new_channel(*args, **kwargs)
        with patch.object(HatchConnection, 'connect', handshake):
            session, channel = await http.connect()
        self.assertIs(session, self.session)
        self.assertIs(channel, self.channels[0])
        self.assertEqual(channel.close_calls, 0)
        self.assertEqual(handshake_calls, 2)
        self.assertEqual(http.bootstrap.await_count, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
