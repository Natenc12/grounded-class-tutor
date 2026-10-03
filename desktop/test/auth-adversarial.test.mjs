import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { basename, dirname, extname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createChatGPT } from '../vendor/siwc-local/dist/index.js';
import { ConnectionStore } from '../vendor/siwc-local/dist/storage.js';
import { streamResponse } from '../vendor/siwc-local/dist/responses.js';
import { ProofError, responseOptions, validateAsk } from '../runtime.mjs';

// Synthetic accounts in disposable directories only; never Electron's userData.
async function accountFixture(t) {
  const directory = await mkdtemp(join(tmpdir(), 'gct-auth-adversarial-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const key = randomBytes(32);
  const encryption = {
    id: 'test-auth-adversarial', isAvailable: () => true,
    encrypt(plaintext) {
      const iv = randomBytes(12), cipher = createCipheriv('aes-256-gcm', key, iv);
      return Buffer.concat([iv, cipher.update(plaintext, 'utf8'), cipher.final(), cipher.getAuthTag()]);
    },
    decrypt(ciphertext) {
      const bytes = Buffer.from(ciphertext), cipher = createDecipheriv('aes-256-gcm', key, bytes.subarray(0, 12));
      cipher.setAuthTag(bytes.subarray(-16));
      return Buffer.concat([cipher.update(bytes.subarray(12, -16)), cipher.final()]).toString('utf8');
    },
  };
  const store = new ConnectionStore(directory, encryption);
  const saved = {
    version: 2, activeProfileId: 'synthetic-a', pendingRegistrations: [],
    profiles: ['a', 'b'].map(id => ({
      version: 1, id: `synthetic-${id}`, label: `Synthetic ${id}`, clientId: `synthetic_client_${id}`,
      subject: `synthetic_subject_${id}`, status: 'connected', scopes: ['openid', 'chatgpt.tokens.use.direct'],
      savedAt: new Date().toISOString(),
      credentials: { accessToken: `synthetic-access-${id}`, refreshToken: `synthetic-refresh-${id}`, expiresAt: Date.now() + 3600000 },
    })),
  };
  await store.withLock(() => store.write(saved));
  const client = createChatGPT({ appName: 'Synthetic auth adversary', appId: 'synthetic-auth-adversary', redirectPort: 0,
    storageDir: directory, credentialEncryption: encryption, openBrowser: () => assert.fail('Tests must never open OAuth') });
  return { client, store, saved, encryption, filename: join(directory, 'chatgpt-auth.json') };
}

const options = () => ({ model: 'synthetic-model', instructions: 'Use sources.', input: 'Synthetic source.' });
const encode = events => events.map(event => `data: ${JSON.stringify(event)}\n\n`).join('');
const wire = events => new Response(encode(events), { headers: { 'content-type': 'text/event-stream' } });
const completed = text => ({ type: 'response.completed', response: { status: 'completed', error: null,
  output: [{ type: 'message', role: 'assistant', content: [{ type: 'output_text', text }] }] } });

test('HTTP admission failures preserve credentials, expose no tokens, and do not retry', async t => {
  const { client, store, filename } = await accountFixture(t);
  const before = await readFile(filename, 'utf8');
  let attempts = 0;
  t.mock.method(globalThis, 'fetch', async (url, request) => {
    attempts++;
    assert.equal(url, 'https://api.openai.com/v1/responses');
    assert.equal(request.headers.authorization, 'Bearer synthetic-access-a');
    assert.equal(request.redirect, 'error');
    return Response.json({ error: { code: 'invalid_token', message: 'SECRET provider echo synthetic-access-a' } }, { status: 401 });
  });
  await assert.rejects(client.streamResponse(options()), error => error.code === 'invalid_token' && !error.message.includes('SECRET'));
  assert.equal(attempts, 1);
  assert.equal(await readFile(filename, 'utf8'), before);
  assert.equal((await store.withLock(() => store.read())).profiles[0].status, 'connected');
  assert.doesNotMatch(JSON.stringify(await client.getSession()), /synthetic-access|synthetic-refresh|SECRET/);
  assert.doesNotMatch(JSON.stringify(await client.listProfiles()), /synthetic-access|synthetic-refresh/);
});

test('switching profiles cancels an in-flight response and only the next request uses the new account', async t => {
  const { client } = await accountFixture(t);
  let firstRequest;
  const started = new Promise(resolve => { firstRequest = resolve; });
  const bearers = [];
  t.mock.method(globalThis, 'fetch', async (_url, request) => {
    bearers.push(request.headers.authorization);
    if (bearers.length === 1) {
      firstRequest();
      return new Promise((_resolve, reject) => request.signal.addEventListener('abort', () => reject(new DOMException('Synthetic abort', 'AbortError')), { once: true }));
    }
    return wire([{ type: 'response.output_text.delta', delta: 'New account response.' }, completed('New account response.')]);
  });
  const oldRequest = client.streamResponse(options());
  const cancelled = assert.rejects(oldRequest, error => error.code === 'cancelled');
  await started;
  await client.selectProfile('synthetic-b');
  await cancelled;
  assert.equal((await client.streamResponse(options())).text, 'New account response.');
  assert.deepEqual(bearers, ['Bearer synthetic-access-a', 'Bearer synthetic-access-b']);
});

test('SDK preserves encrypted credentials when the OS encryption provider becomes unavailable', async t => {
  const { client, encryption, filename } = await accountFixture(t);
  const before = await readFile(filename, 'utf8');
  encryption.isAvailable = () => false;
  t.mock.method(globalThis, 'fetch', () => assert.fail('No request may escape unavailable credential storage'));
  const session = await client.getSession();
  assert.equal(session.status, 'storage_unavailable');
  assert.equal(session.error.code, 'storage_encryption_unavailable');
  await assert.rejects(client.streamResponse(options()), error => error.code === 'storage_encryption_unavailable');
  assert.equal(await readFile(filename, 'utf8'), before);
});

for (const status of ['failed', 'incomplete']) {
  test(`reject contradictory completed event with response.status=${status}`, async t => {
    t.mock.method(globalThis, 'fetch', async () => wire([
      { type: 'response.output_text.delta', delta: 'Premature answer.' },
      { type: 'response.completed', response: { status, output: [] } },
    ]));
    await assert.rejects(streamResponse('synthetic-token', options(), new AbortController().signal));
  });
}

test('reject a completed event carrying a response error', async t => {
  t.mock.method(globalThis, 'fetch', async () => wire([
    { type: 'response.output_text.delta', delta: 'Premature answer.' },
    { type: 'response.completed', response: { status: 'completed', output: [], error: { code: 'subscription_sharing_usage_limit_exceeded' } } },
  ]));
  await assert.rejects(streamResponse('synthetic-token', options(), new AbortController().signal));
});

test('never append post-completion text merely because it shares a network chunk', async t => {
  t.mock.method(globalThis, 'fetch', async () => wire([
    { type: 'response.output_text.delta', delta: 'Completed answer.' },
    completed('Completed answer.'),
    { type: 'response.output_text.delta', delta: ' UNTRUSTED LATE TEXT' },
  ]));
  const outcome = await streamResponse('synthetic-token', options(), new AbortController().signal)
    .then(result => ({ result }), error => ({ error }));
  if (outcome.error) assert.equal(outcome.error.code, 'invalid_stream');
  else assert.equal(outcome.result.text, 'Completed answer.');
});

test('reject disagreement between streamed text and the final response text', async t => {
  t.mock.method(globalThis, 'fetch', async () => wire([
    { type: 'response.output_text.delta', delta: 'The number is 1.' },
    { type: 'response.completed', response: { status: 'completed', output: [{ type: 'message', role: 'assistant', content: [{ type: 'output_text', text: 'The number is 10.' }] }] } },
  ]));
  await assert.rejects(streamResponse('synthetic-token', options(), new AbortController().signal));
});

test('malformed, truncated, and failed streams discard all partial text and never retry', async t => {
  const streams = [
    'data: {malformed}\n\n',
    encode([{ type: 'response.output_text.delta', delta: 'Partial answer.' }]) + 'data: [DONE]\n\n',
    encode([{ type: 'response.output_text.delta', delta: 'Partial answer.' }, { type: 'response.incomplete' }]),
    encode([{ type: 'response.output_text.delta', delta: 'Partial answer.' }, { type: 'response.failed', response: { error: { code: 'subscription_sharing_usage_limit_exceeded' } } }]),
  ];
  let requests = 0;
  t.mock.method(globalThis, 'fetch', async () => new Response(streams[requests++], { headers: { 'content-type': 'text/event-stream' } }));
  for (let index = 0; index < streams.length; index++) {
    await assert.rejects(streamResponse('synthetic-token', options(), new AbortController().signal));
    assert.equal(requests, index + 1);
  }
});

test('SSE byte splitting preserves Unicode and mixed CRLF boundaries', async t => {
  const bytes = new TextEncoder().encode(encode([
    { type: 'response.output_text.delta', delta: 'Mémoire 🧠 [S1]' }, completed('Mémoire 🧠 [S1]'),
  ]).replaceAll('\n', '\r\n'));
  t.mock.method(globalThis, 'fetch', async () => new Response(new ReadableStream({
    start(controller) { for (const byte of bytes) controller.enqueue(Uint8Array.of(byte)); controller.close(); },
  }), { headers: { 'content-type': 'text/event-stream' } }));
  assert.equal((await streamResponse('synthetic-token', options(), new AbortController().signal)).text, 'Mémoire 🧠 [S1]');
});

test('reasoning items and multiple text parts agree with the final completed answer', async t => {
  const terminal = completed('First. Second.');
  terminal.response.output = [
    { type: 'reasoning', summary: [] },
    { type: 'message', role: 'assistant', content: [
      { type: 'output_text', text: 'First. ' }, { type: 'output_text', text: 'Second.' },
    ] },
  ];
  t.mock.method(globalThis, 'fetch', async () => wire([
    { type: 'response.output_text.delta', delta: 'First. ' },
    { type: 'response.output_text.delta', delta: 'Second.' }, terminal,
  ]));
  assert.equal((await streamResponse('synthetic-token', options(), new AbortController().signal)).text, 'First. Second.');
});

for (const output of [undefined, null, {}, [{ type: 'message', role: 'assistant', content: [{ type: 'output_text' }] }]]) {
  test(`reject a completion without valid output structure: ${JSON.stringify(output)}`, async t => {
    t.mock.method(globalThis, 'fetch', async () => wire([
      { type: 'response.output_text.delta', delta: 'Unverified text.' },
      { type: 'response.completed', response: { status: 'completed', output } },
    ]));
    await assert.rejects(streamResponse('synthetic-token', options(), new AbortController().signal), error => error.code === 'invalid_stream');
  });
}

// Execute the actual main-process functions with Electron and SDK surfaces stubbed.
// This never starts Electron, reads userData, opens a browser, or touches real auth.
async function mainHarness(overrides = {}) {
  let source = await readFile(new URL('../main.mjs', import.meta.url), 'utf8');
  source = source.replace(/^import [\s\S]*?;\n/gm, '').replaceAll('import.meta.url', JSON.stringify(new URL('../main.mjs', import.meta.url).href));
  source = source.replace(/startApp\(\)\.catch\([^\n]+\);/, '');
  const actions = new Map();
  const mainFrame = { url: new URL('../renderer/index.html', import.meta.url).href };
  const webContents = { mainFrame, send() {} };
  const fakeWindow = { webContents, isDestroyed: () => false };
  const context = vm.createContext({
    app: { setName() {}, setPath() {}, getPath: () => '/synthetic-unused', requestSingleInstanceLock: () => true, on() {} },
    BrowserWindow: class {}, dialog: {}, safeStorage: {}, shell: {},
    createChatGPT() { assert.fail('Do not create a real SDK client in main harness'); },
    CHATGPT_USAGE_URL: 'https://chatgpt.com/settings/usage',
    ipcMain: { handle: (name, action) => actions.set(name, action) },
    basename, dirname, extname, join, fileURLToPath, pathToFileURL,
    ProofError, responseOptions, validateAsk, runBridge: overrides.runBridge ?? (() => assert.fail('Unexpected bridge')),
    stopBridges: async () => {}, process: { platform: process.platform, exit() { assert.fail('Unexpected process exit'); } },
    structuredClone, AbortController, URL, console,
    __sdk: overrides.sdk ?? {}, __window: fakeWindow,
  });
  vm.runInContext(`${source}\nwindow = __window; chatgpt = __sdk; registerActions(); globalThis.testAPI = { state, snapshot, setSession, refreshModels, safeMessage, ask, setSource: value => { source = value; } };`, context);
  const event = { sender: webContents, senderFrame: mainFrame };
  return { ...context.testAPI, invoke: (name, ...args) => actions.get(`gct:${name}`)(event, ...args) };
}

test('main surfaces credential-storage recovery errors returned inside session state', async () => {
  const main = await mainHarness();
  main.setSession({ status: 'reauth_required', sharing: false, error: {
    code: 'storage_encryption_unavailable', message: 'SECRET error text from provider', retryable: true,
  } });
  assert.match(main.snapshot().notice ?? '', /unlock|credential|storage/i);
  assert.doesNotMatch(JSON.stringify(main.snapshot()), /SECRET/);
});

test('main tells the user when remote revocation failed after local sign-out', async () => {
  const main = await mainHarness();
  assert.match(main.safeMessage({ code: 'revocation_failed', message: 'SECRET provider echo' }), /remote|ChatGPT Settings|disconnect (the )?app/i);
  assert.doesNotMatch(main.safeMessage({ code: 'revocation_failed', message: 'SECRET provider echo' }), /SECRET/);
});

test('main rejects a stale account model catalog after a profile switch', async () => {
  let finish;
  const main = await mainHarness({ sdk: { listModels: () => new Promise(resolve => { finish = resolve; }) } });
  main.setSession({ status: 'connected', sharing: true, profileId: 'synthetic-a' });
  const loading = main.refreshModels();
  main.setSession({ status: 'connected', sharing: true, profileId: 'synthetic-b' });
  finish([{ slug: 'a-only', displayName: 'Account A only' }]);
  await loading;
  assert.equal(main.snapshot().models.length, 0);
});

test('main refuses to publish an answer completed after an account switch', async () => {
  let finish, entered;
  const generating = new Promise(resolve => { entered = resolve; });
  const main = await mainHarness({
    sdk: { streamResponse: () => { entered(); return new Promise(resolve => { finish = resolve; }); } },
    runBridge: async (_repo, _payload, configuration) => {
      await configuration.generate([{ role: 'system', content: 'Grounded only.' }, { role: 'user', content: 'Source.' }]);
      return { state: 'GROUNDED', answer_prose: 'Stale answer.' };
    },
  });
  main.setSession({ status: 'connected', sharing: true, profileId: 'synthetic-a' });
  main.state.models = [{ slug: 'synthetic-model' }];
  main.state.document = { pages: [{ page_or_slide: 1 }] };
  main.setSource({ sample: true });
  const asking = main.invoke('ask', { question: 'Synthetic question', model: 'synthetic-model', pages: [1] });
  await generating;
  main.setSession({ status: 'connected', sharing: true, profileId: 'synthetic-b' });
  finish({ text: 'Stale answer.' });
  const state = await asking;
  assert.equal(state.result, undefined);
  assert.match(state.notice, /cancel/i);
  assert.equal(state.busy, null);
});
