const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const shared = fs.readFileSync(path.join(root, 'static/futurematch/assets/ai-stream.js'), 'utf8');
const employee = fs.readFileSync(path.join(root, 'static/futurematch/assets/chat.js'), 'utf8');
const streamFunction = employee.slice(employee.indexOf('  async function streamFromBackend('), employee.indexOf('  /* ---------------- send / run'));

// Execute the shipped transport code, with an in-memory response and a minimal
// rendering boundary. No provider, database, network or browser packages needed.
function harness(kind, chunks, response = {}) {
  let fetches = 0, cancelled = false, released = false;
  const events = [], elements = [], timers = new Map();
  const context = {
    window: {}, TextDecoder, AbortController, Error, console,
    setTimeout(fn, ms) { const id = {}; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    fetch: async (_url, options) => {
      fetches++;
      const bytes = chunks.map(c => typeof c === 'string' ? new TextEncoder().encode(c) : c);
      const reader = {
        async read() {
          if (options.signal.aborted) throw Object.assign(new Error('aborted'), { name: 'AbortError' });
          if (bytes.length) return { value: bytes.shift(), done: false };
          if (response.stall) {
            return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })), { once: true }));
          }
          return { done: true };
        },
        cancel() { cancelled = true; return Promise.resolve(); },
        releaseLock() { released = true; },
      };
      return {
        ok: response.status == null || response.status < 400,
        status: response.status || 200,
        redirected: response.redirected || false,
        headers: { get: () => response.contentType || 'text/event-stream' },
        json: async () => response.json || {},
        body: { getReader: () => reader },
      };
    },
    document: { createElement: () => { const el = { innerHTML: '', className: '' }; elements.push(el); return el; } },
    ASK_URL: '/ask', WATCHDOG_MS: 25000, WATCHDOG_FIRST_MS: 90000,
    currentAbort: null, aborted: false, CARDS_MARK: '<cards>',
    md: t => t, balanceMarkdown: t => t,
    requestAnimationFrame: fn => fn(),
    down() {}, place() {}, settleActivity() {}, clearThinking() {},
    thinkStatus() {}, addChips(_body, items) { events.push({ type: 'suggestions', items }); },
  };
  vm.createContext(context);
  vm.runInContext(kind === 'shared' ? shared : streamFunction, context);
  async function run(opts) {
    if (kind === 'shared') {
      await context.window.FMAI.ask('/ask', { query: 'Hej' }, {
        text: d => events.push({ type: 'text', content: d.content }),
        error: d => events.push(d), done: d => events.push(d),
      }, opts);
    } else {
      try {
        const result = await context.streamFromBackend({}, 'Hej');
        events.push({ type: 'done', aborted: context.aborted, ...result });
      } catch (e) { events.push({ type: 'error', content: e.userMessage, error: e }); }
    }
    return events;
  }
  return { run, events, elements, timers, context,
    get fetches() { return fetches; }, get cancelled() { return cancelled; }, get released() { return released; } };
}
const chunk = text => 'data: ' + JSON.stringify({ type: 'chunk', content: text }) + '\n\n';
const done = 'data: [DONE]\n\n';
const errors = h => h.events.filter(e => e.type === 'error');
const answer = (h, kind) => kind === 'shared'
  ? h.events.filter(e => e.type === 'text').map(e => e.content).join('')
  : h.elements.map(e => e.innerHTML).join('');

