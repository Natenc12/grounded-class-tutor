import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { pythonExecutable, runBridge, stopBridges } from '../runtime.mjs';

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
