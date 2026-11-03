"""Real native curl HTTP to a loopback peer: dead proxy A, healthy direct B.

SDK BASE alone points at the synthetic peer. No bootstrap, wake, Noise, model
submission, real account or existing Store is used. This does not prove any
real Muse proxy, identity or generation behavior.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('phase2_native_proxy_guard',ROOT/'tests/protocol/network_guard/sitecustomize.py')
guard=importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)
sys.path.insert(0,str(ROOT))
from config import Config
from store import Store
from protocol_backend import ProtocolBackend, BackendOperationError
import muse2api.http as sdk_http


class NativeProxyIsolationTests(unittest.TestCase):
    def test_dead_proxy_not_bypassed_while_healthy_account_renews(self):
        received=[]
        class Peer(BaseHTTPRequestHandler):
            def log_message(self,*_args): pass
            def do_GET(self):
                received.append({'path':self.path,'cookie':self.headers.get('Cookie','')})
                data=json.dumps({'status':'assigned','vm_id':'native-synthetic-vm',
                    'vm_state':'RUNNING','endpoint_url':'https://unused.invalid'}).encode()
                self.send_response(200); self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(data)))
                self.send_header('Set-Cookie','hatch_sess=rotated-synthetic-B; Max-Age=3600; Path=/')
                self.end_headers(); self.wfile.write(data)
        peer=ThreadingHTTPServer(('127.0.0.1',0),Peer)
        thread=threading.Thread(target=peer.serve_forever,daemon=True); thread.start()
        blocked_proxy=socket.socket(); blocked_proxy.bind(('127.0.0.1',0))
        # The port remains bound without listening, so another process cannot
        # turn the intended dead proxy into a reachable proxy during the test.
        proxy_port=blocked_proxy.getsockname()[1]
        environment={name:'' for name in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY',
            'http_proxy','https_proxy','all_proxy','no_proxy')}
        backend=None
        try:
            with tempfile.TemporaryDirectory(prefix='muse-phase2-native-proxy-') as directory, \
                    patch.dict(os.environ,environment), \
                    patch.object(sdk_http,'BASE',f'http://127.0.0.1:{peer.server_port}'):
                cfg=Config(base_dir=directory,backend='protocol',api_key='native-synthetic-service-key')
                cfg.ensure_dirs(); store=Store(cfg)
                bad=store.add_account({'hatch_sess':'synthetic-A'})
                store.update_account(bad['id'],proxy={'server':f'http://127.0.0.1:{proxy_port}'})
                good=store.add_account({'hatch_sess':'synthetic-B'})
                backend=ProtocolBackend(store,cfg)
                with self.assertRaises(BackendOperationError):
                    backend.renew_account(bad['id'],wake_vm=False)
                self.assertEqual(received,[],'failed account bypassed its proxy and contacted target directly')
                result=backend.renew_account(good['id'],wake_vm=False)
                self.assertTrue(result['ok']); self.assertEqual(result['vm_state'],'RUNNING')
                self.assertEqual(len(received),1)
                self.assertTrue(received[0]['path'].startswith('/api/session?'))
                self.assertIn('synthetic-B',received[0]['cookie'])
                self.assertNotIn('synthetic-A',received[0]['cookie'])
                self.assertEqual(store.get_account(good['id'])['cookies']['hatch_sess'],'rotated-synthetic-B')
                self.assertEqual(store.get_account(bad['id'])['cookies']['hatch_sess'],'synthetic-A')
                persisted=json.loads(Path(cfg.accounts_file).read_text('utf8'))
                self.assertEqual(next(a for a in persisted if a['id']==good['id'])['cookies']['hatch_sess'],'rotated-synthetic-B')
                self.assertFalse(store.protocol_operations)
                self.assertFalse(store.tasks)
                backend.stop(); backend=None
        finally:
            if backend is not None: backend.stop()
            blocked_proxy.close(); peer.shutdown(); peer.server_close(); thread.join(2)
        self.assertFalse(thread.is_alive())


if __name__=='__main__':
    import hashlib, platform, importlib.metadata
    print(json.dumps({'platform':sys.platform,'python':platform.python_version(),
        'curl_cffi':importlib.metadata.version('curl_cffi'),
        'protocol_backend_sha256':hashlib.sha256((ROOT/'protocol_backend.py').read_bytes()).hexdigest(),
        'store_sha256':hashlib.sha256((ROOT/'store.py').read_bytes()).hexdigest(),
        'test_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'live_muse':False}),flush=True)
    unittest.main(verbosity=2)
