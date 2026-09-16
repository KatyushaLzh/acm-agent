const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../tools/acm_agent/web_static/core.js'), 'utf8');
const start = source.indexOf('function unwrap(');
const end = source.indexOf('function filePickerField(', start);
assert(start >= 0 && end > start, 'API source boundaries must exist');
function setup(fetch) {
  const context = vm.createContext({ fetch, state: { token: '' } });
  vm.runInContext(source.slice(start, end), context);
  return context.api;
}

(async () => {
  const networkCause = new TypeError('Failed to fetch');
  await assert.rejects(setup(async () => { throw networkCause; })('/api/ai/connections'), error => {
    assert.equal(error.code, 'local_service_unreachable');
    assert.match(error.message, /ACM Agent 本地服务/);
    assert.match(error.message, /重新运行启动器/);
    assert.match(error.message, /新打开的页面地址/);
    assert.equal(error.cause, networkCause);
    assert.equal(error.status, undefined);
    return true;
  });

  const abort = new DOMException('Canceled', 'AbortError');
  await assert.rejects(setup(async () => { throw abort; })('/api/ai/connections'), error => error === abort);

  const failure = { ok: false, error: { code: 'model_discovery_failed', message: '上游接口拒绝访问' } };
  await assert.rejects(setup(async () => ({ ok: false, status: 502, json: async () => failure }))('/api/ai/connections'), error => {
    assert.equal(error.status, 502);
    assert.equal(error.message, failure.error.message);
    assert.equal(error.payload, failure);
    assert.equal(error.code, undefined);
    return true;
  });

  const data = { connections: [] };
  const api = setup(async (url, request) => {
    assert.equal(url, '/api/ai/connections');
    assert.equal(request.method, 'POST');
    assert.equal(request.headers['Content-Type'], 'application/json');
    assert.equal(request.body, JSON.stringify({ display_name: 'Test' }));
    return { ok: true, status: 200, json: async () => ({ ok: true, data }) };
  });
  assert.equal(await api('/api/ai/connections', { body: { display_name: 'Test' } }), data);
  console.log('PASS: network failure, AbortError, HTTP JSON error, successful response');
})().catch(error => { console.error(error); process.exitCode = 1; });
