import { constants } from 'node:fs';
import { chmod, lstat, mkdir, mkdtemp, open, readdir, rm } from 'node:fs/promises';
import { basename, extname, join } from 'node:path';
import { ProofError } from './runtime.mjs';

const MAX_FILE_BYTES = 10 * 1024 * 1024;

export async function createSourceStore(directory) {
  // Called only after Electron owns its single-instance lock. Recover only
  // this app's temporary run folders, leaving unrelated entries untouched.
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const metadata = await lstat(directory);
  if (!metadata.isDirectory() || metadata.isSymbolicLink()) throw new ProofError('storage_unavailable', 'Local document storage could not be opened.');
  await chmod(directory, 0o700);
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (entry.isDirectory() && /^run-[A-Za-z0-9]{6}$/.test(entry.name)) {
      await rm(join(directory, entry.name), { recursive: true, force: true });
    }
  }
  const root = await mkdtemp(join(directory, 'run-'));
  const owned = new Map();
  const pending = new Set();
  let closed = false, closing;

  async function discard(source) {
    const folder = owned.get(source?.file_path);
    if (!folder) return;
    await rm(folder, { recursive: true, force: true });
    owned.delete(source.file_path);
  }

  async function copy(original) {
    let input, output, folder;
    try {
      if (!['.pdf', '.pptx'].includes(extname(original).toLowerCase())) throw new ProofError('invalid_file', 'Choose a PDF or PPTX of at most 10 MB.');
      // Nonblocking admission lets fstat reject FIFOs/devices without hanging
      // before the regular-file check (and before shutdown can cancel us).
      input = await open(original, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0) | (constants.O_NONBLOCK ?? 0));
      const info = await input.stat();
      if (!info.isFile() || info.size > MAX_FILE_BYTES) throw new ProofError('invalid_file', 'Choose a PDF or PPTX of at most 10 MB.');
      if (closed) throw new ProofError('cancelled', 'Document selection was cancelled.');
      folder = await mkdtemp(join(root, 'source-'));
      const target = join(folder, basename(original));
      output = await open(target, 'wx', 0o600);
      // Enforce the cap while copying too: the original can grow after stat().
      const buffer = Buffer.alloc(65536);
      let total = 0;
      for (;;) {
        if (closed) throw new ProofError('cancelled', 'Document selection was cancelled.');
        const { bytesRead } = await input.read(buffer, 0, buffer.length, null);
        if (!bytesRead) break;
        total += bytesRead;
        if (total > MAX_FILE_BYTES) throw new ProofError('invalid_file', 'Choose a PDF or PPTX of at most 10 MB.');
        await output.writeFile(buffer.subarray(0, bytesRead));
      }
      owned.set(target, folder);
      return { file_path: target };
    } catch (error) {
      await input?.close(); input = undefined;
      await output?.close(); output = undefined;
      if (folder) await rm(folder, { recursive: true, force: true });
      throw error;
    } finally {
      await input?.close();
      await output?.close();
    }
  }

  return {
    stage(original) {
      if (closed) return Promise.reject(new ProofError('cancelled', 'Document selection was cancelled.'));
      const operation = copy(original);
      pending.add(operation);
      operation.then(() => pending.delete(operation), () => pending.delete(operation));
      return operation;
    },
    discard,
    close() {
      if (!closing) {
        closed = true;
        closing = Promise.allSettled([...pending]).then(() => rm(root, { recursive: true, force: true }));
      }
      return closing;
    },
  };
}
