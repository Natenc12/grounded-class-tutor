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
