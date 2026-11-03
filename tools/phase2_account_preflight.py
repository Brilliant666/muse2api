import sys,os,json,pathlib,time,hashlib,re,logging,urllib.request
from dotenv import dotenv_values
repo=pathlib.Path(sys.argv[1]); root=pathlib.Path(sys.argv[2]); out=root/'account-preflight'
out.mkdir(exist_ok=True)
for k in list(os.environ):
 if k.startswith(('MUSE_','MUSE2API_')): del os.environ[k]
os.environ.update({k:v for k,v in dotenv_values(root/'windows-preflight.env').items() if v is not None})
sys.path.insert(0,str(repo))
from config import Config
from store import Store
from engine import MuseEngine
logging.disable(logging.CRITICAL)
with urllib.request.urlopen('http://127.0.0.1:19212/json/version',timeout=10) as response:
 assert response.status==200
cfg=Config(); store=Store(cfg); engine=MuseEngine(cfg); engine.start()
assert len(store.accounts)==3
mapping=json.loads((root/'account-map.json').read_text('utf8')); salt=(root/'identity-salt.bin').read_bytes()
result={'schema_version':1,'identity_observed_platform':'Windows isolated browser','backend_generation_target':'linux protocol','checked_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'upstream_identity_verified':False,'accounts':[],'generation_requests':0}
try:
 for row in mapping:
  aid=row['account_id']; account=store.get_account(aid); d=out/row['alias']; d.mkdir(exist_ok=True)
  item={**row,'enabled':True,'identity_verified':False,'quota_current':False}
  try:
   engine.ensure_page(account['cookies'],account.get('cookies_exp'),account_id=aid)
   engine.page.send('Emulation.setDeviceMetricsOverride', {'width':1440,'height':2400,'deviceScaleFactor':1,'mobile':False})
   time.sleep(2)
   resources=engine.page.js("JSON.stringify(performance.getEntriesByType('resource').map(x=>x.name).filter(x=>x.includes('/api/')))")
   (d/'resource-urls.json').write_text(str(resources),'utf8')
   engine._quota_click(engine._QUOTA_SETTINGS_JS,'Settings entry unavailable')
   engine._quota_click(engine._QUOTA_MENU_JS,'Settings item unavailable')
   deadline=time.monotonic()+15
   while True:
    text=engine.page.js(engine._QUOTA_DIALOG_JS) or ''
    quota=engine._parse_quota(text)
    if quota['found']: break
    if time.monotonic()>deadline: raise RuntimeError('No current quota in settings dialog')
    time.sleep(.2)
   emails=engine.page.js("JSON.stringify([...document.querySelectorAll('[role=dialog] input[type=email],[aria-modal=true] input[type=email]')].filter(e=>e.getClientRects().length).map(e=>e.value))")
   extra=json.loads(emails) if isinstance(emails,str) else []
   candidates=set(re.findall(r'[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',text+'\n'+'\n'.join(extra)))
   candidates={x.lower() for x in candidates if not x.lower().endswith('@muse.ai')}
   (d/'settings-dialog.txt').write_text(text,'utf8')
   (d/'identity-candidates.json').write_text(json.dumps(sorted(candidates)),'utf8')
   (d/'quota.json').write_text(json.dumps(quota,ensure_ascii=False,indent=2),'utf8')
   item.update(quota_current=True,quota={k:v for k,v in quota.items() if k!='raw'},checked_at=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
   from tools.phase2_identity import authenticated_viewer_id, IdentityUnverified
   import hmac
   html=engine.page.js('document.documentElement.outerHTML') or ''
   (d/'identity-bootstrap.html').write_text(html,'utf8')
   try:
    uid=authenticated_viewer_id(html)
    (d/'principal.json').write_text(json.dumps({'principal':uid,'source':'authenticated viewerId == prelude.user.identifier'}),'utf8')
    item.update(identity_verified=True,identity_source='authenticated AuthContextProvider viewerId and prelude.user.identifier',upstream_identity_sha256=hmac.new(salt,uid.encode(),hashlib.sha256).hexdigest(),identity_hash_scheme='round-private HMAC-SHA256')
   except IdentityUnverified:
    item['identity_candidate_count']=0
   extra_left=re.search(r'剩余\s*([^）\n)]+)',text)
   if extra_left: item['quota']['extra_left_display']=extra_left.group(1).strip()
   item['quota']['weekly_remaining_pct']=100-item['quota']['weekly_used_pct']
  except Exception as exc:

   if engine.page:
    state=engine.page.js("JSON.stringify({url:location.href,body:document.body.innerText,buttons:[...document.querySelectorAll('button,[role=button]')].map(e=>({text:e.innerText,aria:e.getAttribute('aria-label'),testid:e.getAttribute('data-testid'),visible:!!e.getClientRects().length}))})")
    (d/'ui-state.json').write_text(str(state),'utf8')
   item['error_type']=type(exc).__name__; (d/'error.txt').write_text(str(exc),'utf8')
  finally:
   observed=engine.read_cookies()
   if observed:
    values={k:v['value'] for k,v in observed.items() if v['value']}
    expires={k:v['expires'] for k,v in observed.items() if v['expires']>0}
    item['cookies_persisted']=store.persist_protocol_cookies(aid,account['credential_revision'],values,expires)
   engine.close_current_page()
  result['accounts'].append(item)
  (root/'account-preflight.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n','utf8')
  print(json.dumps({'alias':row['alias'],'identity_verified':item['identity_verified'],'quota_current':item['quota_current'],'identity_candidate_count':item.get('identity_candidate_count'),'error_type':item.get('error_type'),'weekly_used_pct':item.get('quota',{}).get('weekly_used_pct'),'extra_left':item.get('quota',{}).get('extra_left')}),flush=True)
 identities=[x.get('upstream_identity_sha256') for x in result['accounts']]
 result['upstream_identity_verified']=all(identities) and len(set(identities))==3
 result['all_current_quotas']=len(result['accounts'])==3 and all(x['quota_current'] for x in result['accounts'])
 result['all_have_weekly_remaining']=all(x.get('quota',{}).get('weekly_remaining_pct',0)>0 for x in result['accounts'])
 result['finished_at']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
 (root/'account-preflight.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n','utf8')
 print(json.dumps({'distinct_identity_verified':result['upstream_identity_verified'],'all_current_quotas':result['all_current_quotas'],'generation_requests':0}),flush=True)
finally:
 engine.stop()
