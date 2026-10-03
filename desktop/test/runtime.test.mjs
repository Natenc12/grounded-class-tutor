import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pythonEnvironment, pythonExecutable, responseOptions, runBridge, stopBridges, validateAsk } from '../runtime.mjs';

const root = fileURLToPath(new URL('../../', import.meta.url));

test('grounder guidance becomes instructions; sources remain user input; no API-only options', () => {
  const options = responseOptions([
    { role: 'system', content: 'Only use the supplied sources.' },
    { role: 'user', content: 'Untrusted source text [S1].' },
  ], 'per-account-model', new AbortController().signal);
  assert.equal(options.instructions.trim(), 'Only use the supplied sources.');
  assert.deepEqual(options.input, [{ role: 'user', content: 'Untrusted source text [S1].' }]);
  assert.deepEqual(Object.keys(options).sort(), ['input', 'instructions', 'model', 'signal']);
});

test('unknown message roles and oversized context are refused before SDK dispatch', () => {
  assert.throws(() => responseOptions([{ role: 'tool', content: 'Run something.' }], 'm'), /Invalid grounding/);
  assert.throws(() => responseOptions([{ role: 'system', content: 'a'.repeat(60001) }], 'm'), /fewer pages/);
});

test('only main-selected documents and current account models can be asked about', () => {
  const document = { pages: [{ page_or_slide: 1 }, { page_or_slide: 4 }] };
  const models = [{ slug: 'allowed' }];
  const request = { question: ' Why? ', model: 'allowed', pages: [4] };
  assert.equal(validateAsk(request, document, models).question, 'Why?');
  for (const bad of [{ ...request, file_path: '/tmp/arbitrary.pdf' },
    { ...request, model: 'other' }, { ...request, pages: [2] },
    { ...request, pages: [1, 1] }, { ...request, question: '' }]) {
    assert.throws(() => validateAsk(bad, document, models));
  }
});

test('Python receives neither provider credentials nor inherited startup settings', () => {
  const environment = pythonEnvironment(root);
  assert.equal(environment.PYTHON_DOTENV_DISABLED, '1');
  for (const key of ['OPENAI_API_KEY', 'ACCESS_TOKEN', 'CODEX_HOME', 'PYTHONSTARTUP', 'NODE_OPTIONS']) {
    assert.equal(key in environment, false);
  }
});

test('sample inspection requires no database, embeddings, or model', async () => {
  const document = await runBridge(root, { sample: true }, { inspect: true });
  assert.equal(document.type, 'document');
  assert.equal(document.filename, 'local-proof-sample.pdf');
  assert.ok(document.pages.some(page => page.page_or_slide === 1));
});

test('complete SDK text runs through actual GCT citation resolution', async () => {
  let calls = 0;
  const result = await runBridge(root, { sample: true, question: 'What is active recall?', pages: [1] }, {
    generate: async messages => {
      calls++;
      assert.match(messages.find(message => message.role === 'user').content, /SOURCES/);
      return 'Active recall means retrieving information from memory. [S1]\nCOVERAGE: complete';
    },
  });
  assert.equal(calls, 1);
  assert.equal(result.state, 'GROUNDED');
  assert.equal(result.citations[0].file, 'local-proof-sample.pdf');
  assert.equal(result.citations[0].page_or_slide, 1);
});

test('a failed SDK stream cannot become a grounded answer or trigger another call', async () => {
  let calls = 0;
  await assert.rejects(runBridge(root, { sample: true, question: 'What is active recall?', pages: [1] }, {
    generate: async () => { calls++; throw new Error('Simulated incomplete stream'); },
  }), /incomplete stream/);
  assert.equal(calls, 1);
});

test('cancellation terminates the local bridge while generation is pending', async () => {
  const controller = new AbortController();
  let generated;
  const started = new Promise(resolve => { generated = resolve; });
  const result = runBridge(root, { sample: true, question: 'What is active recall?', pages: [1] }, {
    signal: controller.signal,
    generate: async () => { generated(); return new Promise(() => {}); },
  });
  await started;
  controller.abort();
  await assert.rejects(result, /cancelled/);
  await stopBridges();
});

test('cancellation rejects only after reaping a child that ignores graceful termination', { skip: process.platform === 'win32' }, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'gct-child-test-'));
  const executable = join(directory, 'stalled-parser');
  await writeFile(executable, `#!${pythonExecutable(root)}\nimport json, os, signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint(json.dumps({'type':'generate','messages':[{'role':'user','content':str(os.getpid())}]}), flush=True)\nwhile True: time.sleep(0.1)\n`, { mode: 0o700 });
  const controller = new AbortController();
  let started;
  const ready = new Promise(resolve => { started = resolve; });
  let pid;
  const result = runBridge(root, { sample: true }, {
    signal: controller.signal, executable,
    generate: async messages => { pid = Number(messages[0].content); started(); return new Promise(() => {}); },
  });
  try {
    await ready;
    controller.abort();
    await assert.rejects(result, /cancelled/);
    assert.throws(() => process.kill(pid, 0), { code: 'ESRCH' });
  } finally { await stopBridges(); await rm(directory, { recursive: true, force: true }); }
});


test('ask scope is explicit and cannot smuggle pages into class-wide search', () => {
  const models = [{ slug: 'available-model' }];
  const library = { selectedClassId: 'class', selectedDocumentId: 'document' };
  const document = { pages: [{ page_or_slide: 1 }], sample: false };
  const base = { question: ' Recall? ', model: 'available-model' };
  assert.equal(validateAsk({ ...base, scope: 'class' }, undefined, models, library).scope, 'class');
  assert.equal(validateAsk({ ...base, scope: 'document' }, document, models, library).scope, 'document');
  assert.deepEqual(validateAsk({ ...base, scope: 'pages', pages: [1] }, document, models, library).pages, [1]);
  for (const request of [{ ...base, scope: 'class', pages: [1] }, { ...base, scope: 'arbitrary' },
    { ...base, scope: 'document', file_path: '/private/path' }]) {
    assert.throws(() => validateAsk(request, document, models, library));
  }
  assert.throws(() => validateAsk({ ...base, scope: 'class' }, document, models, {}));
  assert.throws(() => validateAsk({ ...base, scope: 'document' }, { ...document, sample: true }, models, library));
});
