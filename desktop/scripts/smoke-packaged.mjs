// This harness never constructs an SDK client or reads real app data.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { constants } from 'node:fs';
import { cp, mkdtemp, mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { extractFile, listPackage } from '@electron/asar';
import { auditPayload, inventoryTree, readJSON, sha256 } from './packaging-utils.mjs';

const desktop = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const original = resolve(process.argv[2] ?? join(desktop, 'dist/Grounded Class Tutor-darwin-arm64/Grounded Class Tutor.app'));
const temporary = await mkdtemp(join(tmpdir(), 'gct-package-check-'));
try {
  const relocated = join(temporary, 'Relocated app é', 'Grounded Class Tutor.app');
  await cp(original, relocated, { recursive: true, verbatimSymlinks: true, mode: constants.COPYFILE_FICLONE });
  const resources = join(relocated, 'Contents/Resources'), python = join(resources, 'python');
  const executable = join(python, 'bin/python3.13'), archive = join(resources, 'app.asar');
  const models = join(resources, 'models/minilm');
  const provenance = await readJSON(join(models, 'provenance.json'));
  assert.equal(provenance.key, 'minilm');
  assert.equal(provenance.license, 'Apache-2.0');
  for (const file of provenance.files) {
    const bytes = await readFile(join(resources, 'models', file.path));
    assert.equal(bytes.length, file.bytes); assert.equal(sha256(bytes), file.sha256);
  }
  await auditPayload(relocated);
  const archiveFiles = listPackage(archive);
  assert.ok(archiveFiles.includes('/main.mjs'));
  assert.ok(!archiveFiles.some(path => /\/(?:test|tests|\.env|\.git|scripts|build)(?:\/|$)/.test(path)));
  assert.match(extractFile(archive, 'node_modules/@siwc/local/dist/responses.js').toString(), /GCT local modification/);
  assert.ok(extractFile(archive, 'node_modules/@siwc/local/LICENSE').length > 100);
  // Execute the actual shipped bridge outside asar using stock Node. Native
  // Electron launch is a separate check; this does not instantiate the SDK.
  const harness = join(temporary, 'harness'); await mkdir(harness);
  for (const file of ['runtime.mjs', 'protocol.mjs']) await writeFile(join(harness, file), extractFile(archive, file));
  const { runBridge, runLibrary } = await import(pathToFileURL(join(harness, 'runtime.mjs')));
  const work = join(temporary, 'synthetic library'); await mkdir(work, { mode: 0o700 });
  const probe = String.raw`
import sys,json,sqlite3,importlib.metadata
from pathlib import Path
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
from pptx import Presentation
from pptx.util import Inches
import gct,pypdf,pptx,lxml.etree,PIL,numpy,onnxruntime,tokenizers
root=Path(sys.prefix).resolve()
assert sys.flags.isolated and sys.dont_write_bytecode
assert all(Path(p).resolve().is_relative_to(root) for p in sys.path), sys.path
assert all(Path(m.__file__).resolve().is_relative_to(root) for m in (gct,pypdf,pptx,lxml.etree,PIL,numpy,onnxruntime,tokenizers))
db=sqlite3.connect(':memory:');db.execute('CREATE VIRTUAL TABLE t USING fts5(text)');db.execute("INSERT INTO t VALUES ('memory')");assert db.execute("SELECT count(*) FROM t WHERE t MATCH 'memory'").fetchone()[0]==1
folder=Path(sys.argv[1])
w=PdfWriter();p=w.add_blank_page(612,792)
font=w._add_object(DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')}))
p[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
s=DecodedStreamObject();s.set_data(b'BT /F1 12 Tf 72 720 Td (Active recall retrieves information from memory.) Tj ET');p[NameObject('/Contents')]=w._add_object(s)
w.write(folder/'Memory.pdf')
r=Presentation();slide=r.slides.add_slide(r.slide_layouts[6]);slide.shapes.add_textbox(Inches(1),Inches(1),Inches(8),Inches(2)).text='Spaced practice spreads study sessions over time.';r.save(folder/'Study.pptx')
print(json.dumps({'python':sys.version.split()[0],'sqlite':sqlite3.sqlite_version,'packages':{n:importlib.metadata.version(n) for n in ('pypdf','python-pptx','lxml','pillow','numpy','onnxruntime','tokenizers')}}))
`;
  const probeResult = execFileSync(executable, ['-I', '-B', '-c', probe, work], { cwd: temporary,
    env: { PATH: '/nonexistent', HOME: work, PYTHONHOME: '/nonexistent', PYTHONPATH: '/nonexistent',
      GCT_PYTHON: '/nonexistent', OPENAI_API_KEY: 'synthetic-must-not-be-used' }, encoding: 'utf8' });
  const options = { packaged: true, resourcesPath: resources, executable: '/nonexistent' };
  const absentCheckout = '/nonexistent-gct-checkout', database = join(work, 'library.sqlite3');
  const call = payload => runLibrary(absentCheckout, database, payload, options);
  const course = await call({ operation: 'create_class', name: 'Synthetic packaged smoke' });
  const saved = [];
  for (const file of ['Memory.pdf', 'Study.pptx']) {
    const document = await call({ operation: 'import_document', class_id: course.id, file_path: join(work, file) });
    saved.push(document); await rm(join(work, file));
    const reopened = await call({ operation: 'get_document', class_id: course.id, document_id: document.id });
    assert.ok(reopened.preview.pages[0].text.length > 10);
  }
  assert.equal((await call({ operation: 'list_classes' }))[0].id, course.id);
  assert.equal((await call({ operation: 'list_documents', class_id: course.id })).length, 2);
  assert.equal((await call({ operation: 'search_status', class_id: course.id })).state, 'missing');
  assert.equal((await call({ operation: 'prepare_search', class_id: course.id })).state, 'ready');
  // Each call is a new isolated process: this also proves cache reuse on restart.
  assert.equal((await call({ operation: 'search_status', class_id: course.id })).mode, 'hybrid');
  const automaticStatuses = [];
  const semantic = await runLibrary(absentCheckout, database, { operation: 'ask', class_id: course.id,
    document_id: saved[0].id, question: 'How do I test myself without looking at notes?' }, { ...options,
    onSearchStatus: status => automaticStatuses.push(status), generate: async messages => {
      assert.ok(messages.some(message => message.content.includes('Active recall retrieves information from memory.')));
      return 'Active recall retrieves information from memory. [S1]\nCOVERAGE: complete';
    } });
  assert.equal(semantic.state, 'GROUNDED');
  assert.deepEqual(automaticStatuses.map(status => status.mode), ['hybrid']);
  // Missing model assets cannot trigger a network fallback. Only keyword search
  // remains; a mocked generated reply keeps this smoke entirely free/offline.
  await rename(models, `${models}-hidden`);
  assert.equal((await call({ operation: 'search_status', class_id: course.id })).state, 'unavailable');
  const fallbackStatuses = [];
  const fallback = await runLibrary(absentCheckout, database, { operation: 'ask', class_id: course.id,
    question: 'What is active recall?' }, { ...options, onSearchStatus: status => fallbackStatuses.push(status),
    generate: async () => 'Recall retrieves information from memory. [S1]\nCOVERAGE: complete' });
  assert.equal(fallback.state, 'GROUNDED');
  assert.deepEqual(fallbackStatuses.map(status => status.mode), ['lexical']);
  await rename(`${models}-hidden`, models);
  const tokenizer = join(models, 'tokenizer.json'), originalTokenizer = await readFile(tokenizer);
  await writeFile(tokenizer, 'invalid synthetic tokenizer');
  assert.equal((await call({ operation: 'prepare_search', class_id: course.id })).state, 'unavailable');
  await writeFile(tokenizer, originalTokenizer);
  assert.equal((await call({ operation: 'search_status', class_id: course.id })).state, 'ready');
  const result = await runLibrary(absentCheckout, database, { operation: 'ask', class_id: course.id,
    document_id: saved[0].id, pages: [1], question: 'What is active recall?' }, { ...options,
    generate: async () => 'Active recall retrieves information from memory. [S1]\nCOVERAGE: complete' });
  assert.equal(result.state, 'GROUNDED');
  const citation = await call({ operation: 'citation', class_id: course.id, chunk_id: result.citations[0].chunk_id });
  assert.equal(citation.document_id, saved[0].id);
  const backup = join(work, 'backup.sqlite3'); await call({ operation: 'backup', output_path: backup });
  assert.equal((await runLibrary(absentCheckout, backup, { operation: 'list_classes' }, options))[0].id, course.id);
  assert.equal((await runLibrary(absentCheckout, backup, { operation: 'search_status', class_id: course.id }, options)).state, 'missing');
  await call({ operation: 'delete_document', class_id: course.id, document_id: saved[0].id });
  assert.equal((await call({ operation: 'search_status', class_id: course.id })).state, 'stale');
  await assert.rejects(call({ operation: 'citation', class_id: course.id, chunk_id: result.citations[0].chunk_id }));
  const sample = await runBridge(absentCheckout, { sample: true }, { ...options, inspect: true });
  assert.equal(sample.filename, 'local-proof-sample.pdf');
  const nativeFiles = (await inventoryTree(python)).filter(file => !file.symlink &&
    (file.path.endsWith('.so') || file.path.endsWith('.dylib') || file.path === 'bin/python3.13'));
  for (const file of nativeFiles) {
    const dependencies = execFileSync('/usr/bin/otool', ['-L', join(python, file.path)], { encoding: 'utf8' });
    // For dylibs, otool -L lists LC_ID_DYLIB before load dependencies. This
    // identity (including upstream wheel build names) is not a file to load.
    const identities = execFileSync('/usr/bin/otool', ['-D', join(python, file.path)], { encoding: 'utf8' })
      .split('\n').slice(1).map(line => line.trim()).filter(Boolean);
    for (const line of dependencies.split('\n').slice(1)) {
      if (!/^\s/.test(line) || line.trim().endsWith(':')) continue;
      const dependency = line.trim().split(' (')[0];
      if (!dependency || dependency.endsWith(':')) continue;
      if (identities.includes(dependency)) continue;
      assert.ok(dependency.startsWith('@') || dependency.startsWith('/usr/lib/') ||
        dependency.startsWith('/System/Library/'), `External native dependency: ${file.path}: ${dependency}`);
    }
  }
  assert.ok(!(await inventoryTree(python)).some(file => file.path.endsWith('.pyc')), 'Runtime wrote bytecode into the app');
  console.log(`Packaged smoke passed: relocated path, isolated Python, PDF/PPTX import, semantic preparation/restart/paraphrase, missing/corrupt-model lexical fallback, stale cache, mocked grounded handshake, citation/deletion, canonical-only backup, sample, ${nativeFiles.length} native dependency checks. ${probeResult.trim()}`);
} finally { await rm(temporary, { recursive: true, force: true }); }
