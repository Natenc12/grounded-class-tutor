import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { ProofError, responseOptions, validateAsk } from '../runtime.mjs';

const connected = { status: 'connected', sharing: true, profileId: 'synthetic-profile', profileLabel: 'Synthetic saved account' };
const blocked = code => ({ status: 'storage_unavailable', sharing: false,
  error: { code, message: 'PRIVATE native failure text', retryable: true } });
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

// Run the real main-process startup and IPC code in a synthetic VM. Every OS,
// filesystem, SDK and browser surface is replaced before any code executes.
async function harness(overrides = {}) {
  let source = await readFile(new URL('../main.mjs', import.meta.url), 'utf8');
  source = source.replace(/^import [\s\S]*?;\n/gm, '').replaceAll('import.meta.url', JSON.stringify(new URL('../main.mjs', import.meta.url).href));
  source = source.replace(/startApp\(\)\.catch\([^\n]+\);/, '');
  const handlers = new Map(), listeners = new Map(), calls = [], publications = [];
  const appPaths = new Map([['appData', '/synthetic-app-data']]);
  const quit = deferred();
  let subscriber, sdkConfig;
  const frame = { url: new URL('../renderer/index.html', import.meta.url).href };
  const webContents = { mainFrame: frame,
    send: (_channel, value) => publications.push(structuredClone(value)),
    setWindowOpenHandler() {}, on() {},
    session: { setPermissionRequestHandler() {}, setPermissionCheckHandler() {} },
  };
  const fakeWindow = { webContents, isDestroyed: () => false, loadFile: async () => {}, show() {}, focus() {} };
  const sdk = {
    subscribe(callback) { subscriber = callback; callback({ status: 'disconnected', sharing: false }); },
    async getSession() {
      calls.push(['getSession']);
      const result = await (overrides.getSession?.() ?? connected);
      subscriber?.(result);
      return result;
    },
    async listModels(options) {
      calls.push(['listModels', options]);
      return overrides.listModels ? overrides.listModels(options) : [{ slug: 'synthetic-model', displayName: 'Synthetic model' }];
    },
    async signIn(options) { calls.push(['signIn', options]); return connected; },
    cancelSignIn() { calls.push(['cancelSignIn']); },
    async disconnect() { calls.push(['disconnect']); },
  };
  const context = vm.createContext({
    app: { setName: value => calls.push(['appName', value]),
      setPath: (key, value) => { appPaths.set(key, value); calls.push(['path', key, value]); },
      getPath: key => appPaths.get(key), requestSingleInstanceLock: () => true,
      whenReady: async () => {}, on: (name, callback) => listeners.set(name, callback),
      quit: () => { calls.push(['quit']); quit.resolve(); } },
    BrowserWindow: class { constructor() { return fakeWindow; } },
    ipcMain: { handle: (name, action) => handlers.set(name, action) },
    dialog: {}, shell: { openExternal() { assert.fail('No browser may open.'); } },
    safeStorage: { isEncryptionAvailable() { assert.fail('Do not touch the native Keychain.'); } },
    createChatGPT: config => { sdkConfig = config; return sdk; },
    CHATGPT_USAGE_URL: 'https://chatgpt.com/settings/usage',
    createSourceStore: async () => ({ close: async () => { calls.push(['sourceClose']); } }),
    mkdir: async () => {}, dirname, join, fileURLToPath, pathToFileURL,
    ProofError, responseOptions, validateAsk,
    runLibrary: async (_root, _path, request) => {
      calls.push(['library', request.operation]);
      if (request.operation === 'list_classes') return [];
      assert.fail('Unexpected synthetic library operation.');
    },
    runBridge: () => assert.fail('No Python worker is needed.'), stopBridges: async () => {},
    process: { platform: process.platform, exit() { assert.fail('Unexpected process exit.'); } },
    structuredClone, AbortController, URL, console,
  });
  vm.runInContext(`${source}\nglobalThis.testAPI = { startApp, snapshot, state, setSession };`, context);
  const event = { sender: webContents, senderFrame: frame };
  return { ...context.testAPI, calls, publications, event,
    config: () => sdkConfig,
    invoke: (name, ...args) => handlers.get(`gct:${name}`)(event, ...args),
    invokeFrom: (sender, name, ...args) => handlers.get(`gct:${name}`)(sender, ...args),
    close: () => { listeners.get('before-quit')({ preventDefault() {} }); return quit.promise; },
  };
}

const count = (main, name) => main.calls.filter(call => call[0] === name).length;

test('first saved-session restoration prevents premature OAuth and keeps the existing storage identity', { timeout: 2000 }, async () => {
  const pending = deferred(), main = await harness({ getSession: () => pending.promise });
  const starting = main.startApp();
  while (!count(main, 'getSession')) await tick();
  await main.invoke('signIn');
  assert.equal(count(main, 'signIn'), 0);
  assert.equal(main.config().storageDir, '/synthetic-app-data/Grounded Class Tutor Local Proof/chatgpt');
  assert.equal(main.config().credentialEncryption.id, 'electron-safe-storage-v1');
  pending.resolve(connected); await starting;
  assert.equal(main.snapshot().session.status, 'connected');
  assert.equal(main.snapshot().models[0].slug, 'synthetic-model');
  assert.equal(count(main, 'signIn'), 0);
});

