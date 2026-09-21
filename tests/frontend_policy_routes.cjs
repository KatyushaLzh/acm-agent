const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const base = path.join(__dirname, '../tools/acm_agent/web_static');
function extract(file, start, end) {
  const source = fs.readFileSync(path.join(base, file), 'utf8');
  const first = source.indexOf(start), last = source.indexOf(end, first);
  assert(first >= 0 && last > first);
  return source.slice(first, last);
}
function input(value) {
  return { value: String(value), setCustomValidity() {}, reportValidity() {}, focus() {} };
}
function setup(saved, draft = saved) {
  const profile = 'summary';
  const row = { fields: {}, grid: { editors: [] }, capability: {}, primary: {} };
  for (const [name, value] of Object.entries({
    max_output_tokens: 2048, request_timeout_seconds: 120, max_retries: 1,
    max_requests: 3, max_total_tokens: 9000,
  })) row.fields[name] = input(value);
  row.grid.editors = draft.map(route => ({
    model: input(JSON.stringify({ provider_id: route.provider_id, model: route.model })),
    strength: input(route.reasoning_strength || 'auto'),
  }));
  const form = { elements: { daily_cny: input(''), monthly_cny: input('') }, row };
  const nodes = { '#ai-policy-form': form };
  const state = { aiStatus: {
    connections: [{ id: 'deepseek', enabled: true, credential: { detected: true }, models: [] }],
    profiles: { summary: { provider_id: 'deepseek', model: 'retired-primary', reasoning_strength: 'high' } },
  } };
  const ctx = vm.createContext({ state, POLICY_PROFILES: [profile], savedAiPolicy: { fallbacks: { summary: saved } },
    aiPolicyDirty: true, asObject: value => value && typeof value === 'object' ? value : {},
    escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;'),
    $(selector, root) {
      if (!root) return nodes[selector];
      if (selector.startsWith('[data-policy-profile=')) return row;
      if (selector.startsWith('[data-budget=')) return row.fields[selector.match(/"(.*?)"/)[1]];
      if (selector === '[data-fallback-model]') return root.model;
      if (selector === '[data-fallback-strength]') return root.strength;
      if (selector === '.policy-fallback-grid') return root.grid;
      if (selector === '.policy-capability') return root.capability;
      if (selector === '.policy-primary-route') return root.primary;
      throw new Error(`Unexpected selector: ${selector}`);
    },
    $$(selector, root) {
      if (selector === '[name], select') return [];
      if (selector === '[data-fallback-slot]') return (root.grid || root).editors;
      throw new Error(`Unexpected selector: ${selector}`);
    },
  });
  vm.runInContext(extract('ai_model_controls.js', 'const PROFILE_LABELS', 'function formatInteger')
    + extract('ai_model_controls.js', 'function modelOptionValue', 'function createOption')
    + extract('view_ai.js', 'function clonePolicy', 'async function repriceAiCosts'), ctx);
  return { ctx, row, state };
}
const stale = { provider_id: 'deepseek', model: 'retired-fallback', reasoning_strength: 'high' };
const identity = value => JSON.parse(JSON.stringify(value));

// A budget edit preserves both the retired model ID and the saved reasoning strength.
{
  const { ctx, row } = setup([stale]);
  row.fields.max_output_tokens.value = '4096';
  const result = ctx.readAiPolicyDraft();
  assert.equal(result.budgets.summary.max_output_tokens, 4096);
  assert.deepEqual(identity(result.fallbacks.summary), [stale]);
  const html = ctx.fallbackEditor('summary', 0, stale);
  assert.match(html, /retired-fallback（已失效）/);
  assert.match(html, /value="high" selected disabled/);
  assert.match(html, /仍可修改预算/);
}
// Removing a retired fallback and repairing another route can both be saved.
{
  const { ctx } = setup([stale], []);
  assert.deepEqual(identity(ctx.readAiPolicyDraft().fallbacks.summary), []);
}
{
  const replacement = { ...stale, model: 'current-model', reasoning_strength: 'auto' };
  const { ctx, state } = setup([stale], [replacement]);
  state.aiStatus.connections[0].models.push({ id: 'current-model',
    effective_capabilities: ['text_chat', 'json_object'], verified_capabilities: ['text_chat', 'json_object'] });
  assert.deepEqual(identity(ctx.readAiPolicyDraft().fallbacks.summary), [replacement]);
}
// Missing providers must also round-trip without silently dropping their saved routes.
{
  const { ctx, state } = setup([stale]);
  state.aiStatus.connections = [];
  assert.deepEqual(identity(ctx.readAiPolicyDraft().fallbacks.summary), [stale]);
  assert.match(ctx.fallbackEditor('summary', 0, stale), /连接已不存在/);
}
// Grandfathering applies to exact saved identities only.
for (const draft of [{ ...stale, model: 'another-retired-model' }, { ...stale, reasoning_strength: 'auto' }]) {
  const { ctx } = setup([stale], [draft]);
  assert.throws(() => ctx.readAiPolicyDraft(), /备用模型不可用/);
}
// A catalog refresh warns about stale primary and fallback routes without losing edits.
{
  const { ctx, row } = setup([stale]);
  row.fields.max_output_tokens.value = '4096';
  ctx.renderAiPolicyEditor({}); // Dirty draft refresh path must not read server budgets.
  assert.equal(row.fields.max_output_tokens.value, '4096');
  assert.equal(ctx.aiPolicyDirty, true);
  assert.match(row.capability.innerHTML, /模型已不在当前可用列表中/);
  assert.match(row.capability.innerHTML, /仍可保存预算/);
  assert.match(row.grid.innerHTML, /retired-fallback（已失效）/);
  assert.match(row.grid.innerHTML, /value="high" selected disabled/);
}
// Refresh uses the persisted primary and keeps server-level readiness errors visible.
{
  const { ctx, row, state } = setup([]);
  state.aiStatus.connections[0].models.push({ id: 'retired-primary',
    effective_capabilities: ['text_chat', 'json_object'], verified_capabilities: ['text_chat', 'json_object'],
    reasoning_strengths: ['high'] });
  state.aiStatus.profiles.summary.ready = false;
  state.aiStatus.profiles.summary.error = '服务端路由检查失败';
  state.aiSelections = { summary: { model_ref: { provider_id: 'other-provider', model: 'unsaved-model' },
    reasoning_strength: 'auto' } };
  ctx.refreshAiPolicyRouteStatus();
  assert.match(row.primary.innerHTML, /retired-primary/);
  assert.doesNotMatch(row.primary.innerHTML, /unsaved-model|other-provider/);
  assert.match(row.capability.innerHTML, /不可用/);
  assert.match(row.capability.innerHTML, /服务端路由检查失败/);
  assert.doesNotMatch(row.capability.innerHTML, /已验证/);
}
console.log('PASS: retired route preservation, repair, strict new routes, dirty catalog refresh');
