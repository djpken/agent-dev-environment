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

function page({holdVerify=false, registrationRequired=false} = {}) {
  const dom = elements();
  const listeners = new Map();
  let needsRegistration = registrationRequired;
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
    if (!dom.has(id)) dom.set(id, {hidden:true, disabled:true, textContent:'', innerHTML:'', className:'',
      value:id === 'trending-source' ? 'github' : id === 'trending-since' ? 'daily' : ''});
    return dom.get(id);
  };
  let releaseVerify, verifyStarted;
  const started = new Promise(resolve => { verifyStarted = resolve; });
  const gate = new Promise(resolve => { releaseVerify = resolve; });
  const context = vm.createContext({
    console, TextEncoder, Date, Uint8Array, btoa:value => Buffer.from(value, 'binary').toString('base64'),
    document:{body:{dataset:{registrationRequired:String(registrationRequired)}}, getElementById:get, querySelectorAll:() => []},
    sessionStorage:{getItem:key => storage.get(key) || null, setItem:(key,value) => storage.set(key,value), removeItem:key => storage.delete(key)},
    setTimeout:() => 1, clearTimeout:() => {},
    Event:class {}, location:{origin:'https://vm.example'},
    window:{confirm:() => true, ethereum:provider, addEventListener:() => {}, dispatchEvent:() => {}},
    async fetch(url, options={}) {
      const payload = options.body ? JSON.parse(options.body) : null;
      requests.push({url, payload, headers:options.headers});
      let data = {}, status = 200;
      if (url.startsWith('/api/v1/registration/challenge')) {
        if (needsRegistration) data={challenge_id:'registration-nonce', message:'register'};
        else { status=409; data={error:'registration is already complete'}; }
      }
      else if (url === '/api/v1/registration') { needsRegistration=false; data={registered:true}; }
      else if (url.startsWith('/api/v1/trending')) data={source:'github', label:'GitHub Trending', since:'daily', sourceUrl:'https://github.com/trending?since=daily', fetchedAt:new Date().toISOString(), items:[{fullName:'owner/repo',name:'repo',author:'owner',url:'https://github.com/owner/repo',language:'Python',starsToday:12,starsTotal:1200,description:'A sample repo'}]};
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
      else if (url === '/api/v1/artifacts' && options.method === 'POST') data={name:payload.name,access_url:'http://vm.example:80/artifacts/report/index.html'};
      else if (url === '/api/v1/artifacts' && !options.method) data={enabled:true,publisher_ready:true,artifacts:[{name:'report',file_count:1,size_bytes:20,pages:[{entrypoint:'index.html',access_url:'http://vm.example:80/artifacts/report/index.html'}]}]};
      else if (url === '/api/v1/artifacts/report' && options.method === 'DELETE') data={deleted:true};
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
  assert.equal(requests.length, 1, 'anonymous page only fetches the public ranking');
  assert.equal(requests[0].url, '/api/v1/trending?source=github&since=daily');
  assert.equal(requests[0].headers.Authorization, undefined, 'public ranking does not require a wallet session');
  assert.equal(first.get('trending-items').innerHTML.includes('owner/repo'), true);
  assert.equal(first.get('auth-title').textContent, '登入 ADES');
  assert.equal(first.get('wallet').textContent, '使用 MetaMask 登入');
  assert.equal(first.get('auth-panel').hidden, false);
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

  assert.equal(first.get('artifact-panel').hidden, false);
  assert.equal(first.get('artifact-form').hidden, false);
  assert.match(first.get('artifacts').innerHTML, /rel="noopener noreferrer"/);
  first.get('artifact-name').value = 'report';
  first.get('artifact-entrypoint').value = 'index.html';
  first.run("state.uploadFiles = [{path:'index.html',file:{arrayBuffer:async () => new TextEncoder().encode('<h1>Report</h1>').buffer}}]");
  await first.get('artifact-form').onsubmit({preventDefault(){}});
  const uploaded = requests.find(r => r.url === '/api/v1/artifacts' && r.payload);
  assert.equal(uploaded.payload.overwrite, true);
  assert.equal(Buffer.from(uploaded.payload.files[0].content_base64, 'base64').toString(), '<h1>Report</h1>');
  await first.run("deleteArtifact('report')");
  assert.equal(signatures, 1, 'publishing must reuse the wallet session');

  const restored = page();
  await settle();
  assert.equal(restored.get('private').hidden, false, 'reload should restore authenticated data');
  assert.equal(signatures, 1, 'reload must not sign');
  await restored.get('logout').onclick();
  assert.equal(restored.get('private').hidden, true);
  assert.equal(storage.size, 0);
  assert.equal(restored.get('artifacts').textContent, '');
  assert.equal(restored.get('artifact-panel').hidden, true);
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

  const bootstrap = page({registrationRequired:true});
  await settle();
  assert.equal(bootstrap.get('auth-title').textContent, '註冊這台環境');
  assert.equal(bootstrap.get('wallet').textContent, '使用 MetaMask 註冊');
  const beforeRegistrationSignatures = signatures;
  await bootstrap.get('wallet').onclick();
  assert.equal(signatures - beforeRegistrationSignatures, 2, 'first registration signs once, then signs in once');
  assert.equal(requests.some(request => request.url === '/api/v1/registration'), true);
  assert.equal(bootstrap.get('auth-panel').hidden, true);
  bootstrap.run('resetWallet()');
  assert.equal(bootstrap.get('wallet').textContent, '使用 MetaMask 登入', 'completed registration changes the center action to login');
  console.log('dashboard registration mode, session, controls, reload, logout, and account-change checks passed');
})().catch(error => { console.error(error); process.exitCode=1; });
