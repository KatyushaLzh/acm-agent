const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../tools/acm_agent/web_static/ai_model_controls.js'), 'utf8');
const context = vm.createContext({
  state: {aiStatus: {connections: [], profiles: {}}},
  asObject: value => value && typeof value === 'object' ? value : {},
  api: async () => ({connections: [], profiles: {}}),
  statCard: (title, value, detail) => `${title}:${value}:${detail}`,
  window: {confirm: () => {throw new Error('Unexpected confirmation');}},
});
vm.runInContext(source.replace(/^import .*;\r?\n/, '').replace(/export \{[\s\S]*?\};\s*$/, ''), context);
const model = {effective_capabilities: {text_chat: true, json_object: true, streaming: true}, verified_capabilities: ['text_chat', 'json_object', 'streaming'], reasoning_strengths: ['auto'], wire_profile: {streaming: 'buffered', structured_output: 'prompt_json'}};
for (const profile of ['recommendation', 'plan_organize', 'plan_generate', 'coaching', 'patch', 'summary']) {
  assert.equal(context.modelVerifiedForProfile(model, profile, 'auto'), true, profile);
}
assert.equal(context.modelVerifiedForProfile({effective_capabilities: ['text_chat']}, 'patch', 'auto'), false);
assert.equal(context.modelVerifiedForProfile(model, 'coaching', 'high'), false);
assert.equal(context.modelVerifiedForProfile({...model, verified_capabilities: ['text_chat', 'json_object']}, 'coaching', 'auto'), false);
assert.match(context.modelCompatibilityLabel(model), /非流式/);
assert.match(context.modelCompatibilityLabel(model), /兼容输出/);
const auditRoot = {};
context.renderAiAuditCards(auditRoot, {all_model_tokens: {runs: 2, unknown_runs: 1, total_tokens_known: 123}});
assert.match(auditRoot.innerHTML, /123（部分未知）/);
context.renderAiAuditCards(auditRoot, {all_model_tokens: {runs: 2, unknown_runs: 2, total_tokens_known: 0}});
assert.match(auditRoot.innerHTML, /全模型 30 天 Token:未知/);
context.renderAiAuditCards(auditRoot, {all_model_tokens: {runs: 1, unknown_runs: 0, total_tokens_known: 123}, governance: {usage_completeness: 'partial'}});
assert.match(auditRoot.innerHTML, /123（部分未知）/);
vm.runInContext('verifySelection = async () => {globalThis.verified = true;}', context);
(async () => {
  assert.equal(await context.ensureSelectionVerified('coaching', {model_ref: {provider_id: 'p', model: 'm'}, reasoning_strength: 'auto'}), true);
  assert.equal(context.verified, true);
})().catch(error => {console.error(error); process.exitCode = 1;});
