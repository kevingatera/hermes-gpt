const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../src/hermes_gpt/clients/chatgpt/assets/layout.js'), 'utf8');
function harness() {
  const sent = [];
  const frames = [];
  let resized;
  let disconnected = false;
  const content = { height: 400, getBoundingClientRect() { return { height: this.height }; } };
  const document = { body: { style: {} }, documentElement: { clientWidth: 700, style: {} }, getElementById: () => content };
  const context = vm.createContext({ document,
    window: { innerHeight: 600, addEventListener: () => {} },
    ResizeObserver: class { constructor(callback) { resized = callback; } observe() {} disconnect() { disconnected = true; } },
    requestAnimationFrame: callback => { frames.push(callback); return frames.length; },
    cancelAnimationFrame: () => {}, getComputedStyle: () => ({ paddingTop: '20px', paddingBottom: '20px' }),
    hostNotify: (method, params) => sent.push({ method, params })
  });
  vm.runInContext(source, context);
  return { context, sent, content, document, flush: () => { while (frames.length) frames.shift()(); },
    resize: () => resized(), disconnected: () => disconnected };
}
test('content growth is reported once per distinct size', () => {
  const h = harness();
  vm.runInContext('updateLayout({})', h.context); h.flush();
  assert.equal(h.sent[0].method, 'ui/notifications/size-changed');
  assert.equal(h.sent[0].params.height, 440);
  h.content.height = 900; h.resize(); h.resize(); h.flush();
  assert.equal(h.sent.length, 2);
  assert.equal(h.sent[1].params.height, 940);
  h.resize(); h.flush(); assert.equal(h.sent.length, 2);
});
test('fixed and maximum-height containers remain scrollable', () => {
  const h = harness();
  vm.runInContext('updateLayout({containerDimensions:{height:600,width:700}})', h.context); h.flush();
  assert.equal(h.document.body.style.height, '100%');
  assert.equal(h.document.body.style.overflowY, 'auto');
  h.content.height = 1200; h.resize(); h.flush();
  assert.equal(h.sent.at(-1).params.height, 600);
  vm.runInContext('updateLayout({containerDimensions:{maxHeight:800}})', h.context); h.flush();
  assert.equal(h.document.body.style.height, '');
  assert.equal(h.document.body.style.maxHeight, '800px');
  assert.equal(h.sent.at(-1).params.height, 800);
  vm.runInContext('stopLayout()', h.context); assert.equal(h.disconnected(), true);
});
