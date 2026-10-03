import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { validLibraryEvent, validSearchStatus } from '../protocol.mjs';
import { pythonExecutable, runBridge, runLibrary, stopBridges } from '../runtime.mjs';

const root = fileURLToPath(new URL('../../', import.meta.url));
const classId = '10000000-0000-4000-8000-000000000001';
const status = (state = 'ready') => ({ state, mode: state === 'ready' ? 'hybrid' : 'lexical',
  message: 'Synthetic local search status.' });
const refusal = { type: 'result', result: { state: 'REFUSAL', answer_prose: null,
  citations: [], coverage: { complete: false, gaps: ['No supported source.'] },
  integrity: { ok: true, reasons: [] }, error: null } };
const emit = event => `print(${JSON.stringify(JSON.stringify(event))}, flush=True)`;

async function fakeBridge(t, program) {
  const folder = await mkdtemp(join(tmpdir(), 'gct-semantic-adversarial-'));
  t.after(async () => { await stopBridges(); await rm(folder, { recursive: true, force: true }); });
  const executable = join(folder, 'bridge');
  const libraryPath = join(folder, 'library.sqlite3');
  await writeFile(executable, `#!${pythonExecutable(root)}\nimport sys, json, os, signal, time\nsys.stdin.readline()\n${program}\n`, { mode: 0o700 });
  return { executable, libraryPath, folder };
}

