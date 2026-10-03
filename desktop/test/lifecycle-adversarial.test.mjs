import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';
import { basename, dirname, extname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { ProofError, responseOptions, validateAsk } from '../runtime.mjs';

const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const connected = { status: 'connected', sharing: true, profileId: 'synthetic-account' };
const document = (filename = 'synthetic.pdf') => ({ type: 'document', filename, page_count: 1,
  pages: [{ page_or_slide: 1, text: 'Synthetic source.', truncated: false }], synthetic: false, truncated: false });

// Exercise the production action handlers with synthetic, in-memory surfaces.
// The harness cannot start Electron, open a browser, or access account storage.
async function harness(overrides = {}) {
  let source = await readFile(new URL('../main.mjs', import.meta.url), 'utf8');
  source = source.replace(/^import .*?;\n/gm, '').replaceAll('import.meta.url', JSON.stringify(new URL('../main.mjs', import.meta.url).href));
  source = source.replace(/startApp\(\)\.catch\([^\n]+\);/, '');
  const handlers = new Map(), listeners = new Map(), calls = [];
  const quit = deferred();
  const mainFrame = { url: new URL('../renderer/index.html', import.meta.url).href };
  const webContents = { mainFrame, send() {} };
  const fakeWindow = { webContents, isDestroyed: () => false };
  const sourceStore = overrides.sourceStore ?? {
    stage: async original => { calls.push(['stage', original]); return { file_path: '/synthetic-root/source/new.pdf' }; },
    discard: async selected => { calls.push(['discard', selected]); },
    close: async () => { calls.push(['store-close']); },
  };
  const context = vm.createContext({
    app: { setName() {}, setPath() {}, getPath: () => '/synthetic-unused', requestSingleInstanceLock: () => true,
      on: (name, callback) => listeners.set(name, callback), quit: () => { calls.push(['quit']); quit.resolve(); } },
    BrowserWindow: class {}, dialog: { showOpenDialog: overrides.dialog ?? (async () => ({ canceled: false, filePaths: ['/synthetic/source.pdf'] })) },
    safeStorage: {}, shell: { openExternal: async () => { calls.push(['external']); } },
    createChatGPT() { assert.fail('No real SDK client may be created'); },
    CHATGPT_USAGE_URL: 'https://chatgpt.com/settings/usage',
    ipcMain: { handle: (name, action) => handlers.set(name, action) },
    basename, dirname, extname, join, fileURLToPath, pathToFileURL,
    ProofError, responseOptions, validateAsk,
    runBridge: overrides.runBridge ?? (async () => { calls.push(['bridge']); return document(); }),
    stopBridges: async () => { calls.push(['stop-bridges']); },
    createSourceStore: () => sourceStore,
    stat: async () => { calls.push(['stat']); return { isFile: () => true, size: 20 }; },
    mkdtemp: async () => { calls.push(['mkdtemp']); return '/synthetic-root/source'; },
    copyFile: async () => { calls.push(['copy']); }, mkdir: async () => {},
    rm: async () => { calls.push(['rm']); },
    process: { platform: process.platform, exit() { assert.fail('Unexpected process exit'); } },
    structuredClone, AbortController, URL, console,
    __sdk: { cancelSignIn() {}, ...overrides.sdk }, __window: fakeWindow, __sourceStore: sourceStore,
  });
  vm.runInContext(`${source}\nwindow = __window; chatgpt = __sdk; sourceStore = __sourceStore;
    registerActions(); globalThis.testAPI = { state, snapshot, setSession, refreshModels,
    setSource: value => { source = value; }, source: () => source };`, context);
  const event = { sender: webContents, senderFrame: mainFrame };
  return { ...context.testAPI, calls, sourceStore, event, window: fakeWindow,
    invoke: (name, ...args) => handlers.get(`gct:${name}`)(event, ...args),
    invokeFrom: (event, name, ...args) => handlers.get(`gct:${name}`)(event, ...args),
    close: () => { listeners.get('before-quit')({ preventDefault() {} }); return quit.promise; },
  };
}

test('reject every new renderer action once app shutdown starts', async () => {
  const main = await harness({ sdk: { listModels: async () => [{ slug: 'synthetic-model' }] } });
  main.setSession(connected);
  await main.close();
  for (const action of ['getState', 'listModels', 'chooseFile', 'useSample', 'openUsage']) {
    await assert.rejects(main.invoke(action), /closing|closed|available/i, action);
  }
  assert.equal(main.calls.some(([name]) => ['stage', 'stat', 'mkdtemp', 'bridge', 'external'].includes(name)), false);
});

test('a file dialog returning after shutdown cannot stage or inspect another document', async () => {
  const chooser = deferred();
  const main = await harness({ dialog: () => chooser.promise });
  const selection = main.invoke('chooseFile');
  await main.close();
  chooser.resolve({ canceled: false, filePaths: ['/synthetic/late.pdf'] });
  await selection;
  assert.equal(main.calls.some(([name]) => ['stage', 'stat', 'mkdtemp', 'copy', 'bridge'].includes(name)), false);
  assert.equal(main.snapshot().document, undefined);
});

test('IPC rejects another window, a subframe, or a changed main-frame URL', async () => {
  const main = await harness();
  await assert.rejects(main.invokeFrom({ ...main.event, sender: {} }, 'useSample'), /available/i);
  await assert.rejects(main.invokeFrom({ ...main.event, senderFrame: { url: main.event.senderFrame.url } }, 'useSample'), /available/i);
  main.event.senderFrame.url = 'https://synthetic.invalid/';
  await assert.rejects(main.invoke('useSample'), /available/i);
  assert.equal(main.calls.length, 0);
});

test('IPC rejects unexpected arguments before performing any operation', async () => {
  const main = await harness();
  await assert.rejects(main.invoke('useSample', { file_path: '/synthetic/private.pdf' }), /arguments/i);
  await assert.rejects(main.invoke('ask'), /arguments/i);
  await assert.rejects(main.invoke('ask', {}, {}), /arguments/i);
  assert.equal(main.calls.length, 0);
});

test('failed candidate inspection preserves the prior source and removes only the candidate', async () => {
  const main = await harness({ runBridge: async () => { throw new ProofError('document_error', 'Synthetic parse failure.'); } });
  const previous = { file_path: '/synthetic-root/source/previous.pdf' };
  main.setSource(previous);
  main.state.document = document('previous.pdf');
  await main.invoke('chooseFile');
  assert.equal(main.source(), previous);
  assert.equal(main.snapshot().document.filename, 'previous.pdf');
  assert.equal(main.calls.filter(([name]) => name === 'discard').length, 1);
  assert.equal(main.calls.find(([name]) => name === 'discard')[1].file_path, '/synthetic-root/source/new.pdf');
});

test('successful source replacement releases the previous copy only after inspection', async () => {
  const inspection = deferred();
  const main = await harness({ runBridge: () => inspection.promise });
  const previous = { file_path: '/synthetic-root/source/previous.pdf' };
  main.setSource(previous);
  main.state.document = document('previous.pdf');
  const selecting = main.invoke('chooseFile');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(main.calls.some(([name]) => name === 'discard'), false);
  assert.equal(main.source(), previous);
  inspection.resolve(document('new.pdf'));
  await selecting;
  assert.equal(main.snapshot().document.filename, 'new.pdf');
  assert.equal(main.calls.filter(([name]) => name === 'discard').length, 1);
  assert.equal(main.calls.find(([name]) => name === 'discard')[1], previous);
});

test('overlapping same-account catalog refreshes cannot publish the older response last', async () => {
  const first = deferred(), second = deferred();
  let calls = 0;
  const main = await harness({ sdk: { listModels: () => (++calls === 1 ? first : second).promise } });
  main.setSession(connected);
  const older = main.refreshModels();
  const newer = main.refreshModels();
  second.resolve([{ slug: 'current-model', displayName: 'Current' }]);
  await newer;
  first.resolve([{ slug: 'stale-model', displayName: 'Stale' }]);
  await older;
  assert.equal(main.snapshot().models[0].slug, 'current-model');
});

test('disconnect and reconnect to the same account invalidates an old catalog response', async () => {
  const pending = deferred();
  const main = await harness({ sdk: { listModels: () => pending.promise } });
  main.setSession(connected);
  const refresh = main.refreshModels();
  main.setSession({ status: 'disconnected', sharing: false });
  main.setSession(connected);
  pending.resolve([{ slug: 'old-connection-model', displayName: 'Old connection' }]);
  await refresh;
  assert.equal(main.snapshot().models.length, 0);
});

test('shutdown cancels a catalog request but waits for its credential work to settle', async () => {
  const pending = deferred();
  let requestSignal;
  const main = await harness({ sdk: { listModels: ({ signal }) => { requestSignal = signal; return pending.promise; } } });
  main.setSession(connected);
  const refresh = main.refreshModels();
  const quitting = main.close();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requestSignal.aborted, true);
  assert.equal(main.calls.some(([name]) => name === 'quit'), false);
  pending.resolve([{ slug: 'late-model', displayName: 'Late' }]);
  await refresh;
  await quitting;
  assert.equal(main.snapshot().models.length, 0);
});

