"""Actual Linux SIGTERM regression for the metrics server, with zero accounts.

Run in a disposable --network none container. Only /tmp is used for Store and
metrics, and only this test's child PID is signalled. No host private data mount.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request


class LinuxServerShutdownTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith('linux'), 'Actual Linux OS signal test required')
    def test_sigterm_exits_zero_after_flushing_metrics(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='phase2-empty-shutdown-') as directory:
            scratch=Path(directory); home=scratch/'application'; metrics=scratch/'metrics.jsonl'
            environment={k:v for k,v in os.environ.items() if not k.startswith(('MUSE_','MUSE2API_'))
                         and k not in {'MODEL_API_KEY','META_API_KEY','META_MODEL_API_KEY','IMAGE_GATEWAY_TOKEN'}}
            environment.update(MUSE_BACKEND='protocol',MUSE2API_HOME=str(home),
                MUSE2API_KEY='synthetic-shutdown-test-key',MUSE2API_CONCURRENCY='1',
                MUSE2API_PROFILE_ROOT=str(scratch/'unused-profile'),PYTHONPATH=str(root),PYTHONUNBUFFERED='1')
            with socket.socket() as port_socket:
                port_socket.bind(('127.0.0.1',0)); port=port_socket.getsockname()[1]
            command=[sys.executable,str(root/'tools/phase2_server.py'),'--metrics',str(metrics),
                '--host','127.0.0.1','--port',str(port),'--workers','1','--sample-seconds','1']
            child=subprocess.Popen(command,env=environment,cwd=scratch,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base=f'http://127.0.0.1:{port}'
            proof={'scope':'actual Linux subprocess and SIGTERM; new empty Store; no external network',
                   'service_pid':child.pid,'signal':'SIGTERM','real_generation_calls':0}
            output=errors=''; forced_kill=False
            started=time.monotonic()
            try:
                deadline=time.monotonic()+20
                while True:
                    try:
                        with opener.open(base+'/healthz',timeout=0.5) as response:
                            proof['health_status']=response.status
                        break
                    except (OSError,urllib.error.URLError):
                        if child.poll() is not None or time.monotonic()>=deadline:
                            raise AssertionError('Empty test server did not become healthy')
                        time.sleep(0.05)
                request=urllib.request.Request(base+'/admin/accounts',
                    headers={'Authorization':'Bearer synthetic-shutdown-test-key'})
                with opener.open(request,timeout=2) as response:
                    accounts=json.load(response)['accounts']
                self.assertEqual(accounts,[],'This regression must never load an account')
                proof['account_count']=len(accounts)
                # Require a real sample to be queued before stopping the server.
                deadline=time.monotonic()+5
                while True:
                    rows=[]
                    if metrics.exists():
                        for line in metrics.read_text('utf8').splitlines():
                            try: rows.append(json.loads(line))
                            except ValueError: pass  # a writer may currently hold the final line
                    if any(row.get('event')=='backend_sample' for row in rows):
                        break
                    if time.monotonic()>=deadline:
                        raise AssertionError('Backend sampler did not produce a real sample')
                    time.sleep(0.05)
                child.send_signal(signal.SIGTERM)
                try:
                    output,errors=child.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    forced_kill=True; child.kill(); output,errors=child.communicate(timeout=5)
            finally:
                if child.poll() is None:
                    forced_kill=True; child.kill(); output,errors=child.communicate(timeout=5)
                rows=[json.loads(line) for line in metrics.read_text('utf8').splitlines()] if metrics.exists() else []
                stderr_rows=[]
                for line in errors.splitlines():
                    try: stderr_rows.append(json.loads(line))
                    except ValueError: pass
                completion=next((row['phase2_metrics'] for row in stderr_rows if 'phase2_metrics' in row),None)
                proof.update(child_returncode=child.returncode,forced_kill=forced_kill,
                    elapsed_seconds=round(time.monotonic()-started,3),
                    metrics_events=[row.get('event') for row in rows],
                    metrics_sha256=hashlib.sha256(metrics.read_bytes()).hexdigest() if metrics.exists() else None,
                    final_metric=rows[-1] if rows else None,writer_completion=completion,
                    stdout=output,stderr=errors)
                print(json.dumps({'linux_shutdown_proof':proof}),flush=True)
                # All rows are from this zero-account fixture; preserve complete
                # evidence through the caller's stdout capture, never edit a tail.
                print(json.dumps({'empty_fixture_metrics':rows}),flush=True)
            self.assertFalse(forced_kill)
            self.assertEqual(child.returncode,0,'SIGTERM replay must not bypass wrapper finally')
            self.assertTrue(rows)
            self.assertEqual(rows[-1].get('event'),'metrics_summary')
            self.assertIsNotNone(completion)
            self.assertTrue(completion['writer_finished'])
            self.assertEqual(completion['queued'],0)
            self.assertEqual(completion['dropped'],0)
            self.assertEqual(completion['writer_errors'],0)
            self.assertEqual(len(rows),completion['written'])
            self.assertEqual(completion['written'],completion['enqueued']+1)
            self.assertTrue(any(row.get('event')=='server_exit' for row in rows))
            self.assertFalse(any(row.get('event','').startswith(('gateway_','noise_','http_control')) for row in rows))
            self.assertTrue(all(row.get('active_operation_count',0)==0 for row in rows))


if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(LinuxServerShutdownTests))
    raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)
