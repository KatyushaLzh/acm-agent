const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const base = path.join(__dirname, '../tools/acm_agent/web_static');
function extract(file, start, end) {
  const src = fs.readFileSync(path.join(base, file), 'utf8');
  const first = src.indexOf(start);
  const last = src.indexOf(end, first);
  assert(first >= 0 && last > first, `missing source boundary: ${start}`);
  return src.slice(first, last);
}
function element() {
  return {value:'', textContent:'', innerHTML:'', disabled:false, open:true, checked:false,
    dataset:{}, listeners:{}, classList:{add(){},remove(){},toggle(){}},
    addEventListener(name, callback) { this.listeners[name] = callback; }};
}
function setup() {
  const nodes = new Map(), requests = [], timers = new Map(), imports = [];
  const state = {knowledgeEpoch:1, knowledgeEditEpoch:0, knowledgeRefreshRequest:null,
    knowledgeProposalId:'old', knowledgeProposalRevision:1, knowledgeProposalDirty:false,
    aiPlanDraft:{title:'A'}, aiPlanValidationEpoch:0, aiPlanValidatedContent:null};
  const ctx = {state, AbortController, JSON, Number, String, Boolean,
    $: key => {if (!nodes.has(key)) nodes.set(key,element()); return nodes.get(key);},
    api: (url, options) => new Promise((resolve,reject) => requests.push({url,options,resolve,reject})),
    setBusy(button,busy) { button.disabled = busy; }, toast(){}, escapeHtml:String,
    window:{clearTimeout(id){timers.delete(id);},setTimeout(callback){const id={};timers.set(id,callback);return id;}},
    knowledgeMarkdown:p=>p.markdown||'', knowledgeWarningItems:()=>[], renderSafeKnowledgeMarkdown(){},
    generatedDraftRequirement:()=>null, importErrors:p=>p?.errors||[],
    previewDuplicate:()=>false, submitPlanImport:async value=>imports.push(value),
    renderAiPlanFeedback(){ctx.syncAiPlanImportAvailability();},
  };
  vm.createContext(ctx);
  vm.runInContext(extract('view_ai.js','function knowledgeProposalPayload','async function waitForKnowledgeJob')
    + extract('view_ai.js','async function refreshKnowledgeProposal','async function applyKnowledgeProposal')
    + extract('view_ai.js','function cancelKnowledgeProposal','async function requestAiRecommendations')
    + extract('events_ai.js','  $("#knowledge-markdown-editor").addEventListener','  $("#knowledge-refresh").addEventListener')
    + extract('view_plan_ai_import.js','function syncAiPlanImportAvailability','function normalizeTaskProblem')
    + extract('view_plan_ai_import.js','async function importAiPlanDraft','function bindAiPlanImportEvents'),ctx);
  return {ctx,state,requests,timers,imports,node:ctx.$};
}
const proposal = (id='old',revision=2) => ({proposal_id:id, revision, status:'preview', markdown:`${id}-${revision}`});
const passed=[];
async function test(name,body){await body();passed.push(name);}
(async()=>{
  await test('summary cancel then old reply cannot restore proposal',async()=>{
    const h=setup(), pending=h.ctx.refreshKnowledgeProposal(h.node('#knowledge-refresh'));
    h.ctx.cancelKnowledgeProposal();
    h.requests[0].resolve(proposal());await pending;
    assert.equal(h.state.knowledgeProposalId,'');
  });
  await test('summary replacement in same epoch ignores previous proposal reply',async()=>{
    const h=setup(), pending=h.ctx.refreshKnowledgeProposal(h.node('#knowledge-refresh'));
    h.ctx.renderKnowledgeProposal(proposal('new',1));
    h.requests[0].resolve(proposal());await pending;
    assert.equal(h.state.knowledgeProposalId,'new');
    assert.equal(h.node('#knowledge-markdown-editor').value,'new-1');
  });
  await test('summary newer edits survive refresh and next refresh uses returned revision',async()=>{
    const h=setup();h.node('#knowledge-markdown-editor').value='sent';
    const pending=h.ctx.refreshKnowledgeProposal(h.node('#knowledge-refresh'));
    h.node('#knowledge-markdown-editor').value='new local edit';
    h.node('#knowledge-markdown-editor').listeners.input();
    h.requests[0].resolve(proposal());await pending;
    assert.equal(h.node('#knowledge-markdown-editor').value,'new local edit');
    assert.equal(h.state.knowledgeProposalRevision,2);
    assert.equal(h.state.knowledgeProposalDirty,true);
    assert.equal(h.node('#knowledge-apply').disabled,true);
    const next=h.ctx.refreshKnowledgeProposal(h.node('#knowledge-refresh'));
    assert.equal(h.requests[1].options.body.expected_revision,2);
    assert.equal(h.requests[1].options.body.entry_markdown,'new local edit');
    h.requests[1].resolve(proposal('old',3));await next;
    assert.equal(h.state.knowledgeProposalDirty,false);
    assert.equal(h.node('#knowledge-apply').disabled,false);
  });
  await test('summary edits reverted to original text still invalidate old response',async()=>{
    const h=setup();h.node('#knowledge-markdown-editor').value='sent';
    const pending=h.ctx.refreshKnowledgeProposal(h.node('#knowledge-refresh'));
    h.node('#knowledge-markdown-editor').listeners.input();
    h.requests[0].resolve(proposal());await pending;
    assert.equal(h.node('#knowledge-markdown-editor').value,'sent');
    assert.equal(h.state.knowledgeProposalDirty,true);
  });
  await test('summary stale rejection does not surface error in new proposal',async()=>{
    const h=setup(), pending=h.ctx.refreshKnowledgeProposal(h.node('#knowledge-refresh'));
    h.ctx.cancelKnowledgeProposal();h.ctx.renderKnowledgeProposal(proposal('new',1));
    h.requests[0].reject(new Error('old failure'));await pending;
    assert.equal(h.state.knowledgeProposalId,'new');
  });
  await test('plan edits immediately invalidate response before debounce fires',async()=>{
    const h=setup(), pending=h.ctx.validateAiPlanDraft();
    h.state.aiPlanDraft.title='B';h.ctx.markAiPlanDraftDirty();
    assert.equal(h.requests[0].options.signal.aborted,true);
    assert.equal(h.timers.size,1);
    h.requests[0].resolve({plan:{title:'A'},errors:[]});await pending;
    assert.equal(h.state.aiPlanPreview,null);
    assert.equal(h.node('#ai-plan-import-confirm').disabled,true);
    const next=h.ctx.validateAiPlanDraft();
    h.requests[1].resolve({plan:{title:'B'},errors:[]});await next;
    assert.equal(h.state.aiPlanPreview.plan.title,'B');
    assert.equal(h.node('#ai-plan-import-confirm').disabled,false);
  });
  await test('plan content identity rejects unannounced draft replacement',async()=>{
    const h=setup(), pending=h.ctx.validateAiPlanDraft();
    h.state.aiPlanDraft={title:'B'};
    h.requests[0].resolve({plan:{title:'A'},errors:[]});await pending;
    assert.equal(h.state.aiPlanPreview,undefined);
  });
  await test('plan import requires exactly the validated content',async()=>{
    const h=setup(), pending=h.ctx.validateAiPlanDraft();
    h.requests[0].resolve({plan:{title:'A'},errors:[]});await pending;
    h.state.aiPlanDraft.title='B';
    await h.ctx.importAiPlanDraft(element());
    assert.equal(h.imports.length,0);
    assert.equal(h.state.aiPlanPreview,null);
  });
  await test('close binds active attempt and retries report failure against same attempt',async()=>{
    const h=setup();
    Object.assign(h.ctx,{renderActive:session=>{h.state.activeSession=session;},switchAiProblem(){},
      loadBootstrap:async()=>{},currentAiProblem:()=>'',requestRecommendations:async()=>{}});
    vm.runInContext(extract('events_workbench.js','  $("#close-form").addEventListener', '\n}\n\nexport'),h.ctx);
    h.state.activeSession={attempt_id:42,problem_id:'CF1A'};
    const form=h.node('#close-form');
    form.elements=Object.fromEntries(Object.entries({problem:'CF1A',result:'AC',minutes:'10',hint_level:'0',failure:'',notes:''})
      .map(([name,value])=>[name,{value}]));
    form.elements.knowledge_enabled={checked:false};
    const event={preventDefault(){},currentTarget:form};
    const closing=form.listeners.submit(event);
    assert.equal(h.requests[0].options.body.attempt_id,42);
    h.requests[0].resolve({ok:true,attempt_id:42,committed:true,report_status:'failed',warnings:[{message:'report retry needed'}]});
    await closing;
    assert(h.node('#close-result').innerHTML.includes('report retry needed'));
    const retry=form.listeners.submit(event);
    assert.equal(h.requests[1].options.body.attempt_id,42);
    h.requests[1].resolve({ok:true,attempt_id:42,committed:true,replayed:true,report_status:'written'});
    await retry;
    assert.equal(h.state.closeAttempt.attemptId,42);
  });
  console.log(JSON.stringify({passed:passed.length,cases:passed},null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
