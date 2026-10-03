import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, readFile, readdir, mkdir, rm, stat, symlink } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createSourceStore } from '../source-store.mjs';

const runFile = promisify(execFile);

async function fixture(t) {
  const folder = await mkdtemp(join(tmpdir(), 'gct-source-test-'));
  const directory = join(folder, 'staging');
  const original = join(folder, 'source.pdf');
  await writeFile(original, 'original course material');
  t.after(() => rm(folder, { recursive: true, force: true }));
  return { folder, directory, original };
}

test('staging isolates the selected bytes and discard never removes an original', async t => {
  const { directory, original } = await fixture(t);
  const store = await createSourceStore(directory);
  t.after(() => store.close());
  const staged = await store.stage(original);
  await writeFile(original, 'changed later');
  assert.equal(await readFile(staged.file_path, 'utf8'), 'original course material');
  if (process.platform !== 'win32') assert.equal((await stat(staged.file_path)).mode & 0o777, 0o600);
  await store.discard({ file_path: original });
  assert.equal(await readFile(original, 'utf8'), 'changed later');
  await store.discard(staged);
  await assert.rejects(stat(staged.file_path), { code: 'ENOENT' });
  await store.close();
  assert.deepEqual(await readdir(directory), []);
});

test('oversized, symlinked, and missing sources leave no candidate copies', async t => {
  const { folder, directory, original } = await fixture(t);
  const store = await createSourceStore(directory);
  t.after(() => store.close());
  const large = join(folder, 'large.pdf');
  await writeFile(large, Buffer.alloc(10 * 1024 * 1024 + 1));
  await assert.rejects(store.stage(large), error => error.code === 'invalid_file');
  await assert.rejects(store.stage(join(folder, 'missing.pdf')));
  if (process.platform !== 'win32') {
    const link = join(folder, 'linked.pdf');
    await symlink(original, link);
    await assert.rejects(store.stage(link));
  }
  const [run] = await readdir(directory);
  assert.deepEqual(await readdir(join(directory, run)), []);
});

test('startup recovers only owned run folders and leaves unrelated entries and symlink targets alone', async t => {
  const { folder, directory } = await fixture(t);
  await mkdir(join(directory, 'run-ABC123'), { recursive: true });
  await writeFile(join(directory, 'run-ABC123', 'abandoned.pdf'), 'private');
  await mkdir(join(directory, 'keep-this'));
  await writeFile(join(directory, 'keep.txt'), 'keep');
  const outside = join(folder, 'outside');
  await mkdir(outside);
  await writeFile(join(outside, 'keep.pdf'), 'keep');
  if (process.platform !== 'win32') await symlink(outside, join(directory, 'run-DEF456'));
  const store = await createSourceStore(directory);
  await store.close();
  await assert.rejects(stat(join(directory, 'run-ABC123')), { code: 'ENOENT' });
  assert.equal(await readFile(join(outside, 'keep.pdf'), 'utf8'), 'keep');
  assert.equal(await readFile(join(directory, 'keep.txt'), 'utf8'), 'keep');
  assert.ok((await stat(join(directory, 'keep-this'))).isDirectory());
});

test('shutdown waits for pending copies, rejects new work, and removes the run directory', async t => {
  const { directory, original } = await fixture(t);
  const store = await createSourceStore(directory);
  const pending = store.stage(original);
  const observed = pending.catch(error => error);
  await store.close();
  assert.equal((await observed).code, 'cancelled');
  await assert.rejects(store.stage(original), error => error.code === 'cancelled');
  assert.deepEqual(await readdir(directory), []);
});

test('a FIFO masquerading as a PDF is rejected and cannot stall shutdown', { skip: process.platform === 'win32' }, async t => {
  const { folder, directory } = await fixture(t);
  const fifo = join(folder, 'named-pipe.pdf');
  await runFile('mkfifo', [fifo], { timeout: 3000, killSignal: 'SIGKILL' });
  // Keep a regression inside a reaped subprocess: a blocking filesystem open
  // can prevent even process.exit() from completing on the test runner itself.
  const script = `
    import assert from 'node:assert/strict';
    import { createSourceStore } from ${JSON.stringify(new URL('../source-store.mjs', import.meta.url).href)};
    const store = await createSourceStore(process.argv[1]);
    try {
      await assert.rejects(store.stage(process.argv[2]), error => error.code === 'invalid_file');
    } finally { await store.close(); }
    console.log('rejected-and-closed');
  `;
  const { stdout } = await runFile(process.execPath,
    ['--input-type=module', '-e', script, directory, fifo],
    { timeout: 3000, killSignal: 'SIGKILL', maxBuffer: 16384 });
  assert.equal(stdout.trim(), 'rejected-and-closed');
  assert.deepEqual(await readdir(directory), []);
});
