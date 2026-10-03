import { createHash } from 'node:crypto';
import { cp, lstat, mkdir, readFile, readdir, realpath, writeFile } from 'node:fs/promises';
import { isAbsolute, join, relative, sep } from 'node:path';

// Only these application-owned inputs can enter the distributable. Never copy
// a checkout recursively: it may contain accounts, libraries, or private PDFs.
export const APP_FILES = Object.freeze([
  'main.mjs', 'runtime.mjs', 'protocol.mjs', 'source-store.mjs', 'preload.cjs',
  'renderer/index.html', 'renderer/app.js', 'renderer/styles.css',
]);
export const PYTHON = Object.freeze({
  version: '3.13.14', release: '20260623',
  url: 'https://github.com/astral-sh/python-build-standalone/releases/download/20260623/cpython-3.13.14%2B20260623-aarch64-apple-darwin-pgo%2Blto-full.tar.zst',
  sha256: '3caf9b0084fdbedc6da1c96a53c020f4a0bce35aab15e63e0e0c62ae450b4d7b',
});
export function sha256(bytes) { return createHash('sha256').update(bytes).digest('hex'); }
export function isWithin(parent, child) {
  const name = relative(parent, child);
  return name === '' || (!isAbsolute(name) && name !== '..' && !name.startsWith(`..${sep}`));
}
export async function ensureBuildDirectory(parent, name) {
  if (!(await lstat(parent)).isDirectory() || !name || name.includes('/') || name === '..') {
    throw new Error('Build directories must be real descendants of the checkout.');
  }
  const path = join(parent, name);
  try {
    const information = await lstat(path);
    if (!information.isDirectory() || information.isSymbolicLink()) throw new Error(`Unsafe build directory: ${name}`);
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
    await mkdir(path);
  }
  return path;
}
export async function copyAppFiles(desktop, target) {
  for (const name of APP_FILES) {
    const source = join(desktop, name);
    if (!(await lstat(source)).isFile()) throw new Error(`Application input must be a regular file: ${name}`);
    await mkdir(join(target, name, '..'), { recursive: true });
    await cp(source, join(target, name));
  }
}
// Copy the selected, checksum-verified public assets only. An asset cache can
// contain other experiments or private files, so it is never copied as a tree.
export async function copyModelFiles(candidate, sourceRoot, targetRoot) {
  if (candidate.key !== 'minilm' || !(await lstat(sourceRoot)).isDirectory() ||
      !(await lstat(targetRoot)).isDirectory()) throw new Error('Invalid model bundle roots.');
  for (const file of candidate.files) {
    const parts = file.path.split('/');
    if (parts[0] !== 'minilm' || parts.length < 2 ||
        parts.some(part => !part || part === '.' || part === '..' || part.includes('\\'))) {
      throw new Error('Invalid model asset path.');
    }
    let source = sourceRoot, target = targetRoot;
    for (const part of parts.slice(0, -1)) {
      source = join(source, part);
      if (!(await lstat(source)).isDirectory()) throw new Error('Model input directory must be real.');
      target = await ensureBuildDirectory(target, part);
    }
    source = join(source, parts.at(-1)); target = join(target, parts.at(-1));
    const information = await lstat(source);
    if (!information.isFile()) throw new Error('Model input must be a regular file.');
    if (information.size !== file.bytes) throw new Error('Model asset checksum mismatch.');
    const bytes = await readFile(source);
    if (bytes.length !== file.bytes || sha256(bytes) !== file.sha256) throw new Error('Model asset checksum mismatch.');
    await writeFile(target, bytes, { flag: 'wx' });
  }
}
export async function copyDependencyNotices(sourceRoot, site, targetRoot) {
  if (!(await lstat(sourceRoot)).isDirectory() || !(await lstat(site)).isDirectory()) {
    throw new Error('Notice and wheel roots must be real directories.');
  }
  async function regular(path) {
    if (!(await lstat(path)).isFile()) throw new Error('Notice input must be a regular file.');
    return readFile(path);
  }
  const manifestBytes = await regular(join(sourceRoot, 'manifest.json'));
  const manifest = JSON.parse(manifestBytes.toString('utf8'));
  if (manifest.schema !== 1 || manifest.notice_file?.path !== 'THIRD_PARTY_NOTICES.txt') {
    throw new Error('Invalid supplemental notice manifest.');
  }
  for (const file of manifest.wheel_files) {
    const parts = file.path.split('/');
    if (parts.some(part => !part || part === '.' || part === '..' || part.includes('\\'))) {
      throw new Error('Invalid wheel notice identity path.');
    }
    let directory = site;
    for (const part of parts.slice(0, -1)) {
      directory = join(directory, part);
      if (!(await lstat(directory)).isDirectory()) throw new Error('Wheel notice directories must be real.');
    }
    const bytes = await regular(join(directory, parts.at(-1)));
    if (bytes.length !== file.bytes || sha256(bytes) !== file.sha256) {
      throw new Error('Installed wheel differs from audited notice inventory.');
    }
  }
  const notices = await regular(join(sourceRoot, manifest.notice_file.path));
  if (notices.length !== manifest.notice_file.bytes || sha256(notices) !== manifest.notice_file.sha256) {
    throw new Error('Supplemental notice checksum mismatch.');
  }
  const target = await ensureBuildDirectory(targetRoot, 'python-wheels');
  await writeFile(join(target, 'manifest.json'), manifestBytes, { flag: 'wx' });
  await writeFile(join(target, 'THIRD_PARTY_NOTICES.txt'), notices, { flag: 'wx' });
  return { manifestSha256: sha256(manifestBytes), noticesSha256: sha256(notices),
    wheelFiles: manifest.wheel_files, sources: manifest.sources.length };
}
export async function inventoryTree(root) {
  const canonicalRoot = await realpath(root);
  const output = [];
  async function visit(directory) {
    for (const entry of (await readdir(directory)).sort()) {
      const path = join(directory, entry), info = await lstat(path), name = relative(root, path);
      if (info.isSymbolicLink()) {
        if (!isWithin(canonicalRoot, await realpath(path))) throw new Error(`External bundle symlink: ${name}`);
        output.push({ path: name, symlink: true });
      } else if (info.isDirectory()) await visit(path);
      else if (info.isFile()) output.push({ path: name, size: info.size });
      else throw new Error(`Unsupported bundle entry: ${name}`);
    }
  }
  await visit(root);
  return output;
}
export async function auditPayload(root) {
  const files = await inventoryTree(root);
  for (const file of files) {
    const parts = file.path.split(sep);
    if (parts.some(part => /^\.env(?:\.|$)/.test(part) ||
      ['.git', '.venv', '.DS_Store', 'credentials.json', 'library.sqlite3', 'document-staging'].includes(part)) ||
      /\.(?:sqlite3?|db)(?:-wal|-shm)?$/i.test(file.path)) throw new Error(`Private/development file in payload: ${file.path}`);
  }
  return files;
}
export async function writeJSON(path, value) { await writeFile(path, `${JSON.stringify(value, null, 2)}\n`); }
export async function readJSON(path) { return JSON.parse(await readFile(path, 'utf8')); }
