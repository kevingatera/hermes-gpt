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

test('long work keeps following past five minutes and retries a failed observation', async () => {
  const elements = new Map();
  const el = id => {
    if (!elements.has(id)) elements.set(id, { value: '', addEventListener() {} });
    return elements.get(id);
  };
  let delay, fail = false;
  const context = vm.createContext({ el, profiles: [], uncertainSubmission: false,
    window: { addEventListener() {} }, clearTimeout() {},
    setTimeout(_callback, milliseconds) { delay = milliseconds; return 1; },
    perform: action => action(), callTool: async () => {
      if (fail) throw new Error('temporary connection failure');
      return { job: { status: 'running', started_at: new Date(Date.now() - 600000).toISOString() } };
    }
  });
  vm.runInContext(source, context);
  vm.runInContext('currentJob = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"; pollCount = 150;', context);
  await vm.runInContext('checkJob()', context);
  assert.equal(delay, 10000);
  assert.match(el('job-status').textContent, /elapsed/);
  assert.doesNotMatch(el('job-status').textContent, /paused/);
  fail = true;
  await assert.rejects(vm.runInContext('checkJob()', context), /temporary/);
  assert.equal(delay, 10000);
  assert.equal(vm.runInContext('currentJob', context), 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
});

for (const [status, expected] of [['cancelled', /cancelled/], ['timed_out', /time limit/], ['failed', /could not finish/]]) {
  test(`empty ${status} result explains what happened`, async () => {
    const elements = new Map();
    const el = id => {
      if (!elements.has(id)) elements.set(id, {value: '', options: [], addEventListener() {}});
      return elements.get(id);
    };
    const context = vm.createContext({el, profiles: [], window: {addEventListener() {}},
      clearTimeout() {}, setTimeout() {}, perform: action => action(),
      callTool: async name => name === 'hermes_session_job_status' ? {job: {status}} : {response: ''}
    });
    vm.runInContext(source, context);
    vm.runInContext('currentJob = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"', context);
    await vm.runInContext('checkJob()', context);
    assert.match(el('answer').textContent, expected);
    assert.equal(el('cancel-job').disabled, true);
    assert.equal(vm.runInContext('currentJob', context), null);
  });
}

for (const uncertain of [false, true]) {
test(`missing reference preserves submission uncertainty: ${uncertain}`, async () => {
  const elements = new Map();
  const el = id => {
    if (!elements.has(id)) elements.set(id, {value: '', addEventListener() {}});
    return elements.get(id);
  };
  let scheduled = false;
  const context = vm.createContext({el, profiles: [{}], uncertainSubmission: uncertain, window: {addEventListener() {}},
    clearTimeout() {}, setTimeout() {scheduled = true;}, perform: action => action(),
    callTool: async () => {throw Object.assign(new Error('missing'), {code: 'JOB_NOT_FOUND'});}
  });
  vm.runInContext(source, context);
  vm.runInContext('currentJob = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"', context);
  await assert.rejects(vm.runInContext('checkJob()', context), /missing/);
  assert.equal(vm.runInContext('currentJob', context), null);
  assert.equal(el('controls').disabled, uncertain);
  assert.equal(scheduled, false);
});

}

test('diagnostics offers a follow action and renders result text literally', () => {
  const consoleSource = fs.readFileSync(path.join(__dirname, '../../src/hermes_gpt/clients/chatgpt/assets/console.js'), 'utf8');
  const rowsFunction = consoleSource.slice(consoleSource.indexOf('function listRows('), consoleSource.indexOf('async function perform('));
  const makeElement = () => ({children: [], appendChild(child) {this.children.push(child);},
    replaceChildren() {this.children = [];}, addEventListener(_name, callback) {this.click = callback;}});
  const target = makeElement();
  let followed;
  const row = {job_id: 'a'.repeat(32), text: '<script>not executable</script>'};
  const context = vm.createContext({document: {createElement: makeElement}, target, row,
    follow: value => {followed = value.job_id;}});
  vm.runInContext(rowsFunction, context);
  vm.runInContext('listRows(target, [row], item => item.text, follow)', context);
  assert.equal(target.children[0].textContent, row.text);
  assert.equal(target.children[0].children[0].textContent, 'Follow result');
  target.children[0].children[0].click();
  assert.equal(followed, row.job_id);
});

test('completed work reports its actual duration rather than time since submission', async () => {
  const elements = new Map();
  const el = id => {
    if (!elements.has(id)) elements.set(id, {value: '', options: [], addEventListener() {}});
    return elements.get(id);
  };
  const context = vm.createContext({el, profiles: [], uncertainSubmission: false,
    window: {addEventListener() {}}, clearTimeout() {}, setTimeout() {},
    callTool: async name => name === 'hermes_session_job_status' ? {job: {
      status: 'completed', started_at: '2026-01-01T00:00:00Z', ended_at: '2026-01-01T00:01:15Z'
    }} : {response: 'done'}
  });
  vm.runInContext(source, context);
  vm.runInContext('currentJob = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"', context);
  await vm.runInContext('checkJob()', context);
  assert.match(el('job-status').textContent, /1m 15s elapsed/);
});
