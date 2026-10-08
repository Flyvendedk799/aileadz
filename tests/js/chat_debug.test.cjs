const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const code = fs.readFileSync(path.resolve(__dirname, '../../static/futurematch/assets/chat-debug.js'), 'utf8');

function setup({ clipboard, legacy = false } = {}) {
  const copied = [], prompts = [], elements = [];
  const buttons = ['employee', 'hr', 'vendor', 'widget'].map(scope => ({
    dataset: { chatScope: scope }, disabled: true, label: { textContent: 'Kopiér chat-id' },
    querySelector() { return this.label; }, focus() {},
  }));
  const context = {
    window: { prompt: (message, value) => prompts.push(value) },
    navigator: { clipboard: { writeText: async value => {
      if (clipboard === false) throw new Error('denied');
      copied.push(value);
    } } },
    document: {
      querySelectorAll: () => buttons,
      addEventListener() {},
      createElement: () => { const el = { style: {}, select() {}, remove() {} }; elements.push(el); return el; },
      body: { appendChild() {} }, execCommand: () => legacy,
    },
    setTimeout: () => 1, clearTimeout() {},
  };
  vm.createContext(context); vm.runInContext(code, context);
  return { api: context.window.FMChatDebug, buttons, copied, prompts, elements };
}

test('copy uses the full server ID and isolates assistant surfaces', async () => {
  const h = setup();
  h.api.setId('e186708d-c619-4e8b-800f-b42fe695bd18', 'employee');
  h.api.setId('hr_566221', 'hr');
  await h.api.copyId(h.buttons[0]);
  await h.api.copyId(h.buttons[1]);
  assert.deepEqual(h.copied, ['e186708d-c619-4e8b-800f-b42fe695bd18', 'hr_566221']);
  assert.equal(h.buttons[2].disabled, true);
  assert.equal(h.buttons[0].label.textContent, 'Kopieret');
});

test('new and restored chats replace the ID; missing or invalid IDs cannot be copied', async () => {
  const h = setup();
  h.api.setId('first', 'employee');
  h.api.setId(null, 'employee');
  await h.api.copyId(h.buttons[0]);
  assert.equal(h.copied.length, 0);
  assert.equal(h.buttons[0].disabled, true);
  h.api.setId('restored-chat', 'employee');
  await h.api.copyId(h.buttons[0]);
  assert.deepEqual(h.copied, ['restored-chat']);
  h.api.setId('<script>', 'employee');
  assert.equal(h.buttons[0].disabled, true);
});

test('clipboard denial offers the actual ID for manual copying', async () => {
  const h = setup({ clipboard: false });
  h.api.setId('chat-123', 'employee');
  await h.api.copyId(h.buttons[0]);
  assert.equal(h.elements[0].value, 'chat-123');
  assert.deepEqual(h.prompts, ['chat-123']);
  assert.equal(h.buttons[0].label.textContent, 'Kopiér chat-id');
});

test('legacy clipboard success does not open a manual prompt', async () => {
  const h = setup({ clipboard: false, legacy: true });
  h.api.setId('chat-123', 'employee');
  await h.api.copyId(h.buttons[0]);
  assert.equal(h.prompts.length, 0);
  assert.equal(h.buttons[0].label.textContent, 'Kopieret');
});
