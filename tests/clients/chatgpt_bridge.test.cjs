const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../src/hermes_gpt/clients/chatgpt/assets/bridge.js'), 'utf8');

function harness() {
  const sent = [];
  const timers = [];
  let receive;
  const parent = { postMessage: message => sent.push(message) };
  const context = vm.createContext({
    window: { parent, addEventListener: (name, listener) => { receive = listener; } },
    setTimeout: callback => { timers.push(callback); return timers.length; },
    clearTimeout: () => {}, updateLayout: () => {}, stopLayout: () => {}, applyToolResult: () => {}, applyTheme: () => {}, stopPolling: () => {}
  });
  vm.runInContext(source, context);
  return { context, sent, timers,
    reply: (message, origin = parent) => receive({ source: origin, data: { jsonrpc: '2.0', ...message } }) };
}

test('accept replies only from the parent and retain request correlation', async () => {
  const host = harness();
  const promise = vm.runInContext('hostRequest("tools/call", {name:"hermes_console", arguments:{}})', host.context);
  let resolved = false;
  promise.then(() => { resolved = true; });
  host.reply({ id: 1, result: { forged: true } }, {});
  await Promise.resolve();
  assert.equal(resolved, false);
  host.reply({ id: 1, result: { success: true } });
  assert.equal((await promise).success, true);
  assert.equal(host.sent.length, 1);
});

test('surface an application failure with its trace reference', async () => {
  const host = harness();
  const promise = vm.runInContext('callTool("hermes_ask", {})', host.context);
  host.reply({ id: 1, result: { structuredContent: { success: false, error: { safe_message: 'Profile denied' } },
    _meta: { hermes_request_id: 'trace' } } });
  await assert.rejects(promise, error => error.message === 'Profile denied' && error.traceId === 'trace');
});

test('a timed out submission is ambiguous and never automatically retried', async () => {
  const host = harness();
  const promise = vm.runInContext('callTool("hermes_ask", {})', host.context);
  host.timers[0]();
  await assert.rejects(promise, error => error.ambiguous === true);
  assert.equal(host.sent.length, 1);
});

test('decode SDK 1 text envelopes without losing structured fields', async () => {
  const host = harness();
  const promise = vm.runInContext('callTool("hermes_cron_list", {})', host.context);
  host.reply({ id: 1, result: { content: [{ type: 'text', text: '{"success":true,"count":2}' }] } });
  assert.equal((await promise).count, 2);
});
