const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync(0, 'utf8');
const address = '0x' + '1'.repeat(40);
const expires_at = new Date(Date.now() + 12 * 3600000).toISOString();
const storage = new Map();
const session = {session:'test-session', role:'admin', address, expires_at};
let signatures = 0, revocations = 0, policyEnabled = false;
const requests = [];
const elements = () => new Map();

function page({holdVerify=false} = {}) {
  const dom = elements();
  const listeners = new Map();
  const provider = {
    isMetaMask:true,
    on(name, listener) { listeners.set(name, listener); },
    removeListener(name) { listeners.delete(name); },
    async request({method}) {
      if (method === 'personal_sign') { signatures++; return 'wallet-signature'; }
      if (method === 'eth_requestAccounts') listeners.get('accountsChanged')?.([address]);
      return [address];
    },
  };
  const get = id => {
    if (!dom.has(id)) dom.set(id, {hidden:true, disabled:true, textContent:'', innerHTML:'', className:''});
    return dom.get(id);
  };
  let releaseVerify, verifyStarted;
  const started = new Promise(resolve => { verifyStarted = resolve; });
  const gate = new Promise(resolve => { releaseVerify = resolve; });
  const context = vm.createContext({
    console, TextEncoder, Date,
    document:{getElementById:get, querySelectorAll:() => []},
    sessionStorage:{getItem:key => storage.get(key) || null, setItem:(key,value) => storage.set(key,value), removeItem:key => storage.delete(key)},
    setTimeout:() => 1, clearTimeout:() => {},
    Event:class {}, location:{origin:'https://vm.example'},
    window:{ethereum:provider, addEventListener:() => {}, dispatchEvent:() => {}},
    async fetch(url, options={}) {
      const payload = options.body ? JSON.parse(options.body) : null;
      requests.push({url, payload, headers:options.headers});
      let data = {}, status = 200;
      if (url.startsWith('/api/v1/registration/challenge')) { status=409; data={error:'registration is already complete'}; }
      else if (url.startsWith('/api/v1/auth/challenge')) data={challenge_id:'nonce', message:'login'};
      else if (url === '/api/v1/auth/verify') {
        verifyStarted();
        if (holdVerify) await gate;
        data=session;
      }
      else if (url === '/api/v1/auth/session') data=session;
      else if (url === '/api/v1/auth/logout') { revocations++; data={signed_out:true}; }
      else if (url === '/api/v1/health') data={status:'healthy', vm_id:'vm', checked_at:'now', components:[{id:'codex',label:'Codex',status:'healthy',update_supported:true}]};
      else if (url === '/api/v1/schedule') data={schedule:{time:'04:00',timezone:'UTC+8',persistent:true},policy_valid:policyEnabled,policy_reason:'disabled'};
      else if (url === '/api/v1/runs') data={runs:[]};
      else if (url === '/api/v1/policy') { policyEnabled=payload.enabled; data={valid:policyEnabled}; }
      else if (url === '/api/v1/update-runs') data={id:'run',status:'queued'};
      else assert.fail('Unexpected request: ' + url);
      return {ok:status<400, status, json:async () => data};
    },
  });
  vm.runInContext(source, context);
  return {context, get, listeners, started, releaseVerify, run:code => vm.runInContext(code, context)};
}

const settle = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  const first = page();
  await settle();
  assert.equal(requests.length, 0, 'anonymous page must not fetch environment data');
  await first.get('wallet').onclick();
  assert.equal(signatures, 1, 'normal login signs once even when MetaMask emits accountsChanged');
  assert.equal(first.get('private').hidden, false);
  assert.equal(storage.size, 1);
  await first.get('update-all').onclick();
  await first.get('authorize-policy').onclick();
  assert.equal(signatures, 1, 'updates and policy changes must not sign again');
  const policy = requests.find(request => request.url === '/api/v1/policy').payload;
  assert.equal(Object.hasOwn(policy, 'expires_at'), false);
  assert.equal(Object.hasOwn(policy, 'signature'), false);
  assert.equal(first.get('schedule').textContent.includes('04:00 UTC+8'), true);
  await first.get('disable-policy').onclick();
  assert.equal(policyEnabled, false);

  const restored = page();
  await settle();
  assert.equal(restored.get('private').hidden, false, 'reload should restore authenticated data');
  assert.equal(signatures, 1, 'reload must not sign');
  await restored.get('logout').onclick();
  assert.equal(restored.get('private').hidden, true);
  assert.equal(storage.size, 0);
  assert.equal(revocations, 1);

  // Logging out during authentication must revoke the late server session.
  const late = page({holdVerify:true});
  const connecting = late.get('wallet').onclick();
  await late.started;
  late.run('resetWallet()');
  late.releaseVerify();
  await connecting;
  assert.equal(late.get('private').hidden, true);
  assert.equal(storage.size, 0);
  assert.equal(revocations, 2);

  const changing = page();
  await changing.get('wallet').onclick();
  changing.listeners.get('accountsChanged')(['0x' + '2'.repeat(40)]);
  await settle();
  assert.equal(changing.get('private').hidden, true);
  assert.equal(storage.size, 0);
  assert.equal(revocations, 3);
  console.log('dashboard session, controls, reload, logout, and account-change checks passed');
})().catch(error => { console.error(error); process.exitCode=1; });
