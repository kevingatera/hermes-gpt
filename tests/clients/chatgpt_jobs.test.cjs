const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../src/hermes_gpt/clients/chatgpt/assets/jobs.js'), 'utf8');
test('following a job selects its profile and keeps the correct conversation', async () => {
  const elements = new Map();
  const el = id => {
    if (!elements.has(id)) elements.set(id, { options: [], value: '', add(option) { this.options.push(option); }, addEventListener() {} });
    return elements.get(id);
  };
  el('profile').value = 'chatgpt';
  el('conversation').options = [{ value: 'old-chatgpt-id' }];
  let work;
  const context = vm.createContext({ el, profiles: [{ profile: 'chatgpt' }, { profile: 'default' }],
    uncertainSubmission: false, window: { addEventListener() {} },
    clearTimeout() {}, setTimeout() {}, Option: class { constructor(label, value) { this.label = label; this.value = value; } },
    loadConversations: async () => { el('conversation').options = [{ value: '' }]; },
    perform: action => { work = action(); return work; },
    callTool: async name => name === 'hermes_session_job_status' ?
      { job: { status: 'completed', profile: 'default', session_id: 'correct-default-id' } } :
      { response: 'Observed answer' }
  });
  vm.runInContext(source, context);
  vm.runInContext('followJob({job_id:"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"})', context);
  await work;
  assert.equal(el('profile').value, 'default');
  assert.equal(el('conversation').value, 'correct-default-id');
  assert.equal(el('conversation').options.some(option => option.value === 'old-chatgpt-id'), false);
  assert.equal(el('answer').textContent, 'Observed answer');
  assert.equal(el('controls').disabled, false);
});
