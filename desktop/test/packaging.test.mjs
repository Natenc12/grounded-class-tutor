import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtemp, mkdir, writeFile, readFile, rm, symlink, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { pythonEnvironment, pythonExecutable, pythonLaunch, runBridge } from '../runtime.mjs';
import { APP_FILES, auditPayload, copyAppFiles, copyDependencyNotices, copyModelFiles, ensureBuildDirectory, sha256 } from '../scripts/packaging-utils.mjs';

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
test('model bundle copies only verified selected files and preserves original bytes', async t => {
  const root = await temporary(t), source = join(root, 'cache'), target = join(root, 'bundle');
  await mkdir(join(source, 'minilm'), { recursive: true }); await mkdir(target);
  const bytes = Buffer.from('synthetic public model');
  await writeFile(join(source, 'minilm/model.onnx'), bytes);
  await writeFile(join(source, 'minilm/private.pdf'), 'private');
  await mkdir(join(source, 'bge')); await writeFile(join(source, 'bge/model.onnx'), 'other experiment');
  const candidate = { key: 'minilm', files: [{ path: 'minilm/model.onnx', bytes: bytes.length, sha256: sha256(bytes) }] };
  await copyModelFiles(candidate, source, target);
  assert.deepEqual((await auditPayload(target)).map(file => file.path), ['minilm/model.onnx']);
  assert.deepEqual(await readFile(join(target, 'minilm/model.onnx')), bytes);
  assert.deepEqual(await readFile(join(source, 'minilm/model.onnx')), bytes);
});
test('model bundle rejects corruption, links and traversal before publishing the model', async t => {
  const root = await temporary(t), source = join(root, 'cache'), target = join(root, 'bundle');
  await mkdir(join(source, 'minilm'), { recursive: true }); await mkdir(target);
  const bytes = Buffer.from('expected model'), path = join(source, 'minilm/model.onnx');
  const candidate = { key: 'minilm', files: [{ path: 'minilm/model.onnx', bytes: bytes.length, sha256: sha256(bytes) }] };
  await writeFile(path, 'damaged model');
  await assert.rejects(copyModelFiles(candidate, source, target), /checksum/);
  assert.deepEqual(await readdir(join(target, 'minilm')), []);
  await rm(path); await writeFile(join(source, 'outside'), bytes); await symlink('../outside', path);
  await assert.rejects(copyModelFiles(candidate, source, target), /regular/);
  await assert.rejects(copyModelFiles({ ...candidate, files: [{ ...candidate.files[0], path: '../escape' }] }, source, target), /path/);
  await rm(path); await rm(join(source, 'minilm'), { recursive: true }); await symlink('.', join(source, 'minilm'));
  await assert.rejects(copyModelFiles(candidate, source, target), /directory/);
});
test('notice staging binds audited text to the installed wheel and rejects changed dependencies', async t => {
  const root = await temporary(t), source = join(root, 'notices'), site = join(root, 'site'), target = join(root, 'licenses');
  await mkdir(source); await mkdir(join(site, 'example.dist-info'), { recursive: true }); await mkdir(target);
  const metadata = Buffer.from('Version: 1'), notices = Buffer.from('Original copyright and license');
  const manifest = { schema: 1, sources: [{ name: 'example' }],
    wheel_files: [{ path: 'example.dist-info/METADATA', bytes: metadata.length, sha256: sha256(metadata) }],
    notice_file: { path: 'THIRD_PARTY_NOTICES.txt', bytes: notices.length, sha256: sha256(notices) } };
  await writeFile(join(source, 'manifest.json'), JSON.stringify(manifest));
  await writeFile(join(source, 'THIRD_PARTY_NOTICES.txt'), notices);
  await writeFile(join(site, 'example.dist-info/METADATA'), metadata);
  const recorded = await copyDependencyNotices(source, site, target);
  assert.equal(recorded.noticesSha256, sha256(notices));
  assert.deepEqual(await readFile(join(target, 'python-wheels/THIRD_PARTY_NOTICES.txt')), notices);
  await writeFile(join(site, 'example.dist-info/METADATA'), 'Version: 2');
  await assert.rejects(copyDependencyNotices(source, site, target), /differs from audited/);
  assert.deepEqual(await readFile(join(target, 'python-wheels/THIRD_PARTY_NOTICES.txt')), notices);
});
test('notice staging refuses damaged or redirected texts before publishing', async t => {
  const root = await temporary(t), source = join(root, 'notices'), site = join(root, 'site'), target = join(root, 'licenses');
  await mkdir(source); await mkdir(site); await mkdir(target);
  const notices = Buffer.from('Original notice');
  await writeFile(join(source, 'manifest.json'), JSON.stringify({ schema: 1, sources: [], wheel_files: [],
    notice_file: { path: 'THIRD_PARTY_NOTICES.txt', bytes: notices.length, sha256: sha256(notices) } }));
  const input = join(source, 'THIRD_PARTY_NOTICES.txt');
  await writeFile(input, 'changed'); await assert.rejects(copyDependencyNotices(source, site, target), /checksum/);
  assert.deepEqual(await readdir(target), []);
  await rm(input); await writeFile(join(source, 'elsewhere'), notices); await symlink('elsewhere', input);
  await assert.rejects(copyDependencyNotices(source, site, target), /regular file/);
  assert.deepEqual(await readdir(target), []);
  await symlink(source, join(root, 'linked-source')); await symlink(site, join(root, 'linked-site'));
  await assert.rejects(copyDependencyNotices(join(root, 'linked-source'), site, target), /real directories/);
  await assert.rejects(copyDependencyNotices(source, join(root, 'linked-site'), target), /real directories/);
  assert.deepEqual(await readdir(target), []);
});
test('notice collector rejects unsafe cached files without network access', () => {
  const repo = join(dirname(fileURLToPath(import.meta.url)), '../..');
  execFileSync(pythonExecutable(repo), [join(repo, 'desktop/test/test_collect_python_notices.py')], {
    cwd: repo, env: pythonEnvironment(repo), timeout: 15000, stdio: 'pipe',
  });
});