for (const code of ['storage_encryption_unavailable', 'storage_decryption_failed']) {
  test(`${code} recovery retries saved credentials and models without starting OAuth`, async () => {
    let allowed = false;
    const main = await harness({ getSession: () => allowed ? connected : blocked(code) });
    await main.startApp();
    assert.equal(main.snapshot().session.status, 'storage_unavailable');
    assert.equal(count(main, 'listModels'), 0);
    assert.doesNotMatch(JSON.stringify(main.snapshot()), /PRIVATE/);
    await main.invoke('signIn');
    assert.equal(count(main, 'signIn'), 0, 'Storage recovery must not offer an OAuth escape hatch.');
    allowed = true;
    await main.invoke('retryConnection');
    assert.equal(main.snapshot().session.status, 'connected');
    assert.equal(main.snapshot().models[0].slug, 'synthetic-model');
    assert.equal(count(main, 'signIn'), 0);
  });
}

test('temporary catalog failure recovers through retry without replacing the saved account', async () => {
  let failed = true;
  const main = await harness({ listModels: async () => {
    if (failed) throw { code: 'network_error', message: 'PRIVATE network detail' };
    return [{ slug: 'recovered-model', displayName: 'Recovered model' }];
  } });
  await main.startApp();
  assert.equal(main.snapshot().session.status, 'connected');
  assert.equal(main.snapshot().models.length, 0);
  failed = false;
  await main.invoke('retryConnection');
  assert.equal(main.snapshot().session.profileId, connected.profileId);
  assert.equal(main.snapshot().models[0].slug, 'recovered-model');
  assert.equal(count(main, 'signIn'), 0);
  assert.doesNotMatch(JSON.stringify(main.snapshot()), /PRIVATE/);
});

test('genuine saved revocation waits for explicit reconnect and never auto-opens OAuth', async () => {
  const main = await harness({ getSession: () => ({ ...connected, status: 'reauth_required', sharing: false }) });
  await main.startApp();
  assert.equal(main.snapshot().session.status, 'reauth_required');
  assert.equal(count(main, 'signIn'), 0);
  assert.equal(count(main, 'listModels'), 0);
  await main.invoke('signIn');
  assert.equal(count(main, 'signIn'), 1);
});

test('retry IPC rejects extra arguments, foreign windows, subframes, and changed renderer URLs', async () => {
  const main = await harness(); await main.startApp();
  const before = count(main, 'getSession');
  for (const action of ['retryConnection', 'cancelConnection']) {
    await assert.rejects(main.invoke(action, { overwrite: true }), /arguments/i);
    await assert.rejects(main.invokeFrom({ ...main.event, sender: {} }, action), /available/i);
    await assert.rejects(main.invokeFrom({ ...main.event, senderFrame: { url: main.event.senderFrame.url } }, action), /available/i);
  }
  main.event.senderFrame.url = 'https://untrusted.invalid/';
  await assert.rejects(main.invoke('retryConnection'), /available/i);
  assert.equal(count(main, 'getSession'), before);
});

test('cancelling a pending restore suppresses late subscriber state and does not start another read', async () => {
  let waiting = false;
  const pending = deferred(), main = await harness({ getSession: () => waiting ? pending.promise : connected });
  await main.startApp();
  main.setSession(blocked('storage_decryption_failed')); waiting = true;
  const retry = main.invoke('retryConnection'); await tick();
  const reads = count(main, 'getSession'), models = count(main, 'listModels');
  await main.invoke('cancelConnection');
  await main.invoke('retryConnection');
  assert.equal(count(main, 'getSession'), reads, 'Do not start concurrent OS prompt attempts.');
  pending.resolve(connected); await retry;
  assert.equal(main.snapshot().session.status, 'storage_unavailable');
  assert.equal(count(main, 'listModels'), models);
  assert.equal(main.snapshot().busy, null);
  assert.equal(count(main, 'signIn'), 0);
});

test('cancelling a catalog retry aborts its signal and rejects the eventual stale model list', async () => {
  let waiting = false, signal;
  const pending = deferred(), main = await harness({ listModels: options => {
    signal = options.signal;
    return waiting ? pending.promise : [];
  } });
  await main.startApp(); waiting = true;
  const retry = main.invoke('retryConnection'); await tick();
  await main.invoke('cancelConnection');
  assert.equal(signal.aborted, true);
  pending.resolve([{ slug: 'late-model', displayName: 'Should never publish' }]); await retry;
  assert.equal(main.snapshot().models.length, 0);
  assert.equal(main.snapshot().session.status, 'connected');
  assert.equal(count(main, 'signIn'), 0);
});

test('shutdown waits for a pending saved-session read and suppresses its late state', async () => {
  let waiting = false;
  const pending = deferred(), main = await harness({ getSession: () => waiting ? pending.promise : connected });
  await main.startApp();
  main.setSession(blocked('storage_encryption_unavailable')); waiting = true;
  const retry = main.invoke('retryConnection'); await tick();
  const quitting = main.close(); await tick();
  assert.equal(count(main, 'quit'), 0);
  const published = main.publications.length;
  pending.resolve(connected); await retry; await quitting;
  assert.equal(main.publications.length, published, 'No state may publish after shutdown begins.');
  assert.equal(main.snapshot().session.status, 'storage_unavailable');
  assert.equal(count(main, 'signIn'), 0);
  await assert.rejects(main.invoke('retryConnection'), /closing|available/i);
});
