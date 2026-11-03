"""Phase-2 budget/replay/identity safety; no external service or account used."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import phase2_batch as batch


class NeverSend:
    def __init__(self,error=None):
        self.calls=[]; self.error=error or TimeoutError('synthetic unknown delivery')
    def open(self,path,body=None,headers=None):
        self.calls.append((path,body is not None))
        raise self.error


class IntegrityService:
    """One synthetic generation; variants deliberately return foreign receipts."""
    class Reply(io.BytesIO): status=200
    def __init__(self,root,mode):
        self.root,self.mode=root,mode; self.posts=0; self.downloads=0
        self.operations_file=root/'original-operations.json'
        self.approved={'account-A':'A01','account-B':'A02','account-C':'A03'}
        self.media=b'synthetic-media-bytes'; self.task=None
    def open(self,path,body=None,headers=None):
        if body is not None:
            self.posts+=1
            import re
            marker=re.search(rb'PHASE2_L1_[0-9]{3}',body).group().decode()
            text=path=='/v1/chat/completions'; tid='task_original'
            op={'id':'op-original','account_id':'account-A','prompt':marker,
                'kind':'chat' if text else 'image','state':'completed','delivery':'completed',
                'session_id':'original-session','message_id':'original-message','vm_id':'original-vm',
                'task_id':None if text else tid,
                'result':{'text':marker} if text else {'filename':'own.png','size':len(self.media),'operation_id':'op-original'}}
            self.task={'id':tid,'task_id':tid,'operation_id':'op-original','account':'account-A',
                'kind':'image','status':'completed','url':'http://127.0.0.1/v1/media/own.png',
                'result':{'filename':'own.png','size':len(self.media)}}
            if self.mode=='wrong_op_task': op['task_id']='task_foreign'
            if self.mode in {'noncompleted','undelivered'}:
                op['state']='unknown' if self.mode=='noncompleted' else 'completed'; op['delivery']='accepted'
            if self.mode in {'failed429','quota','ban'}:
                self.task.update(status='failed',error={'failed429':'429 upstream rate limit',
                    'quota':'upstream quota exceeded','ban':'account banned'}[self.mode])
                op.update(state='failed',delivery='not_sent')
            operations={'op-original':op}
            if self.mode=='ambiguous': operations['op-duplicate']={**op,'id':'op-duplicate'}
            if self.mode=='corrupt_json': self.operations_file.write_text('{broken','utf8')
            else: batch.common.atomic_json(self.operations_file,operations)
            if text:
                value={'choices':[{'message':{'role':'assistant','content':'foreign text' if self.mode=='wrong_text' else marker},'finish_reason':'stop'}]}
            else:
                value={'id':tid,'task_id':tid,'operation_id':'op-foreign' if self.mode=='wrong_recorded_op' else 'op-original'}
                if self.mode=='wrong_create_task': value['id']='task_foreign'
        elif path.startswith('/v1/media/'):
            self.downloads+=1
            return self.Reply(self.media[:-1] if self.mode=='short_media' else self.media)
        else:
            value=dict(self.task)
            if self.mode=='wrong_task_id': value['id']='task_foreign'
            if self.mode=='wrong_task_operation': value['operation_id']='op-foreign'
            if self.mode=='wrong_account': value['account']='account-B'
            if self.mode=='wrong_media_url': value['url']='http://127.0.0.1/v1/media/foreign.png'
        return self.Reply(json.dumps(value).encode())


class Phase2SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='muse-phase2-budget-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.state=batch.State(self.root)

    def args(self,round='L1'):
        return SimpleNamespace(round=round,operations_file=None,task_timeout=1,
            preflight=self.root/'preflight.json',ffprobe=None,ffmpeg=None)

    def write_preflight(self,count=3):
        data={'platform':'linux','backend':'protocol','upstream_identity_verified':True,
            'accounts':[{'alias':f'A{i:02d}','account_id':f'synthetic-account-{i}',
                'upstream_identity_sha256':hashlib.sha256(f'upstream-principal-{i}'.encode()).hexdigest(),
                'enabled':True} for i in range(1,count+1)]}
        path=self.root/'preflight.json'; path.write_text(json.dumps(data),'utf8')
        return path,data

    def test_fixed_plans_and_global_caps(self):
        sums={k:0 for k in batch.LIMITS}
        for name,spec in batch.ROUNDS.items():
            items=batch.plan(name)
            self.assertEqual(len(items),sum(spec[k] for k in batch.LIMITS))
            self.assertEqual(len({i['case_id'] for i in items}),len(items))
            for key in batch.LIMITS:
                self.assertEqual(sum(i['category']==key for i in items),spec[key])
                if batch.budget_slot(name)==name: sums[key]+=spec[key]
        self.assertEqual(sums,{'text':525,'image':31,'video':4})
        self.assertEqual({i['kind'] for i in batch.plan('L1')},
            {'chat','chat_sse','responses','responses_sse','image','edit','video'})

    def test_legacy_or_repository_state_locations_refused(self):
        legacy=self.root/'legacy'; (legacy/'live-cases').mkdir(parents=True)
        (legacy/'live-cases/ledger.json').write_text('{}','utf8')
        with self.assertRaises(batch.BatchError): batch.State(legacy)
        with self.assertRaises(batch.BatchError): batch.State(batch.ROOT/'private-budget-test-must-not-be-created')

    def test_duplicate_identity_and_insufficient_accounts_block(self):
        path,data=self.write_preflight()
        self.assertEqual(len(batch.identities(path,'L1')),3)
        self.assertEqual(len(batch.identities(path,'C3P')),3)
        with self.assertRaises(batch.BatchError): batch.identities(path,'C3')
        with self.assertRaises(batch.BatchError): batch.identities(path,'C5')
        with self.assertRaises(batch.BatchError): batch.identities(path,'SOAK')
        data['accounts'][1]['upstream_identity_sha256']=data['accounts'][0]['upstream_identity_sha256']
        path.write_text(json.dumps(data),'utf8')
        with self.assertRaises(batch.BatchError): batch.identities(path,'L1')
        self.assertFalse(self.state.path.exists())

    def test_identity_gate_precedes_network_or_batch_claim(self):
        self.write_preflight(3); client=NeverSend()
        with patch.object(batch.sys,'platform','linux'):
            with self.assertRaises(batch.BatchError): batch.run_batch(self.state,client,self.args('C3'))
        self.assertEqual(client.calls,[]); self.assertFalse(self.state.path.exists())

    def test_case_and_batch_replay_refused(self):
        self.state.begin('L1','synthetic-preflight-hash')
        self.state.reserve('L1-001','text','/v1/chat/completions','hash')
        self.state.mark_send('L1-001'); self.state.update('L1-001',status='UNKNOWN')
        with self.assertRaises(batch.BatchError): self.state.mark_send('L1-001')
        with self.assertRaises(batch.BatchError): self.state.reserve('L1-001','text','/v1/chat/completions','new')
        with self.assertRaises(batch.BatchError): self.state.begin('L1','new')
        client=NeverSend()
        with self.assertRaises(batch.BatchError): batch.run_batch(self.state,client,self.args())
        self.assertEqual(client.calls,[])
        self.assertEqual(self.state.summary('L1')['consumed']['text'],1)

    def test_concurrent_reservations_cannot_overrun_round_cap(self):
        self.state.begin('L1','hash')
        def reserve(i):
            try: self.state.reserve(f'L1-{i:03d}','text','/v1/chat/completions','hash'); return True
            except batch.BatchError: return False
        with ThreadPoolExecutor(max_workers=5) as pool: results=list(pool.map(reserve,range(1,17)))
        self.assertEqual(sum(results),15)
        self.assertEqual(self.state.summary('L1')['consumed']['text'],15)
        self.assertEqual(self.state.summary('L1')['generation_post_intents'],0)

    def test_timeout_is_charged_once_and_stops_new_sends(self):
        self.state.begin('L1','hash'); client=NeverSend()
        runner=batch.Runner(self.state,client,{},self.args())
        runner.execute(batch.plan('L1')[0],[],0)
        runner.execute(batch.plan('L1')[1],[],0)
        summary=self.state.summary('L1')
        self.assertEqual(summary['consumed']['text'],1)
        self.assertEqual(summary['generation_post_intents'],1)
        self.assertEqual(sum(is_post for _,is_post in client.calls),1)
        self.assertEqual(summary['states'],{'UNKNOWN':1})
        self.assertTrue(runner.stop.is_set())

    def test_rate_limit_keeps_private_response_and_stops(self):
        error=urllib.error.HTTPError('http://127.0.0.1/fake',429,'synthetic',{},io.BytesIO(b'{"error":"rate limited"}'))
        self.state.begin('L1','hash'); client=NeverSend(error)
        runner=batch.Runner(self.state,client,{},self.args()); runner.execute(batch.plan('L1')[0],[],0)
        item=self.state.load()['cases']['L1-001']
        self.assertEqual(item['failure_class'],'RATE_LIMIT')
        self.assertEqual((self.state.folder('L1-001')/'error-response.bin').read_bytes(),b'{"error":"rate limited"}')
        self.assertEqual(sum(post for _,post in client.calls),1)
        self.assertTrue(runner.stop.is_set())

    def test_public_summary_excludes_raw_identity_and_paths(self):
        self.state.begin('L1','hash'); self.state.reserve('L1-001','text','/v1/chat/completions','hash')
        self.state.update('L1-001',account_id='private-account-id',account_alias='A01',
            operation_id='private-operation-id',session_id='private-session-id',downloaded_file='/private/media.png')
        public=json.dumps(self.state.summary('L1'))
        for value in ['private-account-id','private-operation-id','private-session-id','/private/media.png']:
            self.assertNotIn(value,public)
        self.assertIn('A01',public)

    def test_l1_request_shapes_include_distinct_ordered_refs(self):
        refs=batch.fixtures(self.root)
        self.assertNotEqual(refs[0].read_bytes(),refs[1].read_bytes())
        edits=0
        for item in batch.plan('L1'):
            old,marker,path,body,content_type=batch.request_for(item,refs)
            self.assertIn(marker.encode(),body)
            if item['kind']=='edit':
                edits+=1; self.assertEqual(body.count(b'name="image"'),2)
                self.assertLess(body.index(b'filename="reference-1.png"'),body.index(b'filename="reference-2.png"'))
                self.assertTrue(content_type.startswith('multipart/form-data;'))
        self.assertEqual(edits,2)


    def test_complete_l1_with_synthetic_service_and_decoder_boundary(self):
        preflight,data=self.write_preflight(3)
        operations_file=self.root/'synthetic-operations.json'
        operations={}; tasks={}; request_count=0
        image_bytes=batch.fixtures(self.root)[0].read_bytes()
        class Reply(io.BytesIO):
            status=200
        class Service:
            def open(inner,path,body=None,headers=None):
                nonlocal request_count
                if path=='/admin/backend': value={'backend':'protocol','running':True}
                elif path=='/admin/accounts': value=[{'id':a['account_id'],'enabled':True} for a in data['accounts']]
                elif body is not None:
                    request_count+=1
                    import re
                    marker=re.search(rb'PHASE2_L1_[0-9]{3}',body).group().decode()
                    oid=f'op-synthetic-{request_count}'
                    category='video' if path=='/v1/videos' else 'image' if path.startswith('/v1/images') else 'chat'
                    operations[oid]={'id':oid,'account_id':data['accounts'][(request_count-1)%3]['account_id'],
                        'prompt':marker,'kind':category,'state':'completed','delivery':'completed',
                        'session_id':f'synthetic-session-{request_count}','message_id':f'synthetic-message-{request_count}',
                        'vm_id':'synthetic-vm','result':{'text':marker}}
                    batch.common.atomic_json(operations_file,operations)
                    if category!='chat':
                        tid=f'task_{request_count}'
                        filename=tid+('.mp4' if category=='video' else '.png')
                        size=len(b'synthetic-video-bytes') if category=='video' else len(image_bytes)
                        operations[oid]['task_id']=tid
                        operations[oid]['result']={'filename':filename,'size':size,'kind':category,'operation_id':oid}
                        batch.common.atomic_json(operations_file,operations)
                        tasks[tid]={'id':tid,'task_id':tid,'kind':category,'status':'completed','operation_id':oid,
                            'account':operations[oid]['account_id'],
                            'result':{'filename':filename,'size':size},'url':'http://127.0.0.1/v1/media/'+filename}
                        value={'id':tid,'task_id':tid,'operation_id':oid}
                    else:
                        request=json.loads(body); streaming=request.get('stream',False)
                        output=[{'content':[{'type':'output_text','text':marker}]}]
                        if path=='/v1/responses':
                            final={'id':oid,'status':'completed','output':output}
                            if streaming:
                                frames=[{'type':'response.created','response':{'id':oid}},
                                    {'type':'response.output_item.added'},{'type':'response.content_part.added'},
                                    {'type':'response.output_text.delta','delta':marker},
                                    {'type':'response.output_text.done','text':marker},
                                    {'type':'response.output_item.done'},
                                    {'type':'response.completed','response':final}]
                                return Reply(''.join('data: '+json.dumps(f)+'\n\n' for f in frames).encode())
                            value=final
                        elif streaming:
                            frames=[{'choices':[{'delta':{'content':marker},'finish_reason':None}]},
                                    {'choices':[{'delta':{},'finish_reason':'stop'}]}]
                            return Reply((''.join('data: '+json.dumps(f)+'\n\n' for f in frames)+'data: [DONE]\n\n').encode())
                        else: value={'choices':[{'message':{'role':'assistant','content':marker},'finish_reason':'stop'}]}
                elif path.startswith('/v1/media/'):
                    return Reply(b'synthetic-video-bytes' if path.endswith('.mp4') else image_bytes)
                else: value=tasks[path.rsplit('/',1)[1]]
                return Reply(json.dumps(value).encode())
        args=self.args(); args.operations_file=operations_file
        with patch.object(batch.sys,'platform','linux'), patch.object(batch.shutil,'which',return_value='/synthetic/decoder'), \
                patch.object(batch.decoder,'check_file',return_value={'status':'PASS','full_decode_passed':True,'boundary':'synthetic'}):
            summary=batch.run_batch(self.state,Service(),args)
        self.assertEqual(summary['status'],'PASS')
        self.assertEqual(summary['case_count'],20); self.assertEqual(request_count,20)
        self.assertEqual(summary['consumed'],{'text':15,'image':4,'video':1})
        self.assertEqual(summary['states'],{'PASS':20}); self.assertEqual(len(summary['accounts']),3)
        for row in self.state.load()['cases'].values():
            self.assertTrue(row.get('operation_id')); self.assertTrue(row.get('account_id'))


    def test_repeated_original_queries_keep_distinct_receipts(self):
        self.state.begin('L1','hash'); self.state.reserve('L1-001','image','/v1/images/tasks','hash')
        class Response(io.BytesIO): status=200
        class Client:
            def open(inner,*args,**kwargs): return Response(b'{"status":"completed"}')
        runner=batch.Runner(self.state,Client(),{},self.args())
        for _ in range(2): runner.call('L1-001','/v1/images/tasks/original-task',label='task-0001')
        folder=self.state.folder('L1-001')
        self.assertEqual(len(list(folder.glob('task-0001-*.bin'))),2)
        receipts=list(folder.glob('http-*.json')); self.assertEqual(len(receipts),2)
        for path in receipts:
            data=json.loads(path.read_text('utf8'))
            self.assertEqual(data['method'],'GET'); self.assertEqual(data['http_status'],200)
            self.assertTrue(data['started_at']); self.assertTrue(data['finished_at'])
        self.assertEqual(self.state.summary('L1')['generation_post_intents'],0)


    def test_stop_after_reservation_prevents_late_generation_post(self):
        self.state.begin('L1','hash'); client=NeverSend()
        runner=batch.Runner(self.state,client,{},self.args())
        original_reserve=self.state.reserve
        def reserve_then_stop(*args,**kwargs):
            original_reserve(*args,**kwargs); runner.stop.set()
        with patch.object(self.state,'reserve',reserve_then_stop):
            runner.execute(batch.plan('L1')[0],[],0)
        summary=self.state.summary('L1')
        self.assertEqual(summary['consumed']['text'],1)
        self.assertEqual(summary['generation_post_intents'],0)
        self.assertEqual(summary['states'],{'STOPPED_BEFORE_SEND':1})
        self.assertEqual(client.calls,[])


    def test_c3p_and_c3_claims_are_mutually_exclusive_before_network(self):
        for first,second in [('C3P','C3'),('C3','C3P')]:
            with self.subTest(first=first):
                state=batch.State(self.root/first); state.begin(first,'hash')
                with self.assertRaises(batch.BatchError): state.begin(second,'hash')
                client=NeverSend()
                with self.assertRaises(batch.BatchError): batch.run_batch(state,client,self.args(second))
                self.assertEqual(client.calls,[])
                self.assertEqual(set(state.load()['batches']),{first})
                self.assertEqual(state.load()['batches'][first]['budget_slot'],'C3')

    def test_c3p_consumes_one_shared_slot_without_expanding_total(self):
        self.state.begin('C3P','hash')
        for number in range(1,112): self.state.reserve(f'C3P-{number:03d}','text','/v1/chat/completions','hash')
        with self.assertRaises(batch.BatchError): self.state.reserve('C3P-112','text','/v1/chat/completions','hash')
        for number in range(112,120): self.state.reserve(f'C3P-{number:03d}','image','/v1/images/edits','hash')
        self.state.reserve('C3P-120','video','/v1/videos','hash')
        summary=self.state.summary('C3P')
        self.assertEqual(summary['consumed'],{'text':111,'image':8,'video':1})
        self.assertEqual(summary['remaining'],{'text':414,'image':23,'video':3})
        self.assertEqual(sum(summary['remaining'].values())+summary['case_count'],560)
        self.assertTrue(summary['partial_scope']); self.assertEqual(summary['original_target_status'],'NOT_RUN')
        with self.assertRaises(batch.BatchError): self.state.begin('C3','hash')
        self.assertEqual(batch.ROUNDS['C3']['accounts'],5)
        self.assertEqual(batch.ROUNDS['C5']['accounts'],10)
        self.assertEqual(batch.ROUNDS['SOAK']['accounts'],10)

    def test_concurrent_c3_and_c3p_slot_claim_has_one_winner(self):
        def begin(name):
            try: self.state.begin(name,'hash'); return True
            except batch.BatchError: return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(begin,['C3','C3P']))
        self.assertEqual(sum(outcomes),1)
        self.assertEqual(len(self.state.load()['batches']),1)


    def test_foreign_or_ambiguous_receipts_cannot_pass_or_continue(self):
        variants={'wrong_recorded_op':'IDENTITY_CONFLICT','wrong_create_task':'IDENTITY_CONFLICT',
            'wrong_task_id':'IDENTITY_CONFLICT','wrong_task_operation':'IDENTITY_CONFLICT',
            'wrong_op_task':'IDENTITY_CONFLICT','wrong_account':'CROSS_ACCOUNT','wrong_text':'TEXT_MISMATCH',
            'wrong_media_url':'IDENTITY_CONFLICT','noncompleted':'OPERATION_NOT_COMPLETED',
            'undelivered':'OPERATION_NOT_COMPLETED','ambiguous':'BINDING_AMBIGUOUS',
            'corrupt_json':'DATA_CORRUPTION','short_media':'DATA_CORRUPTION'}
        for mode,expected in variants.items():
            with self.subTest(mode=mode):
                root=self.root/mode; state=batch.State(root); state.begin('L1','hash')
                service=IntegrityService(root,mode); args=self.args(); args.operations_file=service.operations_file
                runner=batch.Runner(state,service,service.approved,args)
                kind='chat' if mode in {'wrong_text','noncompleted','undelivered','ambiguous','corrupt_json'} else 'image'
                item=next(i for i in batch.plan('L1') if i['kind']==kind)
                runner.execute(item,[],0)
                row=state.load()['cases'][item['case_id']]
                self.assertNotEqual(row['status'],'PASS'); self.assertEqual(row['failure_class'],expected)
                self.assertTrue(runner.stop.is_set()); self.assertEqual(service.posts,1)
                runner.execute(next(i for i in batch.plan('L1') if i['case_id']!=item['case_id']),[],0)
                self.assertEqual(service.posts,1,'severe integrity fault allowed another generation POST')
                if mode=='wrong_recorded_op': self.assertEqual(row['operation_id'],'op-foreign','conflicting recorded ID was overwritten')
                if mode=='wrong_media_url': self.assertEqual(service.downloads,0)

    def test_http200_failed_task_rate_limit_stops_first_case(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading
        root=self.root/'real-loopback-429'; state=batch.State(root); state.begin('L1','hash')
        service=IntegrityService(root,'failed429')
        class Peer(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def reply(self,body=None):
                with service.open(self.path,body) as response: raw=response.read()
                self.send_response(200); self.send_header('Content-Length',str(len(raw)))
                self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(raw)
            def do_POST(self): self.reply(self.rfile.read(int(self.headers['Content-Length'])))
            def do_GET(self): self.reply()
        peer=ThreadingHTTPServer(('127.0.0.1',0),Peer)
        thread=threading.Thread(target=peer.serve_forever,daemon=True); thread.start()
        args=self.args(); args.operations_file=service.operations_file
        client=batch.common.Client(f'http://127.0.0.1:{peer.server_port}','synthetic-service-key',5)
        runner=batch.Runner(state,client,service.approved,args)
        item=next(i for i in batch.plan('L1') if i['kind']=='image')
        try: runner.execute(item,[],0)
        finally: peer.shutdown(); peer.server_close(); thread.join(2)
        row=state.load()['cases'][item['case_id']]
        self.assertEqual(row['http_status'],200); self.assertEqual(row['failure_class'],'RATE_LIMIT')
        self.assertEqual(row['task_status'],'failed'); self.assertTrue(runner.stop.is_set())
        runner.execute(batch.plan('L1')[0],[],0); self.assertEqual(service.posts,1)
        self.assertEqual(state.summary('L1')['consumed']['image'],1)

    def test_quota_and_ban_task_errors_stop_without_second_post(self):
        for mode,expected in [('quota','QUOTA'),('ban','ACCOUNT_BANNED')]:
            with self.subTest(mode=mode):
                root=self.root/mode; state=batch.State(root); state.begin('L1','hash')
                service=IntegrityService(root,mode); args=self.args(); args.operations_file=service.operations_file
                runner=batch.Runner(state,service,service.approved,args)
                item=next(i for i in batch.plan('L1') if i['kind']=='image')
                runner.execute(item,[],0)
                self.assertEqual(state.load()['cases'][item['case_id']]['failure_class'],expected)
                self.assertTrue(runner.stop.is_set()); runner.execute(batch.plan('L1')[0],[],0)
                self.assertEqual(service.posts,1)


    def test_unverified_returned_operation_cannot_be_recovered(self):
        self.state.begin('L1','hash'); self.state.reserve('L1-001','text','/v1/chat/completions','hash')
        self.state.update('L1-001',status='FAIL',operation_id='unverified-foreign-operation')
        client=NeverSend()
        with patch.object(batch.common,'Client',return_value=client),patch('builtins.print'):
            code=batch.main(['recover','--round','L1','--private-root',str(self.root),
                '--base','http://127.0.0.1:1','--case','L1-001'])
        self.assertEqual(code,2)
        self.assertEqual(client.calls,[],'unverified HTTP-returned ID triggered a recovery POST')
        self.assertEqual(self.state.summary('L1')['consumed']['text'],1)


if __name__=='__main__': unittest.main(verbosity=2)