for (const kind of ['shared', 'employee']) {
  test(`${kind}: preserves Danish UTF-8 across one-byte chunks and ignores late events`, async () => {
    const bytes = new TextEncoder().encode(chunk('Blåbær og læring') + done + chunk('late'));
    const h = harness(kind, Array.from(bytes, b => Uint8Array.of(b)));
    await h.run();
    assert.equal(answer(h, kind), 'Blåbær og læring');
    assert.equal(errors(h).length, 0);
    assert.equal(h.cancelled, true);
    assert.equal(h.released, true);
    assert.equal(h.timers.size, 0);
  });
  test(`${kind}: CRLF, multiline JSON, data without space, and terminal event at EOF`, async () => {
    const input = ': heartbeat\r\n\r\ndata:{"type":"chunk",\r\ndata:"content":"Hej"}\r\n\r\ndata:{"type":"done"}';
    const h = harness(kind, Array.from(input));
    await h.run();
    assert.equal(answer(h, kind), 'Hej');
    assert.equal(errors(h).length, 0);
  });
  test(`${kind}: truncated answer stays visible and fails explicitly`, async () => {
    const h = harness(kind, [chunk('Delvist svar')]);
    await h.run();
    assert.equal(answer(h, kind), 'Delvist svar');
    assert.equal(errors(h).length, 1);
    assert.equal(h.fetches, 1);
    assert.equal(h.timers.size, 0);
  });
  test(`${kind}: empty EOF is a failure`, async () => {
    const h = harness(kind, []);
    await h.run();
    assert.equal(errors(h).length, 1);
  });
  test(`${kind}: malformed event is not silently dropped`, async () => {
    const h = harness(kind, [chunk('Bevar dette'), 'data: {broken}\n\n', done]);
    await h.run();
    assert.equal(answer(h, kind), 'Bevar dette');
    assert.equal(errors(h).length, 1);
  });
  test(`${kind}: server error is terminal and preserves the prior answer`, async () => {
    const h = harness(kind, [chunk('Bevar dette') + 'data: {"type":"error","content":"AI er midlertidigt utilgængelig."}\n\n' + done + chunk('late')]);
    await h.run();
    assert.equal(answer(h, kind), 'Bevar dette');
    assert.equal(errors(h)[0].content, 'AI er midlertidigt utilgængelig.');
    assert.equal(errors(h).length, 1);
    if (kind === 'shared') assert.deepEqual(h.events.filter(e => e.type === 'done').map(e => e.failed), [true]);
  });
  for (const [status, expected] of [[401, 'Log ind igen'], [429, 'Vent et øjeblik']]) {
    test(`${kind}: HTTP ${status} gives actionable guidance`, async () => {
      const h = harness(kind, [], { status, contentType: 'application/json' });
      await h.run();
      assert.ok(errors(h)[0].content.includes(expected));
      assert.equal(h.fetches, 1);
    });
  }
  test(`${kind}: JSON credit guard is shown even with HTTP 200`, async () => {
    const h = harness(kind, [], { contentType: 'application/json', json: { answers: [{ content: 'AI er sat på pause.' }] } });
    await h.run();
    assert.equal(errors(h)[0].content, 'AI er sat på pause.');
  });
  for (const timerMs of [90000, 180000]) {
    test(`${kind}: ${timerMs === 90000 ? 'idle' : 'total'} deadline stops a stalled stream`, async () => {
      const h = harness(kind, [], { stall: true });
      const pending = h.run();
      await Promise.resolve(); await Promise.resolve();
      const timer = [...h.timers.values()].find(t => t.ms === timerMs);
      assert.ok(timer);
      timer.fn();
      await pending;
      assert.ok(errors(h)[0].content.includes('tog for lang tid'));
      assert.equal(h.timers.size, 0);
    });
  }
  test(`${kind}: user stop is distinct from a network failure`, async () => {
    const h = harness(kind, [], { stall: true });
    const controller = new AbortController();
    const pending = h.run({ signal: controller.signal });
    await Promise.resolve(); await Promise.resolve();
    if (kind === 'shared') controller.abort();
    else { h.context.aborted = true; h.context.currentAbort.abort(); }
    await pending;
    assert.equal(errors(h).length, 0);
    assert.equal(h.events.filter(e => e.type === 'done')[0].aborted, true);
    assert.equal(h.timers.size, 0);
  });
}

test('employee: a failed POST is never automatically replayed', async () => {
  let calls = 0, errorRows = 0, finished = false;
  const context = {
    sending: false, aborted: false, attached: [], lastActualQuery: '',
    document: { querySelector: () => null }, input: { value: 'draft' },
    addUser() {}, takeHandoff: () => null, resize() {}, toggleSend() {}, setSending() {},
    addBot: () => ({}), thinking: () => ({ remove() {} }),
    streamFromBackend: async () => { calls++; throw new Error('network'); },
    settleToolChips() {}, appendError() { errorRows++; }, finish() { finished = true; },
  };
  vm.createContext(context);
  vm.runInContext(employee.slice(employee.indexOf('  async function run('), employee.indexOf('  function finish()')), context);
  await context.run('Gem min profil');
  assert.equal(calls, 1);
  assert.equal(errorRows, 1);
  assert.equal(finished, true);
});

