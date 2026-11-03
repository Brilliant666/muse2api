"""Controlled multi-account isolation: bad A must not stall healthy B.

Actual ASGI routes, AccountPoolManager, ProtocolBackend and fresh JSON Store run
with synthetic transport/service failures. These are not real upstream HTTP
401/403/429, quota depletion or proxy-wire observations. No real account is used.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import threading
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('phase2_fault_network_guard',ROOT/'tests/protocol/network_guard/sitecustomize.py')
guard=importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)
sys.path.insert(0,str(ROOT/'tests/protocol'))
from test_app_contract import AppContractTests, SyntheticOperationError, MuseAuthError


class FaultIsolationTests(AppContractTests):
    def setUp(self):
        super().setUp()
        self.cfg.max_concurrency=3
        self.module.GEN_LOCK=self.module.ConcurrencyLock(3)

    def exercise_fault(self,kind):
        bad,healthy=self.accounts
        bad_id,healthy_id=bad['id'],healthy['id']
        self.store.mark(bad_id,True,'previously verified synthetic account')
        entered=threading.Event(); release=threading.Event(); healthy_done=threading.Event()
        out={}; original_transport=self.protocol.transport; original_service=self.protocol.service
        after_accepted=kind in {'429','quota'}

        async def wait_for_release():
            entered.set()
            while not release.is_set(): await asyncio.sleep(0.005)

        def transport(settings,cookies,on_cookies):
            http=original_transport(settings,cookies,on_cookies)
            if settings.account_id==bad_id and not after_accepted:
                async def failed_session():
                    await wait_for_release()
                    if kind in {'401','403'}:
                        raise MuseAuthError('synthetic Muse HTTP '+kind)
                    raise SyntheticOperationError(code='proxy_unavailable',phase='connect',delivery='not_sent')
                http.session_info=failed_session
            return http

        def service(settings,pool):
            value=original_service(settings,pool)
            if settings.account_id==bad_id and after_accepted:
                async def failed_turn(prompt,**kwargs):
                    async with pool.connection():
                        self.protocol.operations.append({'kind':'image','account':bad_id,'prompt':prompt})
                        await wait_for_release()
                        raise SyntheticOperationError(code='rate_limited' if kind=='429' else 'quota_exceeded',
                            phase='history',delivery='accepted')
                value.run_with_media=failed_turn
            return value

        self.backend.transport_factory=transport; self.backend.service_factory=service
        created=self.request('POST','/v1/images/tasks',payload={'prompt':'bad-A-'+kind,'timeout':4})
        self.assertEqual(created.status_code,202,created.text)
        task_id=created.json()['id']

        def healthy_request():
            try:
                out['response']=self.request('POST','/v1/chat/completions',payload={
                    'messages':[{'role':'user','content':'healthy-B-during-'+kind}],'timeout':4})
            except BaseException as exc: out['error']=exc
            finally: healthy_done.set()
        follower=threading.Thread(target=healthy_request,daemon=True)
        try:
            self.assertTrue(entered.wait(2),'bad account never entered controlled failure boundary')
            follower.start()
            self.assertTrue(healthy_done.wait(1.5),'healthy account stalled behind bad account')
            self.assertNotIn('error',out)
            self.assertEqual(out['response'].status_code,200,out['response'].text)
            self.assertFalse(release.is_set(),'healthy completion must precede releasing bad A')
            self.assertEqual(self.module.ACCOUNT_POOL.busy_accounts,{bad_id})
        finally:
            release.set()
            if follower.ident is not None: follower.join(5)
        self.assertFalse(follower.is_alive())
        failed=self.wait_task(task_id)
        self.assertEqual(failed['status'],'unknown' if after_accepted else 'failed',failed)
        counts={aid:sum(o['account']==aid for o in self.protocol.operations) for aid in (bad_id,healthy_id)}
        self.assertEqual(counts,{bad_id:int(after_accepted),healthy_id:1},'no fault may resend on healthy account')
        self.assertFalse(self.module.ACCOUNT_POOL.busy_accounts)
        self.assertFalse(self.module.GEN_LOCK.locked())
        self.assertTrue(self.store.get_account(healthy_id)['ok'])
        operations=list(self.store.protocol_operations.values())
        self.assertEqual(len(operations),1+int(after_accepted))
        self.assertEqual(sum(o['account_id']==healthy_id for o in operations),1)
        if after_accepted:
            original=self.store.get_protocol_operation(task_id)
            self.assertEqual(original['account_id'],bad_id)
            self.assertEqual(original['state'],'unknown')
            self.assertEqual(original['delivery'],'accepted')
            self.assertEqual(original['code'],'rate_limited' if kind=='429' else 'quota_exceeded')
            self.assertEqual(self.store.get_account(bad_id).get('protocol_blocked'),task_id)
            self.assertFalse(self.store.get_account(healthy_id).get('protocol_blocked'))
        elif kind=='401': self.assertFalse(self.store.get_account(bad_id)['ok'])
        elif kind=='403': self.assertTrue(self.store.get_account(bad_id)['ok'])

    def test_controlled_401_does_not_block_healthy_account(self): self.exercise_fault('401')
    def test_controlled_403_does_not_block_healthy_account(self): self.exercise_fault('403')
    def test_controlled_429_after_acceptance_is_isolated_not_replayed(self): self.exercise_fault('429')
    def test_controlled_quota_failure_is_isolated_not_replayed(self): self.exercise_fault('quota')
    def test_controlled_proxy_failure_does_not_block_healthy_account(self): self.exercise_fault('proxy')

    def test_cookie_replacement_rejects_only_stale_account_callback(self):
        a,b=self.accounts
        self.backend.renew_account(a['id']); self.backend.renew_account(b['id'])
        transports={t.account_id:t for t in self.protocol.transports}
        self.store.update_account(a['id'],cookies={'hatch_sess':'new-A-import'})
        transports[b['id']].rotate({'hatch_sess':'rotated-B-only'})
        with self.assertRaises(self.backend_module.BackendOperationError):
            transports[a['id']].rotate({'hatch_sess':'stale-A-response'})
        self.assertEqual(self.store.get_account(a['id'])['cookies'],{'hatch_sess':'new-A-import'})
        self.assertEqual(self.store.get_account(b['id'])['cookies'],{'hatch_sess':'rotated-B-only'})
        result=self.request('POST','/v1/chat/completions',payload={
            'messages':[{'role':'user','content':'healthy after A cookie replacement'}],'timeout':4})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(len(self.protocol.operations),1)
        self.assertFalse(self.module.ACCOUNT_POOL.busy_accounts)


NAMES=[name for name in FaultIsolationTests.__dict__ if name.startswith('test_')]
def load_tests(loader,tests,pattern):
    return unittest.TestSuite(FaultIsolationTests(name) for name in NAMES)

if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(load_tests(None,None,None))
    raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)
