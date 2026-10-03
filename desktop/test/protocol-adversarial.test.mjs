import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { pythonExecutable, runBridge, runLibrary, stopBridges } from '../runtime.mjs';

import { validLibraryEvent } from '../protocol.mjs';

const root = fileURLToPath(new URL('../../', import.meta.url));

async function fakeBridge(t, program) {
  const folder = await mkdtemp(join(tmpdir(), 'gct-protocol-test-'));
  t.after(async () => { await stopBridges(); await rm(folder, { recursive: true, force: true }); });
  const executable = join(folder, 'bridge');
  await writeFile(executable, `#!${pythonExecutable(root)}\nimport sys, json\nsys.stdin.readline()\n${program}\n`, { mode: 0o700 });
  return executable;
}

for (const event of [
  { type: 'document', filename: 'sample.pdf', page_count: 1, pages: 'invalid' },
  { type: 'document', filename: 'sample.pdf', page_count: 1, pages: [{ page_or_slide: 2, text: 'wrong page', truncated: false }], synthetic: false, truncated: false },
  { type: 'result', result: { state: 'GROUNDED', answer_prose: 'Unvalidated.' } },
  { type: 'result', result: ['GROUNDED'] },
  { type: 'result', result: { state: 'GROUNDED', answer_prose: null, citations: [], coverage: { complete: false, gaps: ['Unsupported'] }, integrity: { ok: false, reasons: ['Invalid'] }, error: { kind: 'provider_terminal', message: 'Failed' } } },
]) {
  test(`malformed ${event.type} is rejected before reaching the application: ${JSON.stringify(event)}`, { skip: process.platform === 'win32' }, async t => {
    const executable = await fakeBridge(t, `print(${JSON.stringify(JSON.stringify(event))}, flush=True)`);
    await assert.rejects(runBridge(root, {}, { executable, inspect: event.type === 'document', timeoutMs: 2000 }),
      error => error.code === 'bridge_invalid');
  });
}

test('a parser exit during pending generation fails immediately and cancels the request', { skip: process.platform === 'win32' }, async t => {
  const executable = await fakeBridge(t, `print(json.dumps({'type':'generate','messages':[{'role':'system','content':'Rules'},{'role':'user','content':'Source'}]}), flush=True)`);
  let generationSignal;
  await assert.rejects(runBridge(root, {}, {
    executable, timeoutMs: 800,
    generate: async (_messages, signal) => { generationSignal = signal; return new Promise(() => {}); },
  }), error => error.code === 'bridge_failed');
  assert.equal(generationSignal?.aborted, true);
});

for (const [state, generated] of [
  ['GROUNDED', 'Active recall retrieves information from memory. [S1]\nCOVERAGE: complete'],
  ['PARTIAL', 'Active recall retrieves information from memory. [S1]\nCOVERAGE: gaps: ideal spacing interval'],
  ['REFUSAL', 'COVERAGE: complete'],
  ['INTEGRITY_FLAGGED', 'Invalid label. [S999]\nCOVERAGE: complete'],
  ['ERROR', 'x'.repeat(50000)],
]) {
  test(`the boundary accepts the actual Grounder ${state} contract`, async () => {
    const result = await runBridge(root, { sample: true, question: 'Explain the study method.', pages: [1] }, {
      generate: async () => generated,
    });
    assert.equal(result.state, state);
  });
}

test('timeout cancels an in-flight generation even without an external controller', { skip: process.platform === 'win32' }, async t => {
  const executable = await fakeBridge(t, `print(json.dumps({'type':'generate','messages':[]}), flush=True)\nsys.stdin.readline()`);
  let generationSignal;
  await assert.rejects(runBridge(root, {}, {
    executable, timeoutMs: 2000,
    generate: async (_messages, signal) => { generationSignal = signal; return new Promise(() => {}); },
  }), error => error.code === 'timeout');
  assert.equal(generationSignal?.aborted, true);
});