test('HR panel: preserves partial answers and drafts submitted while busy', () => {
  function element() {
    return {
      dataset: {}, children: [], listeners: {}, value: '', _html: '',
      get innerHTML() { return this._html; },
      set innerHTML(value) { this._html = value; this.children = []; },
      set textContent(value) { this._html = value; this.children = []; },
      get textContent() { return this._html; },
      getAttribute() { return ''; }, setAttribute() {},
      querySelector() { return null; }, appendChild(child) { this.children.push(child); },
      addEventListener(type, fn) { this.listeners[type] = fn; },
    };
  }
  const nodes = Object.fromEntries(['fmAip', 'fmAipFab', 'fmAipPanel', 'fmAipClose', 'fmAipNew', 'fmAipForm', 'fmAipInput', 'fmAipBody'].map(id => [id, element()]));
  const requests = [];
  const kit = { md: text => text, ask: (_url, payload, handlers) => requests.push({ payload, handlers }) };
  const context = {
    window: { FMAI: kit }, FMAI: kit,
    document: { getElementById: id => nodes[id], createElement: element, addEventListener() {} },
  };
  vm.createContext(context);
  const panel = fs.readFileSync(path.join(root, 'templates/fm/_ai_panel.html'), 'utf8');
  vm.runInContext(panel.slice(panel.indexOf('<script>') + 8, panel.indexOf('</script>', panel.indexOf('<script>'))), context);
  function submit(text) {
    nodes.fmAipInput.value = text;
    nodes.fmAipForm.listeners.submit({ preventDefault() {} });
  }
  submit('Hvad er vores budget?');
  assert.equal(requests.length, 1);
  requests[0].handlers.text({ content: 'Budgettet er' });
  const answer = nodes.fmAipBody.children[1];
  submit('Og næste måned?');
  assert.equal(nodes.fmAipInput.value, 'Og næste måned?');
  assert.equal(requests.length, 1);
  requests[0].handlers.error({ content: 'Forbindelsen blev afbrudt.' });
  assert.equal(answer.innerHTML, 'Budgettet er');
  assert.equal(answer.children[0].textContent, 'Forbindelsen blev afbrudt.');
  requests[0].handlers.done({ failed: true });
  submit(nodes.fmAipInput.value);
  assert.equal(requests.length, 2);
  assert.equal(requests[1].payload.query, 'Og næste måned?');
});

test('employee: explicit resend keeps its context and preserves a new draft and attachments', async () => {
  let sent;
  const context = {
    sending: false, aborted: false, attached: ['New course'], lastActualQuery: '',
    document: { querySelector: () => null }, input: { value: 'My next question' },
    resize() {}, toggleSend() {}, setSending() {},
    addBot: () => ({}), thinking: () => ({ remove() {} }),
    streamFromBackend: async (_body, query, kind, handoff) => { sent = { query, kind, handoff }; return {}; },
    addFeedback() {}, finish() {},
  };
  vm.createContext(context);
  vm.runInContext(employee.slice(employee.indexOf('  async function run('), employee.indexOf('  function finish()')), context);
  const handoff = { from: 'profile', focus: 'skill:42' };
  await context.run('[VEDHÆFTET KURSUS: "Original"]\nHej', { skipUser: true, kind: 'seed', context: handoff });
  assert.equal(sent.query, '[VEDHÆFTET KURSUS: "Original"]\nHej');
  assert.equal(sent.kind, 'seed');
  assert.equal(sent.handoff, handoff);
  assert.equal(context.input.value, 'My next question');
  assert.deepEqual(context.attached, ['New course']);
});

for (const content of ['', chunk('   '), 'data: {"type":"suggestions","items":["Hej"]}\n\n']) {
  test(`shared: explicit completion without an answer is actionable (${JSON.stringify(content)})`, async () => {
    const h = harness('shared', [content + done]);
    await h.run();
    assert.ok(errors(h)[0].content.includes('ikke et svar'));
    assert.equal(h.events.filter(e => e.type === 'done')[0].failed, true);
  });
}

test('shared: a confirmation card is a valid answer without text', async () => {
  const h = harness('shared', ['data: {"type":"confirm_card","token":"local-test"}\n\n' + done]);
  const events = [];
  await h.context.window.FMAI.ask('/ask', {}, {
    confirm_card: d => events.push(d), done: d => events.push(d),
    error: () => assert.fail('A confirmation should not be called empty'),
  });
  assert.equal(events.length, 2);
  assert.equal(events[1].failed, undefined);
});

function uiElement() {
  return {
    dataset: {}, children: [], listeners: {}, attributes: {}, value: '', className: '', _html: '',
    classList: { add() {}, remove() {} },
    get innerHTML() { return this._html; },
    set innerHTML(value) { this._html = value; this.children = []; },
    set textContent(value) { this.innerHTML = value; },
    get textContent() { return this._html + this.children.map(c => c.textContent).join(''); },
    getAttribute(name) { return this.attributes[name] || ''; },
    setAttribute(name, value) { this.attributes[name] = value; },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    querySelectorAll(selector) {
      const classes = selector.split(',').map(s => s.slice(1));
      return this.children.flatMap(child => [
        ...(classes.some(c => child.className.split(' ').includes(c)) ? [child] : []),
        ...child.querySelectorAll(selector),
      ]);
    },
    appendChild(child) { child.parentElement = this; this.children.push(child); },
    remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(c => c !== this); },
    addEventListener(type, fn) { this.listeners[type] = fn; }, focus() {},
  };
}