test('a late sign-in completion after shutdown cannot initiate a model request', async () => {
  const pending = deferred();
  let modelRequests = 0;
  const main = await harness({ sdk: { signIn: () => pending.promise, listModels: async () => { modelRequests++; return []; } } });
  const connecting = main.invoke('signIn');
  const quitting = main.close();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(main.calls.some(([name]) => name === 'quit'), false);
  pending.resolve(connected);
  await connecting;
  await quitting;
  assert.equal(modelRequests, 0);
});

test('shutdown waits for SDK generation even after its Python bridge has already rejected', async () => {
  const pending = deferred(), started = deferred();
  let requestSignal;
  const main = await harness({
    sdk: { streamResponse: options => { requestSignal = options.signal; started.resolve(); return pending.promise; } },
    runBridge: async (_root, _payload, { signal, generate }) => {
      // The real bridge rejects promptly on abort while SDK token rotation can
      // continue until its replacement credential has been persisted.
      generate([{ role: 'system', content: 'Rules.' }, { role: 'user', content: 'Source.' }], signal).catch(() => {});
      return new Promise((_resolve, reject) => signal.addEventListener('abort',
        () => reject(new ProofError('cancelled', 'Cancelled.')), { once: true }));
    },
  });
  main.setSession(connected);
  main.setSource({ sample: true });
  main.state.document = document();
  main.state.models = [{ slug: 'synthetic-model' }];
  const asking = main.invoke('ask', { question: 'Question?', model: 'synthetic-model', pages: [1] });
  await started.promise;
  const quitting = main.close();
  await asking;
  assert.equal(requestSignal.aborted, true);
  assert.equal(main.calls.some(([name]) => name === 'quit'), false);
  pending.resolve({ text: 'Late result after credential checkpoint.' });
  await quitting;
  assert.equal(main.snapshot().result, undefined);
});