async function readPid(path) {
  const deadline = Date.now() + 3000;
  while (Date.now() < deadline) {
    try { return Number(await readFile(path, 'utf8')); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  assert.fail('Synthetic child did not start.');
}

function assertReaped(pid) {
  assert.throws(() => process.kill(pid, 0), error => error.code === 'ESRCH');
}

test('semantic states cannot falsely advertise hybrid readiness or add untrusted fields', () => {
  for (const state of ['unavailable', 'missing', 'stale', 'ready', 'limited', 'failed']) {
    const value = status(state);
    assert.equal(validSearchStatus(value), true, state);
    assert.equal(validSearchStatus({ ...value, mode: value.mode === 'hybrid' ? 'lexical' : 'hybrid' }), false);
    assert.equal(validSearchStatus({ ...value, model_path: '/private/model.onnx' }), false);
    for (const operation of ['search_status', 'prepare_search']) {
      assert.equal(validLibraryEvent({ type: 'library', operation, value }, { operation, class_id: classId }), true);
    }
  }
  for (const value of [null, [], {}, status('building'), { ...status(), message: '' },
    { ...status(), message: '   ' }, { ...status(), message: 'x'.repeat(501) },
    { ...status(), state: true }, { ...status(), mode: 'cloud' }]) {
    assert.equal(validSearchStatus(value), false);
  }
  assert.equal(validSearchStatus({ ...status(), message: '🎓'.repeat(500) }), true);
  assert.equal(validSearchStatus({ ...status(), message: '🎓'.repeat(501) }), false);
});

test('preparing a local index cannot request model generation even if a callback is supplied',
  { skip: process.platform === 'win32' }, async t => {
    const peer = await fakeBridge(t, `${emit({ type: 'generate', messages: [] })}\nsys.stdin.readline()`);
    let generationCalls = 0;
    await assert.rejects(runLibrary(root, peer.libraryPath, { operation: 'prepare_search', class_id: classId }, {
      executable: peer.executable, timeoutMs: 2000,
      generate: async () => { generationCalls++; assert.fail('Local indexing cannot generate.'); },
    }), error => error.code === 'bridge_invalid');
    assert.equal(generationCalls, 0);
  });

test('an automatic ask reports its validated search mode before returning a local refusal',
  { skip: process.platform === 'win32' }, async t => {
    const value = status('missing');
    const peer = await fakeBridge(t, `${emit({ type: 'search_status', value })}\n${emit(refusal)}`);
    const seen = [];
    const result = await runLibrary(root, peer.libraryPath, { operation: 'ask', class_id: classId, question: 'Synthetic?' }, {
      executable: peer.executable, onSearchStatus: value => seen.push(value),
      generate: async () => assert.fail('No account is needed for a local refusal.'),
    });
    assert.equal(result.state, 'REFUSAL');
    assert.deepEqual(seen, [value]);
  });

test('duplicate search-mode events fail before any generation can occur',
  { skip: process.platform === 'win32' }, async t => {
    const event = { type: 'search_status', value: status() };
    const peer = await fakeBridge(t, `${emit(event)}\n${emit(event)}\n${emit(refusal)}`);
    const seen = [];
    await assert.rejects(runLibrary(root, peer.libraryPath, { operation: 'ask', class_id: classId, question: 'Synthetic?' }, {
      executable: peer.executable, onSearchStatus: value => seen.push(value),
      generate: async () => assert.fail('Duplicate mode events must not reach generation.'),
    }), error => error.code === 'bridge_invalid');
    assert.equal(seen.length, 1);
  });

test('a child cannot announce a different retrieval mode after generation has begun',
  { skip: process.platform === 'win32' }, async t => {
    const peer = await fakeBridge(t, `${emit({ type: 'search_status', value: status() })}\n${emit({ type: 'generate', messages: [] })}\nsys.stdin.readline()\n${emit({ type: 'search_status', value: status('failed') })}\n${emit(refusal)}`);
    let generated = 0, callbacks = 0;
    await assert.rejects(runLibrary(root, peer.libraryPath, { operation: 'ask', class_id: classId, question: 'Synthetic?' }, {
      executable: peer.executable, generate: async () => { generated++; return 'Synthetic response.'; },
      onSearchStatus: () => { callbacks++; },
    }), error => error.code === 'bridge_invalid');
    assert.equal(generated, 1);
    assert.equal(callbacks, 1);
  });

for (const next of [{ type: 'generate', messages: [] }, refusal]) {
  test(`automatic retrieval must report its mode before ${next.type}`,
    { skip: process.platform === 'win32' }, async t => {
      const peer = await fakeBridge(t, emit(next));
      let generated = 0, callbacks = 0;
      await assert.rejects(runLibrary(root, peer.libraryPath,
        { operation: 'ask', class_id: classId, question: 'Synthetic?' }, {
          executable: peer.executable,
          generate: async () => { generated++; return 'This must never be reached.'; },
          onSearchStatus: () => { callbacks++; },
        }), error => error.code === 'bridge_invalid');
      assert.equal(generated, 0);
      assert.equal(callbacks, 0);
    });
}

for (const operation of ['search_status', 'prepare_search', 'list_classes']) {
  test(`nonterminal search-status events are rejected for ${operation}`,
    { skip: process.platform === 'win32' }, async t => {
      const peer = await fakeBridge(t, emit({ type: 'search_status', value: status() }));
      let callbacks = 0;
      await assert.rejects(runLibrary(root, peer.libraryPath, { operation, class_id: classId }, {
        executable: peer.executable, onSearchStatus: () => { callbacks++; },
      }), error => error.code === 'bridge_invalid');
      assert.equal(callbacks, 0);
    });
}

test('an inspector cannot smuggle a local search-status event into application state',
  { skip: process.platform === 'win32' }, async t => {
    const peer = await fakeBridge(t, emit({ type: 'search_status', value: status() }));
    let callbacks = 0;
    await assert.rejects(runBridge(root, { sample: true }, { executable: peer.executable, inspect: true,
      onSearchStatus: () => { callbacks++; } }), error => error.code === 'bridge_invalid');
    assert.equal(callbacks, 0);
  });

test('a successful-looking preparation status requires a clean process exit',
  { skip: process.platform === 'win32' }, async t => {
    const peer = await fakeBridge(t, `${emit({ type: 'library', operation: 'prepare_search', value: status() })}\nsys.exit(7)`);
    await assert.rejects(runLibrary(root, peer.libraryPath, { operation: 'prepare_search', class_id: classId }, {
      executable: peer.executable,
    }), error => error.code === 'bridge_failed');
  });

test('preparation cancellation reaps a SIGTERM-ignoring worker before releasing the caller',
  { skip: process.platform === 'win32' }, async t => {
    const peer = await fakeBridge(t, '');
    const marker = join(peer.folder, 'pid');
    await writeFile(peer.executable, `#!${pythonExecutable(root)}\nimport sys, os, signal, time\nsys.stdin.readline()\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwith open(${JSON.stringify(marker)}, 'w') as marker: marker.write(str(os.getpid()))\nwhile True: time.sleep(0.05)\n`, { mode: 0o700 });
    const controller = new AbortController();
    const pending = runLibrary(root, peer.libraryPath, { operation: 'prepare_search', class_id: classId }, {
      executable: peer.executable, signal: controller.signal, timeoutMs: 5000,
    });
    const rejected = assert.rejects(pending, error => error.code === 'cancelled');
    const pid = await readPid(marker);
    process.kill(pid, 0);
    controller.abort();
    await rejected;
    assertReaped(pid);
  });

test('preparation timeout also reaps an unresponsive worker and cannot report ready',
  { skip: process.platform === 'win32' }, async t => {
    const peer = await fakeBridge(t, '');
    const marker = join(peer.folder, 'pid');
    await writeFile(peer.executable, `#!${pythonExecutable(root)}\nimport sys, os, signal, time\nsys.stdin.readline()\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwith open(${JSON.stringify(marker)}, 'w') as marker: marker.write(str(os.getpid()))\nwhile True: time.sleep(0.05)\n`, { mode: 0o700 });
    const pending = runLibrary(root, peer.libraryPath, { operation: 'prepare_search', class_id: classId }, {
      executable: peer.executable, timeoutMs: 1500,
    });
    const rejected = assert.rejects(pending, error => error.code === 'timeout');
    const pid = await readPid(marker);
    await rejected;
    assertReaped(pid);
  });

test('explicit-page evidence cannot be mislabeled as automatically retrieved hybrid evidence',
  { skip: process.platform === 'win32' }, async t => {
    const peer = await fakeBridge(t, `${emit({ type: 'search_status', value: status() })}\n${emit(refusal)}`);
    let callbacks = 0;
    await assert.rejects(runLibrary(root, peer.libraryPath, {
      operation: 'ask', class_id: classId, document_id: '20000000-0000-4000-8000-000000000001',
      pages: [1], question: 'Synthetic?',
    }, { executable: peer.executable, onSearchStatus: () => { callbacks++; } }),
    error => error.code === 'bridge_invalid');
    assert.equal(callbacks, 0);
  });
