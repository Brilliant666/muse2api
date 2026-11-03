#!/usr/bin/env python3
"""Bounded phase-2 Linux batches; no generation replay, no phase-1 defaults.

Credentials and raw receipts stay in the REQUIRED external private root. The
preflight must contain independently verified upstream identities, not Cookie
fingerprints. Every case intent is charged durably before its one generation
POST. Query/recover accept a recorded case only and never a caller-supplied ID.
C3P is explicitly a three-account partial batch sharing the original C3 budget
slot. C3 and C3P are mutually exclusive, not two independent authorizations.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time
from types import SimpleNamespace
import urllib.error
import urllib.parse
import uuid

import live_protocol_case as common
import decode_acceptance_media as decoder

ROOT = Path(__file__).resolve().parents[1]
ROUNDS = {
    'L1': {'text': 15, 'image': 4, 'video': 1, 'concurrency': 1, 'accounts': 2},
    'C3': {'text': 111, 'image': 8, 'video': 1, 'concurrency': 3, 'accounts': 5},
    'C3P': {'text': 111, 'image': 8, 'video': 1, 'concurrency': 3, 'accounts': 3},
    'C5': {'text': 111, 'image': 8, 'video': 1, 'concurrency': 5, 'accounts': 10},
    'SOAK': {'text': 288, 'image': 11, 'video': 1, 'concurrency': 3, 'accounts': 10},
}
LIMITS = {'text': 525, 'image': 31, 'video': 4}
BUDGET_ALIASES = {'C3P': 'C3'}
CASE = re.compile(r'^(L1|C3P|C3|C5|SOAK)-[0-9]{3}$')


class BatchError(RuntimeError):
    pass


class SafetyError(BatchError):
    def __init__(self,failure_class,message):
        super().__init__(message)
        self.failure_class=failure_class


STOP_FAILURES={'RATE_LIMIT','QUOTA','ACCOUNT_BANNED','DATA_CORRUPTION','CROSS_ACCOUNT',
    'IDENTITY_CONFLICT','TEXT_MISMATCH','BINDING_AMBIGUOUS','BINDING_MISSING',
    'OPERATION_NOT_COMPLETED','AUTH','ACCESS_DENIED','OUTCOME_UNKNOWN','DECODE_BLOCKED','UPSTREAM_OR_VALIDATION'}
IDENTITY_FAILURES={'CROSS_ACCOUNT','IDENTITY_CONFLICT','TEXT_MISMATCH','BINDING_AMBIGUOUS','BINDING_MISSING'}


def classify_failure(exc,record,http_code,category):
    explicit=getattr(exc,'failure_class',None)
    if explicit in IDENTITY_FAILURES-{'BINDING_MISSING'}: return explicit
    verified=record.get('operation_binding_verified') is True
    operation_status=record.get('operation_upstream_status') if verified else None
    if type(operation_status) is int and operation_status==429: return 'RATE_LIMIT'
    message=' '.join(str(v or '') for v in (record.get('upstream_error'),record.get('upstream_error_code'),
        record.get('operation_error') if verified else None,
        record.get('operation_error_code') if verified else None,str(exc))).lower()
    if http_code==429 or re.search(r'\b429\b',message) or any(t in message for t in ('rate limit','rate_limit','too many requests','限流','请求过多')): return 'RATE_LIMIT'
    if any(t in message for t in ('quota','credit','额度','余额不足')): return 'QUOTA'
    if any(t in message for t in ('banned','account suspended','account disabled','封禁','封号','账号禁用')): return 'ACCOUNT_BANNED'
    if isinstance(exc,(json.JSONDecodeError,UnicodeDecodeError)) or explicit=='DATA_CORRUPTION': return 'DATA_CORRUPTION'
    if http_code==401: return 'AUTH'
    if http_code==403: return 'ACCESS_DENIED'
    if explicit: return explicit
    if category=='UNKNOWN': return 'OUTCOME_UNKNOWN'
    return 'UPSTREAM_OR_VALIDATION'


def budget_slot(name):
    if name not in ROUNDS:
        raise BatchError('Unknown round; no budget slot')
    return BUDGET_ALIASES.get(name,name)


def slot_claimed(data,name):
    return any(budget_slot(other)==budget_slot(name) for other in data['batches'])


def sha(value):
    return hashlib.sha256(value).hexdigest()


def now():
    return common.now()


class State:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        if self.root.is_relative_to(ROOT):
            raise BatchError('Private root must be outside the Git worktree')
        if (self.root/'live-cases/ledger.json').exists() or (self.root/'ledger.json').exists():
            raise BatchError('Phase-1 ledger location refused; use the independent phase-2 root')
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root/'phase2-ledger.json'
        self.mutex = threading.RLock()

    @contextmanager
    def transaction(self):
        with self.mutex:
            lock = self.root/'phase2-ledger.lock'
            try:
                fd = os.open(lock, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
            except FileExistsError:
                raise BatchError('Ledger locked; inspect the previous run, do not replay') from None
            try:
                os.close(fd)
                yield
            finally:
                lock.unlink()

    def load(self):
        if not self.path.exists():
            return {'schema_version': 2, 'limits': LIMITS, 'rounds': ROUNDS,
                    'budget_slot_aliases': BUDGET_ALIASES, 'batches': {}, 'cases': {}}
        data = json.loads(self.path.read_text('utf-8'))
        if (data.get('schema_version') != 2 or data.get('limits') != LIMITS or data.get('rounds') != ROUNDS
                or data.get('budget_slot_aliases') != BUDGET_ALIASES):
            raise BatchError('Existing ledger schema/caps differ; refusing reset')
        return data

    def begin(self, name, preflight_digest):
        with self.transaction():
            data = self.load()
            if slot_claimed(data,name):
                raise BatchError('Batch budget slot already claimed, including C3/C3P alias; query/recover only')
            data['batches'][name] = {'status': 'RUNNING', 'started_at': now(),
                'preflight_sha256': preflight_digest, 'spec': ROUNDS[name],
                'budget_slot': budget_slot(name), 'partial_scope': name=='C3P',
                'scope_note': 'Three-account concurrency3 partial acceptance; original C3 >=5 and C5/SOAK >=10 remain unverified' if name=='C3P' else None}
            common.atomic_json(self.path, data)

    def reserve(self, case, category, endpoint, request_hash):
        if not CASE.fullmatch(case) or category not in LIMITS:
            raise BatchError('Invalid case/category')
        name = case.rsplit('-', 1)[0]
        with self.transaction():
            data = self.load()
            if name not in data['batches'] or case in data['cases']:
                raise BatchError('No new batch or case already charged; never resend')
            cases = data['cases'].values()
            total = sum(v['category'] == category for v in cases)
            local = sum(v['category'] == category and budget_slot(v['round']) == budget_slot(name) for v in cases)
            if total >= LIMITS[category] or local >= ROUNDS[name][category]:
                raise BatchError('Fixed generation budget exhausted')
            data['cases'][case] = {'case_id': case, 'round': name, 'category': category,
                'status': 'INTENT', 'intent_at': now(), 'endpoint': endpoint,
                'request_sha256': request_hash, 'units': 1, 'automatic_replay': False,
                'generation_post_count': 0}
            common.atomic_json(self.path, data)

    def update(self, case, **changes):
        with self.transaction():
            data = self.load()
            if case not in data['cases']:
                raise BatchError('Missing original intent')
            data['cases'][case].update(changes, updated_at=now())
            common.atomic_json(self.path, data)

    def mark_send(self, case):
        with self.transaction():
            data = self.load(); entry = data['cases'].get(case)
            if not entry or entry['status'] != 'INTENT' or entry.get('send_started_at'):
                raise BatchError('Case already started; no replay allowed')
            entry.update(status='SENDING', send_started_at=now(), generation_post_count=1)
            common.atomic_json(self.path, data)

    def folder(self, case):
        if not CASE.fullmatch(case):
            raise BatchError('Invalid case ID')
        p = self.root/'cases'/case; p.mkdir(parents=True, exist_ok=True)
        return p

    def summary(self, name=None):
        data = self.load(); rows = [v for v in data['cases'].values() if not name or v['round'] == name]
        use = Counter(v['category'] for v in data['cases'].values())
        aliases = Counter(v.get('account_alias', 'UNBOUND') for v in rows)
        return {'schema_version': 2, 'round': name, 'status': data['batches'].get(name, {}).get('status', 'NOT_RUN'),
            'budget_slot': budget_slot(name) if name else None, 'partial_scope': name=='C3P',
            'original_target_status': 'NOT_RUN' if name=='C3P' else None,
            'scope_note': 'Three-account concurrency3 only; this does not certify original >=5/10 account targets' if name=='C3P' else None,
            'consumed': {k: use[k] for k in LIMITS}, 'remaining': {k: LIMITS[k]-use[k] for k in LIMITS},
            'case_count': len(rows), 'states': dict(Counter(v['status'] for v in rows)),
            'accounts': dict(aliases), 'generation_post_intents': sum(v['generation_post_count'] for v in rows),
            'post_count_scope': 'Conservatively charged client POST attempts; not upstream wire submissions',
            'cases': [{k: v[k] for k in ('case_id','category','status','account_alias','http_status',
                'error_class','failure_class','queue_wait_seconds','latency_seconds','characters','bytes','sha256') if k in v} for v in rows]}


def identities(path, name):
    data = json.loads(Path(path).read_text('utf-8-sig'))
    if data.get('upstream_identity_verified') is not True or data.get('platform') != 'linux' or data.get('backend') != 'protocol':
        raise BatchError('Real Linux protocol upstream identity preflight is required')
    accounts = [a for a in data.get('accounts', []) if a.get('enabled', True)]
    if len(accounts) < ROUNDS[name]['accounts'] or len(accounts) > 10:
        raise BatchError('Independent account threshold not met')
    for field in ('alias','account_id','upstream_identity_sha256'):
        values = [a.get(field) for a in accounts]
        if any(not isinstance(v, str) or not v for v in values) or len(set(values)) != len(values):
            raise BatchError('Missing/duplicate account identity; Cookie variation is insufficient')
    if any(not re.fullmatch(r'[0-9a-f]{64}', a['upstream_identity_sha256']) for a in accounts):
        raise BatchError('Use a verified upstream principal digest, not a local alias')
    return {a['account_id']: a['alias'] for a in accounts}


def plan(name):
    spec = ROUNDS[name]
    items = [{'kind': ('chat','chat_sse','responses','responses_sse')[i % 4], 'category':'text'} for i in range(spec['text'])]
    media = [{'kind': 'edit' if name != 'L1' or i % 2 else 'image', 'category':'image'} for i in range(spec['image'])]
    media.append({'kind':'video','category':'video'})
    for i, item in enumerate(media):
        items.insert(min(len(items), int((i+1)*sum(spec[k] for k in LIMITS)/(len(media)+1))), item)
    for i, item in enumerate(items, 1):
        item['case_id'] = f'{name}-{i:03d}'
    return items


def fixtures(root):
    from PIL import Image, ImageDraw
    out = []
    for i, color in enumerate(('red','blue')):
        path = root/f'reference-{i+1}.png'
        if not path.exists():
            image = Image.new('RGB', (64,64), 'white'); draw = ImageDraw.Draw(image)
            if i: draw.ellipse((12,12,52,52), fill=color)
            else: draw.rectangle((12,12,52,52), fill=color)
            image.save(path)
        out.append(path)
    return out


def request_for(item, references):
    kind, case = item['kind'], item['case_id']; marker = 'PHASE2_' + case.replace('-', '_')
    old = {'chat':'T1','chat_sse':'T2','responses':'T3','responses_sse':'T4','image':'I1','edit':'I2','video':'V1'}[kind]
    prompt = (f'Acceptance marker {marker}. Reply only with {marker}.' if item['category']=='text' else
        f'Acceptance marker {marker}. ' + ('A blue circle moving slowly on plain white, five seconds.' if kind=='video' else
        'Place the red-square first reference left of the blue-circle second reference on white.' if kind=='edit' else
        'A simple blue circle centered on white.'))
    payload = common.payload(old, prompt=prompt)
    if kind == 'edit': body, content_type = common.multipart_edit(payload, references)
    else: body, content_type = json.dumps(payload).encode(), 'application/json'
    return old, marker, common.ENDPOINTS[old], body, content_type


def text_content(old, raw):
    if old in {'T2','T4'}:
        pieces=[]
        for _, value in common.frames(raw):
            if value=='[DONE]': continue
            obj=json.loads(value)
            if old=='T2': pieces.extend(c.get('delta',{}).get('content','') for c in obj.get('choices',[]))
            elif obj.get('type')=='response.output_text.delta': pieces.append(obj.get('delta',''))
        return ''.join(pieces)
    obj=json.loads(raw)
    if old=='T1': return obj['choices'][0]['message']['content']
    return ''.join(p.get('text','') for o in obj.get('output',[]) for p in o.get('content',[]) if p.get('type')=='output_text')


class Runner:
    def __init__(self, state, client, approved, args):
        self.state, self.client, self.approved, self.args = state, client, approved, args
        self.stop = threading.Event(); self.lock = threading.Lock(); self.failures = Counter()
        self.inflight = self.peak = 0

    def call(self, case, path, body=None, content_type='application/json', label='response'):
        folder = self.state.folder(case); token=uuid.uuid4().hex
        receipt={'started_at':now(),'method':'POST' if body is not None else 'GET',
                 'path':path,'label':label,'request_sha256':sha(body) if body is not None else None}
        began=time.monotonic()
        capture='response.bin' if label=='response' else label+'-'+token+'.bin'
        if body is not None and label!='response':
            (folder/(label+'-request-'+token+'.bin')).write_bytes(body)
        try:
            with self.client.open(path, body, {'Content-Type':content_type,
                    'Idempotency-Key':'phase2-'+case} if body is not None else None) as response:
                raw=common.bounded_read(response,folder/capture)
                receipt.update(http_status=response.status,response_file=capture,response_sha256=sha(raw))
                return response.status,raw
        except Exception as exc:
            receipt['error_class']=type(exc).__name__
            raise
        finally:
            receipt.update(finished_at=now(),elapsed_seconds=round(time.monotonic()-began,3))
            common.atomic_json(folder/('http-'+token+'.json'),receipt)

    def bind(self, case, marker, text=None, *, require_completed=False):
        record=self.state.load()['cases'][case]
        if self.args.operations_file:
            value=json.loads(Path(self.args.operations_file).read_text('utf-8'))
            operations=[v for v in value.values() if marker in v.get('prompt','')]
        else:
            with self.client.open('/admin/protocol/operations?limit=1000') as response:
                rows=json.loads(response.read(common.MAX_RESPONSE))['operations']
            if record.get('operation_id'):
                operations=[v for v in rows if v['id']==record['operation_id']]
            elif text is not None:
                operations=[v for v in rows if v.get('kind')=='chat' and v.get('result',{}).get('text')==text]
            else: operations=[]
        if len(operations)!=1:
            raise SafetyError('BINDING_AMBIGUOUS' if operations else 'BINDING_MISSING',
                              'Original operation binding missing or ambiguous; no ID guessed')
        op=operations[0]; aid=op.get('account_id'); result=op.get('result') or {}
        if aid not in self.approved:
            raise SafetyError('CROSS_ACCOUNT','Operation belongs to an unapproved account')
        if record.get('operation_id') and record['operation_id']!=op.get('id'):
            raise SafetyError('IDENTITY_CONFLICT','Recorded operation ID conflicts with own marker operation')
        if record.get('task_id') and op.get('task_id') is not None and op['task_id']!=record['task_id']:
            raise SafetyError('IDENTITY_CONFLICT','Operation refers to another task')
        if record.get('task_account_id') and record['task_account_id']!=aid:
            raise SafetyError('CROSS_ACCOUNT','Task account differs from original operation account')
        if record.get('account_id') and record['account_id']!=aid:
            raise SafetyError('CROSS_ACCOUNT','Original operation changed account ownership')
        if op.get('kind')!=('chat' if record['category']=='text' else record['category']):
            raise SafetyError('IDENTITY_CONFLICT','Operation category differs from original request')
        if result.get('operation_id') is not None and result['operation_id']!=op.get('id'):
            raise SafetyError('IDENTITY_CONFLICT','Operation result belongs to another operation')
        if text is not None and result.get('text')!=text:
            raise SafetyError('TEXT_MISMATCH','Returned text differs from this case own operation result')
        self.state.update(case,operation_id=op['id'],account_id=aid,account_alias=self.approved[aid],
            session_id=op.get('session_id'),message_id=op.get('message_id'),vm_id=op.get('vm_id'),
            operation_state=op.get('state'),operation_delivery=op.get('delivery'),
            operation_error_code=op.get('code'),operation_error=op.get('error'),
            operation_upstream_status=op.get('upstream_status'),
            operation_result=result,operation_binding_verified=True)
        common.atomic_json(self.state.folder(case)/'operation.json',op)
        if require_completed and (op.get('state')!='completed' or op.get('delivery')!='completed'):
            raise SafetyError('OPERATION_NOT_COMPLETED','Own operation is not completed and delivered')
        return op

    def task_identity(self,case,task,*,initial=False):
        record=self.state.load()['cases'][case]
        ids=[task[k] for k in ('id','task_id') if task.get(k) is not None]
        if not ids or any(not isinstance(v,str) or not v for v in ids) or len(set(ids))!=1:
            raise SafetyError('IDENTITY_CONFLICT','Task response has missing or inconsistent task IDs')
        tid=ids[0]
        if not initial and not record.get('task_id'):
            raise SafetyError('IDENTITY_CONFLICT','No recorded original task ID')
        if record.get('task_id') and record['task_id']!=tid:
            raise SafetyError('IDENTITY_CONFLICT','Returned task differs from original task')
        oid=task.get('operation_id'); account=task.get('account')
        if oid is not None and (not isinstance(oid,str) or not oid):
            raise SafetyError('IDENTITY_CONFLICT','Task operation identifier has invalid type')
        if oid and record.get('operation_id') and oid!=record['operation_id']:
            raise SafetyError('IDENTITY_CONFLICT','Task changed its original operation ID')
        if account is not None and (not isinstance(account,str) or not account):
            raise SafetyError('CROSS_ACCOUNT','Task account identifier has invalid type')
        if account and record.get('task_account_id') and account!=record['task_account_id']:
            raise SafetyError('CROSS_ACCOUNT','Task changed account ownership while polling')
        if account and record.get('account_id') and account!=record['account_id']:
            raise SafetyError('CROSS_ACCOUNT','Task account conflicts with bound original operation')
        if task.get('kind') is not None and task['kind']!=record['category']:
            raise SafetyError('IDENTITY_CONFLICT','Task response category mismatch')
        updates={'task_id':tid,'task_status':task.get('status')}
        if oid: updates['operation_id']=oid
        if account: updates['task_account_id']=account
        if task.get('error') is not None: updates['upstream_error']=task['error']
        if task.get('code') is not None: updates['upstream_error_code']=task['code']
        self.state.update(case,**updates)
        return tid

    def poll(self, case):
        record=self.state.load()['cases'][case]; path=record.get('query_path')
        if not path: raise BatchError('No recorded original task ID')
        deadline=time.monotonic()+self.args.task_timeout; index=0
        while True:
            index+=1; _,raw=self.call(case,path,label=f'task-{index:04d}'); task=json.loads(raw)
            common.atomic_json(self.state.folder(case)/'task.json',task)
            self.task_identity(case,task)
            if task.get('status') in {'completed','failed','unknown','delivery_failed'}: return task
            if time.monotonic()>=deadline: raise TimeoutError('Original task polling deadline')
            time.sleep(2)

    def media(self, case, task, category):
        if task.get('status')!='completed':
            self.state.update(case, upstream_error_code=task.get('code'), upstream_error=task.get('error'))
            raise BatchError('Original task did not complete')
        url=task.get('url') or task.get('result',{}).get('url')
        if not url and task.get('data'): url=task['data'][0].get('url')
        parts=urllib.parse.urlsplit(url or ''); name=urllib.parse.unquote(parts.path.removeprefix('/v1/media/'))
        if not parts.path.startswith('/v1/media/') or not name or any(x in name for x in ('/','\\','..')):
            raise BatchError('Only original local /v1/media files can be downloaded')
        record=self.state.load()['cases'][case]; own=record.get('operation_result') or {}
        expected_name=own.get('filename'); expected_size=own.get('size')
        if not isinstance(expected_name,str) or name!=expected_name:
            raise SafetyError('IDENTITY_CONFLICT','Media URL differs from own operation result filename')
        if type(expected_size) is not int or expected_size<1:
            raise SafetyError('DATA_CORRUPTION','Own operation media byte count is missing or invalid')
        result=task.get('result') or {}
        if result.get('filename') is not None and result['filename']!=expected_name:
            raise SafetyError('IDENTITY_CONFLICT','Task result filename differs from own operation')
        for key in ('size','bytes'):
            if result.get(key) is not None and result[key]!=expected_size:
                raise SafetyError('DATA_CORRUPTION','Task result size differs from own operation')
        urls=[v for v in (task.get('url'),result.get('url')) if v]
        urls.extend(v['url'] for v in task.get('data',[]) if isinstance(v,dict) and v.get('url'))
        if any(urllib.parse.unquote(urllib.parse.urlsplit(v).path)!=('/v1/media/'+expected_name) for v in urls):
            raise SafetyError('IDENTITY_CONFLICT','Task exposes a foreign media result URL')
        folder=self.state.folder(case); output=folder/('media'+Path(name).suffix); partial=output.with_suffix(output.suffix+'.part')
        count=0; digest=hashlib.sha256()
        try:
            with self.client.open(parts.path) as response, partial.open('xb') as stream:
                while block:=response.read(1024*1024):
                    count+=len(block)
                    if count>256*1024*1024: raise BatchError('Media size limit exceeded')
                    stream.write(block); digest.update(block)
            if count!=expected_size: raise SafetyError('DATA_CORRUPTION','Downloaded bytes differ from own operation size')
            os.replace(partial,output)
        finally: partial.unlink(missing_ok=True)
        decoded=decoder.check_file(output,SimpleNamespace(kind='video' if category=='video' else 'image',
            ffprobe=self.args.ffprobe,ffmpeg=self.args.ffmpeg,timeout=300))
        common.atomic_json(folder/'decode.json',decoded)
        self.state.update(case,bytes=count,sha256=digest.hexdigest(),downloaded_file=str(output),media_decode=decoded,media_identity_verified=True)
        if decoded['status']!='PASS': raise SafetyError('DECODE_BLOCKED' if decoded['status']=='BLOCKED' else 'DATA_CORRUPTION','Full media decode did not pass')

    def execute(self,item,references,not_before):
        case=item['case_id']
        if self.stop.wait(max(0,not_before-time.monotonic())): return
        if self.stop.is_set(): return
        old,marker,path,body,content_type=request_for(item,references)
        self.state.reserve(case,item['category'],path,sha(body)); folder=self.state.folder(case)
        (folder/'request.bin').write_bytes(body)
        self.state.update(case, marker=marker, kind=item['kind'],
            queue_wait_seconds=round(max(0,time.monotonic()-not_before),3))
        with self.lock:
            if self.stop.is_set():
                self.state.update(case,status='STOPPED_BEFORE_SEND')
                return
            self.state.mark_send(case)
            self.inflight+=1; self.peak=max(self.peak,self.inflight)
        started=time.monotonic()
        try:
            status,raw=self.call(case,path,body,content_type)
            self.state.update(case,http_status=status)
            if item['category']=='text':
                details=common.validate_text(old,raw); text=text_content(old,raw)
                self.bind(case,marker,text,require_completed=True); self.state.update(case,**details)
            else:
                task=json.loads(raw); tid=self.task_identity(case,task,initial=True)
                self.state.update(case,
                    query_path=('/v1/videos/' if item['kind']=='video' else '/v1/images/tasks/')+urllib.parse.quote(tid,safe=''))
                task=self.poll(case); self.bind(case,marker,require_completed=task.get('status')=='completed'); self.media(case,task,item['category'])
            self.state.update(case,status='PASS')
        except Exception as exc:
            category='UNKNOWN' if not isinstance(exc,urllib.error.HTTPError) and isinstance(exc,(TimeoutError,ConnectionError,urllib.error.URLError)) else 'FAIL'
            code=exc.code if isinstance(exc,urllib.error.HTTPError) else None
            if isinstance(exc,urllib.error.HTTPError):
                common.bounded_read(exc,folder/'error-response.bin')
            common.atomic_json(folder/'error.json',{'error_type':type(exc).__name__,'message':str(exc),'http_status':code})
            self.state.update(case,status=category,error_class=type(exc).__name__,
                **({'http_status':code} if code is not None else {}))
            if not isinstance(exc,SafetyError):
                try: self.bind(case,marker)
                except Exception as binding:
                    self.state.update(case,binding_error_class=type(binding).__name__)
                    if isinstance(binding,SafetyError): self.stop.set()
            record=self.state.load()['cases'][case]
            if record.get('operation_state')=='unknown': category='UNKNOWN'
            failure=classify_failure(exc,record,code,category)
            self.state.update(case,status=category,failure_class=failure)
            with self.lock:
                self.failures[(failure,code)]+=1
                if failure in STOP_FAILURES or category=='UNKNOWN' or self.failures[(failure,code)]>=2:
                    self.stop.set()
        finally:
            self.state.update(case,latency_seconds=round(time.monotonic()-started,3),finished_at=now())
            with self.lock: self.inflight-=1


def run_batch(state, client, args):
    if slot_claimed(state.load(),args.round): raise BatchError('Batch/C3 alias budget slot already claimed; no rerun')
    if not sys.platform.startswith('linux'): raise BatchError('Real batch execution requires Linux; no Windows fallback')
    accounts=identities(args.preflight,args.round)
    if not shutil.which(args.ffmpeg or 'ffmpeg') or not shutil.which(args.ffprobe or 'ffprobe'):
        raise BatchError('Full video decoders must be installed before sending')
    import PIL.Image
    with client.open('/admin/backend') as response: backend=json.loads(response.read())
    if backend.get('backend')!='protocol' or not backend.get('running'): raise BatchError('Protocol backend not running')
    with client.open('/admin/accounts') as response: rows=json.loads(response.read())
    rows=rows if isinstance(rows,list) else rows['accounts']
    if {a['id'] for a in rows if a.get('enabled',True)}!=set(accounts): raise BatchError('Service account pool differs from approved preflight')
    lock=state.root/'phase2-run.lock'
    try: fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    except FileExistsError: raise BatchError('Another batch/stale execution lock exists; inspect without replay') from None
    os.close(fd)
    try:
        state.begin(args.round,sha(Path(args.preflight).read_bytes()))
        refs=fixtures(state.root); items=plan(args.round); runner=Runner(state,client,accounts,args)
        start=time.monotonic(); duration=7200 if args.round=='SOAK' else 0
        with ThreadPoolExecutor(max_workers=ROUNDS[args.round]['concurrency']) as pool:
            jobs=[pool.submit(runner.execute,item,refs,start+(duration*i/max(1,len(items)-1))) for i,item in enumerate(items)]
            for job in as_completed(jobs):
                try: job.result()
                except Exception: runner.stop.set()
        summary=state.summary(args.round); passed=summary['states'].get('PASS',0)
        required=sum(ROUNDS[args.round][k] for k in LIMITS)
        complete=passed==required and len(set(summary['accounts'])-{'UNBOUND'})>=ROUNDS[args.round]['accounts']
        with state.transaction():
            data=state.load(); data['batches'][args.round].update(status='PASS' if complete else 'INCOMPLETE',
                finished_at=now(),client_peak_inflight=runner.peak,elapsed_seconds=round(time.monotonic()-start,3),
                server_or_upstream_parallelism_not_inferred_from_client_count=True)
            common.atomic_json(state.path,data)
        summary=state.summary(args.round); common.atomic_json(state.root/(args.round+'-summary.json'),summary)
        return summary
    finally: lock.unlink()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('status','run','query','recover'))
    parser.add_argument('--private-root',type=Path,required=True)
    parser.add_argument('--round',choices=tuple(ROUNDS),required=True)
    parser.add_argument('--base',required=False)
    parser.add_argument('--preflight',type=Path)
    parser.add_argument('--operations-file',type=Path,help='Optional read-only new Store JSON for exact prompt-marker identity binding')
    parser.add_argument('--case')
    parser.add_argument('--key-env',default='MUSE2API_KEY')
    parser.add_argument('--request-timeout',type=int,default=900)
    parser.add_argument('--task-timeout',type=int,default=900)
    parser.add_argument('--ffprobe'); parser.add_argument('--ffmpeg')
    args=parser.parse_args(argv)
    try:
        state=State(args.private_root)
        if args.command=='status': result=state.summary(args.round)
        else:
            if not args.base: raise BatchError('Explicit loopback --base required')
            client=common.Client(args.base,os.environ.get(args.key_env,''),min(1200,max(1,args.request_timeout)))
            if args.command=='run':
                args.preflight=args.preflight or state.root/'preflight.json'
                result=run_batch(state,client,args)
            else:
                if state.root.joinpath('phase2-run.lock').exists(): raise BatchError('Wait for the active batch before diagnostic actions')
                if not args.case or not args.case.startswith(args.round+'-'): raise BatchError('Recorded original --case required')
                record=state.load()['cases'].get(args.case)
                if not record: raise BatchError('No original case intent')
                runner=Runner(state,client,{},args)
                if args.command=='query': runner.poll(args.case)
                else:
                    oid=record.get('operation_id')
                    if not oid or record.get('operation_binding_verified') is not True or not record.get('account_id'):
                        raise SafetyError('IDENTITY_CONFLICT','Recovery requires a verified original operation/account binding, not an unverified returned ID')
                    _,raw=runner.call(args.case,'/admin/protocol/operations/'+urllib.parse.quote(oid,safe='')+'/recover',
                        json.dumps({'timeout':60}).encode(),label='recovery')
                    data=json.loads(raw)
                    if data.get('resubmitted') is not False: raise BatchError('Recovery did not attest resubmitted:false')
                    state.update(args.case,recovery_status='RECOVERED' if data.get('ok') else 'UNKNOWN')
                result=state.summary(args.round)
        print(json.dumps(result,ensure_ascii=False)); return 0 if result.get('status') in {'PASS','NOT_RUN'} else 1
    except Exception as exc:
        print(json.dumps({'status':'BLOCKED','error_class':type(exc).__name__,
            'note':'Inspect private preflight/ledger; no automatic replay performed'})); return 2


if __name__=='__main__':
    raise SystemExit(main())
