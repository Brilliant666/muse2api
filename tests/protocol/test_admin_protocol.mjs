/** Real admin function behavior in a minimal DOM harness; no browser or network. */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const html = readFileSync(new URL('../../admin.html', import.meta.url), 'utf8');
function source(start, end) {
  const at = html.indexOf(start), until = html.indexOf(end, at + start.length);
  assert.ok(at >= 0 && until > at, `missing function boundary ${start}`);
  return html.slice(at, until);
}
function harness(parts, extras = {}) {
  const elements = new Map(), calls = [], notices = [];
  const get = id => {
    if (!elements.has(id)) elements.set(id, {value:'test prompt', disabled:false, innerHTML:'', textContent:'',
      classList:{add(){},remove(){}}, dataset:{}});
    return elements.get(id);
  };
  const context = {console, Date, JSON, Error, encodeURIComponent, backendInfo:{backend:'protocol',updater_supported:false},
    _lastRepoStatus:null, _notifiedSha:'', _dismissedSha:'', LANG:'zh',
    $:get, esc:s=>String(s ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    api:async(...args)=>{calls.push(args); return {};}, fetch:()=>{throw Error('unexpected external fetch');},
    toast:(...args)=>notices.push(args), confirm:()=>true, loadTasks(){},loadPool(){},loadMedia(){},
    fmtBytes:()=>'', setTimeout:()=>{throw Error('unexpected polling');}, ...extras};
  vm.createContext(context);
  vm.runInContext(parts.join('\n'), context);
  return {context, elements, calls, notices, get};
}
const label = source('function operationLabel(', '\nfunction protocolActions(');
const actions = source('function protocolActions(', '\nasync function loadProtocolOperations(');
const tests = [];
function test(name, run) { tests.push({name,run}); }

test('unknown and delivery failures offer recovery without replay action', () => {
  const h = harness([label,actions]);
  for (const state of ['unknown','delivery_failed']) {
    const buttons = h.context.protocolActions('op_<unsafe>',state);
    assert.match(buttons,/data-protocol-action="recover"/);
    assert.match(buttons,/data-protocol-action="abandon"/);
    assert.ok(!buttons.includes('op_<unsafe>'));
  }
  assert.equal(h.context.protocolActions('op_1','completed'),'');
  assert.equal(h.context.operationLabel('unknown'),'结果待确认');
});

test('protected update check never falls through to upstream GitHub fallback', async () => {
  const h = harness([source('async function loadRepoStatus(', '\nasync function upgradeNow(')],
    {api:async()=>({disabled:true,has_update:false})});
  await h.context.loadRepoStatus(true);
  assert.equal(h.get('btnRepoPull').disabled,true);
  assert.equal(h.get('btnBannerUpgrade').disabled,true);
  assert.equal(h.get('repoBadge').textContent,'协议版本');
});

test('protected upgrade action does not invoke update or confirmation', async () => {
  const h = harness([source('async function upgradeNow(', '\n/* ================= 总览刷新')],
    {confirm:()=>{throw Error('unexpected update confirmation');}});
  await h.context.upgradeNow();
  assert.equal(h.calls.length,0);
  assert.equal(h.notices.length,1);
});

test('image outcome unknown stops polling and preserves uncertainty', async () => {
  let calls = 0, refreshes = 0;
  const h = harness([source('async function runImage(', '\nasync function runVideo(')],
    {api:async()=>{calls++;return {id:'task_1',status:'unknown'};},loadTasks:()=>refreshes++});
  await h.context.runImage();
  assert.equal(calls,1);
  assert.equal(refreshes,1);
  assert.equal(h.get('imgBtn').disabled,false);
  assert.match(h.get('imgRes').innerHTML,/结果需要确认/);
  assert.ok(!h.get('imgRes').innerHTML.includes('✗ 生成失败'));
});

test('video delivery failure stops polling and offers existing task recovery', async () => {
  let calls = 0, refreshes = 0;
  const h = harness([label,source('async function runVideo(', '\n/* ================= 官方仓库')],
    {api:async()=>++calls===1?{id:'task_1'}:{status:'delivery_failed'},loadTasks:()=>refreshes++});
  await h.context.runVideo();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls,2);
  assert.equal(refreshes,1);
  assert.equal(h.get('vidBtn').disabled,false);
  assert.match(h.get('vidRes').innerHTML,/文件待取回/);
});

test('abandon requires confirmation and never claims upstream cancellation', async () => {
  const body = source('async function handleProtocolAction(', '\ndocument.addEventListener(');
  let agreed = false;
  const h = harness([label,body],{confirm:()=>agreed});
  const button = {dataset:{protocolAction:'abandon',protocolOperation:'op_1'},disabled:false};
  await h.context.handleProtocolAction(button);
  assert.equal(h.calls.length,0);
  agreed = true;
  await h.context.handleProtocolAction(button);
  assert.equal(h.calls[0][0],'/admin/protocol/operations/op_1/abandon');
  assert.equal(h.calls[0][1].body,'{"confirm":true}');
  assert.match(h.notices[0][0],/上游任务未取消/);
  assert.equal(button.disabled,false);
});

for (const {name,run} of tests) { await run(); console.log('PASS',name); }
console.log(`PASS ${tests.length} admin protocol behaviors; DOM/network boundaries are simulated`);