const classId = '10000000-0000-4000-8000-000000000001';
const documentId = '20000000-0000-4000-8000-000000000001';
const classRow = { id: classId, name: 'Synthetic class', created_at: '2026-10-02T12:00:00Z' };
const documentRow = { id: documentId, class_id: classId, filename: 'Synthetic.pdf', sha256: 'a'.repeat(64),
  byte_count: 12, page_count: 1, status: 'ready', created_at: '2026-10-02T12:00:00Z' };

test('library boundary rejects cross-class, duplicate, and unexpected metadata', () => {
  const request = { operation: 'list_documents', class_id: classId };
  const event = value => ({ type: 'library', operation: request.operation, value });
  assert.equal(validLibraryEvent(event([documentRow]), request), true);
  for (const value of [null, [documentRow, documentRow], [{ ...documentRow, class_id: documentId }],
    [{ ...documentRow, file_path: '/private/path' }], [{ ...documentRow, byte_count: 20 * 1024 * 1024 }],
    [{ ...documentRow, filename: '../private.pdf' }], [{ ...documentRow, page_count: true }]]) {
    assert.equal(validLibraryEvent(event(value), request), false);
  }
  assert.equal(validLibraryEvent({ type: 'library', operation: 'create_class', value: classRow }, { operation: 'list_classes' }), false);
});

test('stored preview must describe the requested document and exactly match source metadata', () => {
  const request = { operation: 'get_document', class_id: classId, document_id: documentId };
  const preview = { type: 'document', filename: 'Synthetic.pdf', page_count: 1, truncated: false, synthetic: false,
    pages: [{ page_or_slide: 1, text: 'Synthetic source.', truncated: false }] };
  const event = (document, candidate) => ({ type: 'library', operation: request.operation, value: { document, preview: candidate } });
  assert.equal(validLibraryEvent(event(documentRow, preview), request), true);
  assert.equal(validLibraryEvent(event({ ...documentRow, id: classId }, preview), request), false);
  assert.equal(validLibraryEvent(event(documentRow, { ...preview, filename: 'Different.pdf' }), request), false);
  assert.equal(validLibraryEvent(event(documentRow, { ...preview, synthetic: true }), request), false);
});

test('CRUD service cannot request generation and library errors do not echo raw messages', { skip: process.platform === 'win32' }, async t => {
  const executable = await fakeBridge(t, `print(json.dumps({'type':'generate','messages':[]}), flush=True)\nsys.stdin.readline()`);
  await assert.rejects(runLibrary(root, '/synthetic/library.sqlite3', { operation: 'list_classes' }, {
    executable, timeoutMs: 2000, generate: async () => assert.fail('CRUD cannot generate'),
  }), error => error.code === 'bridge_invalid');
  const failed = await fakeBridge(t, `print(json.dumps({'type':'error','code':'storage_error','message':'SECRET /private/path token'}), flush=True)\nsys.exit(1)`);
  await assert.rejects(runLibrary(root, '/synthetic/library.sqlite3', { operation: 'list_classes' }, { executable: failed }),
    error => error.code === 'library_error' && !error.message.includes('SECRET') && !error.message.includes('/private'));
});

test('a valid library payload still requires a clean successful process exit', { skip: process.platform === 'win32' }, async t => {
  const event = { type: 'library', operation: 'list_classes', value: [classRow] };
  const executable = await fakeBridge(t, `print(${JSON.stringify(JSON.stringify(event))}, flush=True)\nsys.exit(1)`);
  await assert.rejects(runLibrary(root, '/synthetic/library.sqlite3', { operation: 'list_classes' }, { executable }),
    error => error.code === 'bridge_failed');
});

test('citation text uses the same Unicode character ceiling as the Python library', () => {
  const event = text => ({ type: 'library', operation: 'citation', value: {
    document_id: documentId, filename: 'Synthetic.pdf', page_or_slide: 1, text,
  } });
  assert.equal(validLibraryEvent(event('🎓'.repeat(30000)), { operation: 'citation' }), true);
  assert.equal(validLibraryEvent(event('🎓'.repeat(30001)), { operation: 'citation' }), false);
});