test('shutdown waits for disconnect to persist local revocation without starting another session read', async () => {
  const pending = deferred();
  let sessionReads = 0;
  const main = await harness({ sdk: {
    disconnect: () => pending.promise,
    getSession: async () => { sessionReads++; return { status: 'disconnected', sharing: false }; },
  } });
  main.setSession(connected);
  const disconnecting = main.invoke('signOut');
  const quitting = main.close();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(main.calls.some(([name]) => name === 'quit'), false);
  pending.resolve();
  await disconnecting;
  await quitting;
  assert.equal(sessionReads, 0);
});

test('shutdown waits for the source store to finish cleaning a pending copy', async () => {
  const staging = deferred(), cleanup = deferred();
  const main = await harness({ sourceStore: {
    stage: () => staging.promise,
    discard: async () => {},
    close: async () => { await staging.promise; await cleanup.promise; },
  } });
  const selecting = main.invoke('chooseFile');
  await new Promise(resolve => setImmediate(resolve));
  const quitting = main.close();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(main.calls.some(([name]) => name === 'quit'), false);
  staging.resolve({ file_path: '/synthetic-root/source/late.pdf' });
  await selecting;
  assert.equal(main.snapshot().document, undefined);
  assert.equal(main.calls.some(([name]) => name === 'quit'), false);
  cleanup.resolve();
  await quitting;
  assert.equal(main.calls.filter(([name]) => name === 'quit').length, 1);
});

