const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

// Exercise the real renderer with synthetic snapshots only. These assertions
// do not model native Keychain dialogs or establish their prompt count.
const html = fs.readFileSync(path.join(__dirname, '../renderer/index.html'), 'utf8');
const source = fs.readFileSync(path.join(__dirname, '../renderer/app.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};

class Element {
  constructor(tag = 'div') {
    Object.assign(this, {
      tagName: tag, children: [], listeners: {}, value: '', disabled: false,
      hidden: false, textContent: '', className: '', attributes: {},
    });
    this.classList = { toggle: (name, value) => {
      const names = new Set(this.className.split(' ').filter(Boolean));
      if (value) names.add(name); else names.delete(name);
      this.className = [...names].join(' ');
    } };
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  setAttribute(key, value) { this.attributes[key] = value; }
  fire(type) { return this.listeners[type]?.({ target: this }); }
}

function snapshot(status, extra = {}) {
  return {
    session: { status, sharing: status === 'connected' }, models: [], busy: null,
    library: {
      classes: [{ id: 'class-a', name: 'Synthetic class' }], selectedClassId: 'class-a',
      documents: [{ id: 'doc-a', filename: 'Synthetic notes.pdf', page_count: 1 }],
      selectedDocumentId: null,
      search: { state: 'missing', mode: 'lexical', message: 'Keyword search is active.' },
    },
    ...extra,
  };
}

function harness(actions = {}) {
  const nodes = Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)]
    .map(match => [match[1], new Element()]));
  const initial = deferred();
  const calls = [];
  let emit;
  let current = snapshot('restoring');
  const gct = {
    onState(fn) { emit = fn; return () => {}; },
    getState() { return initial.promise; },
  };
  for (const name of ['signIn', 'retryConnection', 'cancelConnection', 'chooseFile', 'backupLibrary']) {
    gct[name] = (...args) => {
      calls.push({ name, args });
      return actions[name] ? actions[name](...args) : Promise.resolve(current);
    };
  }
  vm.runInNewContext(source, {
    document: { getElementById: id => nodes[id], createElement: tag => new Element(tag) },
    window: { gct, addEventListener() {} }, console, Promise, Set, Map, JSON,
  });
  return {
    nodes, calls, initial,
    update(value) { current = structuredClone(value); emit(current); },
  };
}

test('initial restoration withholds browser sign-in before the first state snapshot', async () => {
  const signInTag = html.match(/<button\b[^>]*id="sign-in"[^>]*>/)?.[0];
  assert.match(signInTag, /\bdisabled\b/);
  assert.match(signInTag, /\bhidden\b/);
  const h = harness();
  await flush();
  assert.equal(h.nodes['sign-in'].hidden, true);
  assert.equal(h.nodes['sign-in'].disabled, true);
  assert.equal(h.nodes['retry-connection'].hidden, true);
  assert.equal(h.nodes.ask.disabled, true);
  assert.match(h.nodes['account-status-text'].textContent, /Restoring/);
  assert.deepEqual(h.calls, []);
});

test('blocked credential access offers local retry and preserves usable library controls', async () => {
  const h = harness();
  h.update(snapshot('storage_unavailable'));
  assert.equal(h.nodes['sign-in'].hidden, true);
  assert.equal(h.nodes['sign-in'].disabled, true);
  assert.equal(h.nodes['retry-connection'].hidden, false);
  assert.equal(h.nodes['retry-connection'].disabled, false);
  assert.equal(h.nodes['retry-connection'].textContent, 'Retry saved connection');
  assert.match(h.nodes['account-detail'].textContent, /preserved/);
  for (const id of ['class-picker', 'choose-file', 'backup-library', 'prepare-search']) {
    assert.equal(h.nodes[id].disabled, false, `${id} remains available`);
  }
  h.nodes['backup-library'].fire('click');
  await flush();
  assert.deepEqual(h.calls, [{ name: 'backupLibrary', args: [] }]);
  assert.equal(h.nodes.ask.disabled, true);
});

test('a saved account with no model catalog retries without offering browser reconnection', async () => {
  const h = harness();
  h.update(snapshot('connected'));
  assert.equal(h.nodes['sign-in'].hidden, true);
  assert.equal(h.nodes['retry-connection'].hidden, false);
  assert.equal(h.nodes['retry-connection'].textContent, 'Retry model connection');
  assert.match(h.nodes['ask-help'].textContent, /Retry model connection/);
  assert.doesNotMatch(h.nodes['account-detail'].textContent, /Reconnect/);
  h.nodes['retry-connection'].fire('click');
  await flush();
  assert.deepEqual(h.calls, [{ name: 'retryConnection', args: [] }]);
  h.update(snapshot('connected', { models: [{ slug: 'synthetic-model', display_name: 'Synthetic model' }] }));
  h.nodes.question.value = 'A synthetic question';
  h.nodes.question.fire('input');
  assert.equal(h.nodes['retry-connection'].hidden, true);
  assert.equal(h.nodes.ask.disabled, false);
  h.initial.resolve(snapshot('disconnected'));
  await flush();
  assert.equal(h.nodes['account-status-text'].textContent, 'Connected');
  assert.equal(h.nodes.ask.disabled, false, 'late initial snapshots cannot undo recovery');
});

test('retry and cancellation use no-argument actions and stay busy until restoration settles', async () => {
  const retry = deferred();
  const h = harness({
    retryConnection: () => retry.promise,
    cancelConnection: () => Promise.resolve(snapshot('storage_unavailable', { busy: 'restore' })),
  });
  h.update(snapshot('storage_unavailable'));
  h.nodes['retry-connection'].fire('click');
  assert.equal(h.nodes['cancel-connection'].hidden, false);
  assert.equal(h.nodes['retry-connection'].hidden, true);
  assert.equal(h.nodes['sign-in'].hidden, true);
  assert.equal(h.nodes['choose-file'].disabled, true);
  h.update(snapshot('storage_unavailable', { busy: 'restore' }));
  h.nodes['cancel-connection'].fire('click');
  assert.equal(h.nodes['cancel-connection'].disabled, true);
  await flush();
  assert.equal(h.nodes['choose-file'].disabled, true, 'an unabortable read still owns the operation');
  assert.deepEqual(h.calls, [
    { name: 'retryConnection', args: [] }, { name: 'cancelConnection', args: [] },
  ]);
  h.update(snapshot('storage_unavailable'));
  retry.resolve(snapshot('connected'));
  await flush();
  assert.equal(h.nodes['account-status-text'].textContent, 'Saved connection unavailable');
  assert.equal(h.nodes['cancel-connection'].hidden, true);
  assert.equal(h.nodes['retry-connection'].hidden, false);
  assert.equal(h.nodes['choose-file'].disabled, false);
});

test('actual revocation requires an explicit reconnect action rather than a storage retry', async () => {
  const h = harness();
  h.update(snapshot('reauth_required'));
  assert.equal(h.nodes['sign-in'].hidden, false);
  assert.equal(h.nodes['sign-in'].disabled, false);
  assert.equal(h.nodes['sign-in'].textContent, 'Reconnect with ChatGPT');
  assert.equal(h.nodes['retry-connection'].hidden, true);
  assert.deepEqual(h.calls, []);
  h.nodes['sign-in'].fire('click');
  await flush();
  assert.deepEqual(h.calls, [{ name: 'signIn', args: [] }]);
});
