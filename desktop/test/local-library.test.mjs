import test from 'node:test';
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { pythonEnvironment, pythonExecutable, runLibrary, stopBridges } from '../runtime.mjs';

const root = fileURLToPath(new URL('../../', import.meta.url));

test('real local service preserves a class, original and cited evidence across fresh processes', async t => {
  const folder = await mkdtemp(join(tmpdir(), 'gct-library-integration-'));
  t.after(async () => { await stopBridges(); await rm(folder, { recursive: true, force: true }); });
  const path = join(folder, 'library.sqlite3'), source = join(folder, 'Synthetic lecture.pdf');
  await promisify(execFile)(pythonExecutable(root), ['-c', `
import sys
from reportlab.pdfgen.canvas import Canvas
canvas = Canvas(sys.argv[1])
for text in ('Active recall retrieves information from memory.', 'Spaced practice spreads sessions over time.'):
    canvas.drawString(72, 720, text)
    canvas.showPage()
canvas.save()
`, source], { cwd: root, env: pythonEnvironment(root), timeout: 10000 });
  const call = (operation, fields = {}, options = {}) => runLibrary(root, path, { operation, ...fields }, options);
  const course = await call('create_class', { name: 'Synthetic course' });
  const other = await call('create_class', { name: 'Another course' });
  const document = await call('import_document', { class_id: course.id, file_path: source });
  await rm(source);
  // Every call launches a new Python process. Neither the original path nor a
  // previous connection is available to hide a failed publication.
  const reopened = await call('get_document', { class_id: course.id, document_id: document.id });
  assert.equal(reopened.preview.pages[1].text.trim(), 'Spaced practice spreads sessions over time.');
  assert.equal(reopened.document.sha256, document.sha256);
  await assert.rejects(call('get_document', { class_id: other.id, document_id: document.id }), error => error.code === 'library_error');

  let generations = 0;
  const result = await call('ask', { class_id: course.id, question: 'What is active recall?' }, {
    generate: async messages => {
      generations++;
      assert.match(messages[1].content, /Active recall retrieves information/);
      return 'Active recall retrieves information from memory [S1].\nCOVERAGE: complete';
    },
  });
  assert.equal(result.state, 'GROUNDED');
  const cited = await call('citation', { class_id: course.id, chunk_id: result.citations[0].chunk_id });
  assert.equal(cited.document_id, document.id);
  assert.equal(cited.page_or_slide, 1);
  assert.match(cited.text, /retrieves information/);
  const empty = await call('ask', { class_id: other.id, question: 'What is active recall?' }, {
    generate: async () => assert.fail('An empty class must not invoke generation'),
  });
  assert.equal(empty.state, 'REFUSAL');
  assert.equal(generations, 1);

  const selected = await call('ask', { class_id: course.id, document_id: document.id, pages: [2],
    question: 'How should I arrange my studying?' }, {
    generate: async messages => {
      assert.match(messages[1].content, /Spaced practice spreads sessions/);
      assert.doesNotMatch(messages[1].content, /Active recall retrieves/);
      return 'Spread the sessions over time [S1].\nCOVERAGE: complete';
    },
  });
  assert.equal(selected.citations[0].page_or_slide, 2);
  assert.equal((await call('citation', { class_id: course.id, chunk_id: selected.citations[0].chunk_id })).document_id, document.id);

  const backup = join(folder, 'backup.sqlite3');
  await call('backup', { output_path: backup });
  await assert.rejects(call('backup', { output_path: backup }), error => error.code === 'library_error');
  await call('delete_document', { class_id: course.id, document_id: document.id });
  assert.deepEqual(await call('list_documents', { class_id: course.id }), []);
  const restored = await runLibrary(root, backup, { operation: 'get_document', class_id: course.id, document_id: document.id });
  assert.equal(restored.document.sha256, document.sha256);
  assert.equal(restored.preview.pages[0].page_or_slide, 1);
});

test('a Python-admitted Unicode class remains readable through the desktop wire boundary', async t => {
  const folder = await mkdtemp(join(tmpdir(), 'gct-unicode-library-'));
  t.after(async () => { await stopBridges(); await rm(folder, { recursive: true, force: true }); });
  const path = join(folder, 'library.sqlite3'), name = '🎓'.repeat(200);
  const created = await runLibrary(root, path, { operation: 'create_class', name });
  assert.equal(created.name, name);
  const classes = await runLibrary(root, path, { operation: 'list_classes' });
  assert.deepEqual(classes, [created]);
  await assert.rejects(runLibrary(root, path, { operation: 'create_class', name: name + '🎓' }),
    error => error.code === 'library_error');
  assert.deepEqual(await runLibrary(root, path, { operation: 'list_classes' }), [created]);
});