test('cancelling document selection before a file dialog returns prevents staging', async () => {
  const chooser = deferred();
  const main = await harness({ dialog: () => chooser.promise });
  const selecting = main.invoke('chooseFile');
  await main.invoke('cancelAsk');
  chooser.resolve({ canceled: false, filePaths: ['/synthetic/late.pdf'] });
  await selecting;
  assert.equal(main.calls.some(([name]) => ['stage', 'bridge'].includes(name)), false);
  assert.equal(main.snapshot().busy, null);
  assert.equal(main.snapshot().document, undefined);
});

test('a source cleanup failure cannot leave every future action permanently busy', async () => {
  const main = await harness({
    sourceStore: {
      stage: async () => ({ file_path: '/synthetic-root/source/new.pdf' }),
      discard: async () => { throw new Error('Synthetic filesystem failure'); },
      close: async () => {},
    },
    runBridge: async () => { throw new ProofError('document_error', 'Synthetic parse failure.'); },
  });
  await main.invoke('chooseFile');
  assert.equal(main.snapshot().busy, null);
});

test('cancelling an in-flight inspection preserves the old document and discards the candidate', async () => {
  const inspection = deferred();
  const main = await harness({ runBridge: () => inspection.promise });
  const previous = { file_path: '/synthetic-root/source/previous.pdf' };
  main.setSource(previous);
  main.state.document = document('previous.pdf');
  const selecting = main.invoke('chooseFile');
  await new Promise(resolve => setImmediate(resolve));
  await main.invoke('cancelAsk');
  inspection.resolve(document('new.pdf'));
  await selecting;
  assert.equal(main.snapshot().document.filename, 'previous.pdf');
  assert.equal(main.source(), previous);
  assert.equal(main.calls.filter(([name]) => name === 'discard').length, 1);
  assert.equal(main.calls.find(([name]) => name === 'discard')[1].file_path, '/synthetic-root/source/new.pdf');
  assert.equal(main.snapshot().busy, null);
});

test('duplicate questions and source replacements cannot race a running answer', async () => {
  const answer = deferred();
  let bridgeRequests = 0;
  const main = await harness({ runBridge: () => { bridgeRequests++; return answer.promise; } });
  main.setSession(connected);
  main.setSource({ sample: true });
  main.state.document = document();
  main.state.models = [{ slug: 'synthetic-model' }];
  const question = { question: 'What is in the source?', model: 'synthetic-model', pages: [1] };
  const asking = main.invoke('ask', question);
  await main.invoke('ask', question);
  await main.invoke('chooseFile');
  assert.equal(bridgeRequests, 1);
  assert.equal(main.snapshot().busy, 'ask');
  answer.resolve({ state: 'GROUNDED', answer_prose: 'Synthetic completed answer.' });
  await asking;
  assert.equal(main.snapshot().busy, null);
  assert.equal(main.snapshot().result.answer_prose, 'Synthetic completed answer.');
});
