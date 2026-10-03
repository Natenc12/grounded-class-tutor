import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm, symlink, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pythonLaunch, runBridge } from '../runtime.mjs';
import { APP_FILES, auditPayload, copyAppFiles, ensureBuildDirectory } from '../scripts/packaging-utils.mjs';

async function temporary(t) {
  const path = await mkdtemp(join(tmpdir(), 'gct-package-test-'));
  t.after(() => rm(path, { recursive: true, force: true })); return path;
}
test('packaged launch uses bundled Python only, with isolated imports and no inherited environment', async t => {
  const resources = await temporary(t), bin = join(resources, 'python/bin'); await mkdir(bin, { recursive: true });
  await writeFile(join(bin, 'python3.13'), 'synthetic executable');
  const launch = pythonLaunch('/missing-checkout', { packaged: true, resourcesPath: resources, executable: '/override' });
  assert.equal(launch.executable, join(bin, 'python3.13'));
  assert.deepEqual(launch.args, ['-I', '-B']);
  assert.equal(launch.cwd, join(resources, 'python'));
  assert.deepEqual(launch.env, { PATH: '/usr/bin:/bin' });
});
test('missing bundled runtime fails closed even when a developer executable is supplied', async t => {
  const resources = await temporary(t);
  assert.throws(() => pythonLaunch('/repo', { packaged: true, resourcesPath: 'relative' }), /bundled/);
  await assert.rejects(runBridge('/repo', {}, { packaged: true, resourcesPath: resources, executable: process.execPath }), /bundled/);
  assert.equal(pythonLaunch('/repo', { executable: '/explicit-dev-python' }).executable, '/explicit-dev-python');
});
test('application staging admits only regular explicitly listed files', async t => {
  const root = await temporary(t), source = join(root, 'source'), stage = join(root, 'stage');
  for (const file of APP_FILES) { await mkdir(join(source, file, '..'), { recursive: true }); await writeFile(join(source, file), file); }
  await writeFile(join(source, '.env'), 'synthetic secret'); await writeFile(join(source, 'library.sqlite3'), 'synthetic private');
  await copyAppFiles(source, stage);
  assert.deepEqual((await auditPayload(stage)).map(file => file.path).sort(), [...APP_FILES].sort());
  await rm(join(source, 'main.mjs')); await symlink(join(source, '.env'), join(source, 'main.mjs'));
  await assert.rejects(copyAppFiles(source, stage), /regular file/);
});
test('payload audit rejects private files and symlinks escaping the bundle', async t => {
  const root = await temporary(t), bundle = join(root, 'bundle'); await mkdir(bundle);
  await writeFile(join(root, 'outside'), 'outside'); await symlink('../outside', join(bundle, 'escape'));
  await assert.rejects(auditPayload(bundle), /External bundle symlink/); await rm(join(bundle, 'escape'));
  for (const file of ['.env', 'library.sqlite3', 'credentials.json']) {
    await writeFile(join(bundle, file), 'synthetic'); await assert.rejects(auditPayload(bundle), /Private/); await rm(join(bundle, file));
  }
  assert.deepEqual(await readdir(bundle), []);
});
test('payload audit accepts relative internal links through macOS temporary path aliases', async t => {
  const root = await temporary(t); await writeFile(join(root, 'real'), 'synthetic');
  await symlink('real', join(root, 'internal'));
  assert.equal((await auditPayload(root)).find(file => file.path === 'internal').symlink, true);
});
test('build cleanup refuses redirected directories without touching their contents', async t => {
  const root = await temporary(t), outside = join(root, 'outside'); await mkdir(outside);
  await writeFile(join(outside, 'preserve'), 'synthetic'); await symlink(outside, join(root, 'build'));
  await assert.rejects(ensureBuildDirectory(root, 'build'), /Unsafe/);
  assert.deepEqual(await readdir(outside), ['preserve']);
  assert.equal(await ensureBuildDirectory(root, 'real-build'), join(root, 'real-build'));
});