function hrUI(kind) {
  const nodes = {}, requests = [], timers = new Map(), resets = [];
  const getNode = id => nodes[id] || (nodes[id] = uiElement());
  const context = {
    window: {}, AbortController,
    setTimeout(fn) { const id = {}; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); },
    document: { getElementById: getNode, createElement: uiElement, addEventListener() {} },
    fetch(url, opts) {
      if (!url.includes('reset')) return Promise.resolve({ ok: true, json: async () => ({ sessions: [] }) });
      return new Promise((resolve, reject) => {
        resets.push({ resolve, reject });
        opts.signal.addEventListener('abort', () => reject(new Error('timeout')), { once: true });
      });
    },
  };
  vm.createContext(context);
  vm.runInContext(shared, context);
  context.FMAI = context.window.FMAI;
  context.FMAI.md = text => text;
  context.FMAI.ask = (_url, payload, handlers) => { requests.push({ payload, handlers }); };
  const file = kind === 'panel' ? '_ai_panel.html' : 'chatbot.html';
  const source = fs.readFileSync(path.join(root, 'templates/fm', file), 'utf8');
  vm.runInContext(source.slice(source.indexOf('<script>') + 8, source.indexOf('</script>', source.indexOf('<script>'))), context);
  return {
    input: getNode(kind === 'panel' ? 'fmAipInput' : 'chatInput'),
    body: getNode(kind === 'panel' ? 'fmAipBody' : 'chatMessages'),
    requests, resets, timers,
    pick(text) {
      if (kind === 'panel') return getNode('fmAipBody').listeners.click({ target: { closest: () => ({ textContent: text }) } });
      return context.sendSuggestion({ textContent: text });
    },
    reset() { return kind === 'panel' ? getNode('fmAipNew').listeners.click() : context.resetChat(); },
  };
}

for (const kind of ['panel', 'page']) {
  test(`HR ${kind}: suggestion clicks preserve drafts, and busy clicks keep their chips`, () => {
    const h = hrUI(kind);
    h.input.value = 'Min kladde';
    h.pick('Vis budgettet');
    assert.equal(h.requests[0].payload.query, 'Vis budgettet');
    assert.equal(h.input.value, 'Min kladde');
    h.requests[0].handlers.suggestions({ items: ['Vis kurser'] });
    const chips = h.body.querySelector('.fm-chips');
    chips.children[0].listeners.click();
    assert.equal(h.requests.length, 1);
    assert.equal(h.body.querySelector('.fm-chips'), chips);
    assert.equal(h.input.value, 'Min kladde');
    h.requests[0].handlers.done({});
    chips.children[0].listeners.click();
    assert.equal(h.requests.length, 2);
    assert.equal(h.requests[1].payload.query, 'Vis kurser');
    assert.equal(h.body.querySelector('.fm-chips'), null);
    assert.equal(h.input.value, 'Min kladde');
  });
  for (const failure of ['http', 'network', 'invalid-json', 'timeout']) {
    test(`HR ${kind}: failed reset (${failure}) preserves the conversation and unlocks sending`, async () => {
      const h = hrUI(kind);
      h.pick('Mit oprindelige spørgsmål');
      h.requests[0].handlers.done({});
      h.input.value = 'Min kladde';
      const pending = h.reset();
      h.pick('Send ikke under nulstilling');
      assert.equal(h.requests.length, 1);
      if (failure === 'http') h.resets[0].resolve({ ok: false });
      else if (failure === 'network') h.resets[0].reject(new Error('network'));
      else if (failure === 'invalid-json') h.resets[0].resolve({ ok: true, json: async () => ({ success: false }) });
      else [...h.timers.values()][0]();
      await pending;
      assert.ok(h.body.textContent.includes('Mit oprindelige spørgsmål'));
      assert.ok(h.body.textContent.includes('kunne ikke starte'));
      assert.equal(h.input.value, 'Min kladde');
      assert.equal(h.timers.size, 0);
      h.pick('Et nyt spørgsmål');
      assert.equal(h.requests.length, 2);
    });
  }
  test(`HR ${kind}: successful reset preserves a draft and clears only the old conversation`, async () => {
    const h = hrUI(kind);
    h.pick('Mit oprindelige spørgsmål');
    h.requests[0].handlers.done({});
    h.input.value = 'Min kladde';
    const pending = h.reset();
    h.resets[0].resolve({ ok: true, json: async () => ({ success: true }) });
    await pending;
    assert.ok(!h.body.textContent.includes('Mit oprindelige spørgsmål'));
    assert.ok(h.body.textContent.includes('Ny samtale'));
    assert.equal(h.input.value, 'Min kladde');
    assert.equal(h.timers.size, 0);
  });
}
