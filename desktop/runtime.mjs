import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { isAbsolute, join } from 'node:path';
import { characterCount, validDocument, validLibraryEvent, validResult, validSearchStatus } from './protocol.mjs';

const runningChildren = new Set();

export async function stopBridges() {
  const jobs = [...runningChildren];
  for (const job of jobs) job.stop();
  await Promise.all(jobs.map(job => job.closed));
}

export class ProofError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

export function pythonEnvironment(repoRoot) {
  // No inherited API keys, OAuth tokens, dotenv, or Python startup injection.
  return {
    PATH: process.env.PATH ?? '/usr/bin:/bin',
    ...(process.env.SYSTEMROOT ? { SYSTEMROOT: process.env.SYSTEMROOT } : {}),
    PYTHONPATH: join(repoRoot, 'src'),
    PYTHON_DOTENV_DISABLED: '1',
    PYTHONUNBUFFERED: '1',
    PYTHONNOUSERSITE: '1',
  };
}

export function pythonExecutable(repoRoot) {
  if (process.env.GCT_PYTHON) return process.env.GCT_PYTHON;
  const local = join(repoRoot, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  return existsSync(local) ? local : (process.platform === 'win32' ? 'python' : 'python3');
}

export function pythonLaunch(repoRoot, { packaged = false, resourcesPath, executable } = {}) {
  if (!packaged) return { executable: executable ?? pythonExecutable(repoRoot), args: [],
    cwd: repoRoot, env: pythonEnvironment(repoRoot) };
  if (typeof resourcesPath !== 'string' || !isAbsolute(resourcesPath)) {
    throw new ProofError('python_unavailable', 'The bundled document runtime is missing. Reinstall the app.');
  }
  const runtime = join(resourcesPath, 'python');
  const bundled = join(runtime, 'bin', 'python3.13');
  if (!existsSync(bundled)) {
    throw new ProofError('python_unavailable', 'The bundled document runtime is missing. Reinstall the app.');
  }
  // -I ignores Python environment variables and user/cwd imports; -B prevents
  // writes inside a read-only app bundle. Never use a developer override here.
  return { executable: bundled, args: ['-I', '-B'], cwd: runtime, env: { PATH: '/usr/bin:/bin' } };
}

export function responseOptions(messages, model, signal) {
  if (!Array.isArray(messages) || messages.length > 10) throw new ProofError('bridge_invalid', 'Invalid grounding request.');
  let instructions = '';
  const input = [];
  let length = 0;
  for (const message of messages) {
    if (!message || typeof message.content !== 'string') throw new ProofError('bridge_invalid', 'Invalid grounding request.');
    if (message.content.length > 120000) throw new ProofError('context_too_large', 'Select fewer pages.');
    length += characterCount(message.content);
    if (length > 60000) throw new ProofError('context_too_large', 'Select fewer pages.');
    if (message.role === 'system') instructions += `${message.content}\n`;
    else if (['user', 'assistant', 'developer'].includes(message.role)) input.push({ role: message.role, content: message.content });
    else throw new ProofError('bridge_invalid', 'Invalid grounding request.');
  }
  if (!instructions || !input.length) throw new ProofError('bridge_invalid', 'The grounding request is incomplete.');
  return { model, instructions, input, signal };
}

export function validateAsk(value, document, models, library) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).some(key => !['question', 'model', 'pages', 'scope'].includes(key))) {
    throw new ProofError('invalid_request', 'Check the question and selected pages.');
  }
  const { question, model, pages } = value;
  if (typeof question !== 'string' || !question.trim() || question.length > 4000 || characterCount(question) > 2000) {
    throw new ProofError('invalid_question', 'Enter a question of up to 2,000 characters.');
  }
  if (typeof model !== 'string' || !models.some(entry => entry.slug === model)) {
    throw new ProofError('invalid_model', 'Choose an available model for this account.');
  }
  const scope = value.scope ?? 'pages';
  if (!['class', 'document', 'pages'].includes(scope)) throw new ProofError('invalid_scope', 'Choose a class, document, or selected pages.');
  if (scope !== 'pages') {
    if (pages !== undefined || !library?.selectedClassId ||
        (scope === 'document' && (!library.selectedDocumentId || document?.sample))) {
      throw new ProofError('invalid_scope', 'Select a stored class or document first.');
    }
    return { question: question.trim(), model, scope };
  }
  if (!document || !Array.isArray(pages) || !pages.length || pages.length > 5 ||
      new Set(pages).size !== pages.length || pages.some(page => !Number.isInteger(page) ||
        !document.pages.some(entry => entry.page_or_slide === page))) {
    throw new ProofError('invalid_pages', 'Choose between one and five pages.');
  }
  return { question: question.trim(), model, pages, ...(value.scope === undefined ? {} : { scope }) };
}

export function runBridge(repoRoot, payload, { inspect = false, generate, signal,
  timeoutMs = inspect ? 30000 : 240000, executable, libraryPath, packaged = false, resourcesPath,
  modelRoot, onSearchStatus } = {}) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(new ProofError('cancelled', 'Request cancelled.')); return; }
    const command = libraryPath ? ['-m', 'gct.local.service', '--library', libraryPath] :
      ['-m', 'gct.local_proof', ...(inspect ? ['--inspect'] : [])];
    const launch = pythonLaunch(repoRoot, { executable, packaged, resourcesPath });
    if (libraryPath) {
      const assets = packaged ? join(resourcesPath, 'models', 'minilm')
        : modelRoot ?? join(repoRoot, 'desktop', 'build', 'models', 'minilm');
      if (typeof assets !== 'string' || !isAbsolute(assets)) {
        reject(new ProofError('invalid_model_path', 'The local search model location is invalid.')); return;
      }
      command.push('--model-root', assets);
    }
    const child = spawn(launch.executable, [...launch.args, ...command], {
      cwd: launch.cwd, env: launch.env, stdio: ['pipe', 'pipe', 'pipe'], shell: false,
    });
    let markClosed;
    const closed = new Promise(resolveClosed => { markClosed = resolveClosed; });
    const generation = new AbortController();
    let finished = false, buffered = '', outputBytes = 0, calls = 0, terminal, generating = false, searchStatusSeen = false;
    let chain = Promise.resolve();
    const stop = () => {
      generation.abort();
      child.stdin.destroy();
      child.kill('SIGTERM');
      const force = setTimeout(() => child.kill('SIGKILL'), 1000);
      force.unref();
      child.once('close', () => clearTimeout(force));
    };
    const job = { stop, closed };
    runningChildren.add(job);
    child.once('close', () => { runningChildren.delete(job); markClosed(); });
    const complete = (error, value) => {
      if (finished) return;
      finished = true;
      generation.abort();
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      // Callers may release an owned import directory immediately on rejection.
      // Keep it owned until the parser has actually exited, including SIGKILL.
      if (error) { stop(); closed.then(() => reject(error)); } else resolve(value);
    };
    const abort = () => complete(new ProofError('cancelled', 'Request cancelled.'));
    const timer = setTimeout(() => complete(new ProofError('timeout', 'The request took too long. Try fewer pages.')), timeoutMs);
    signal?.addEventListener('abort', abort, { once: true });
    child.on('error', () => complete(new ProofError('python_unavailable', packaged
      ? 'The bundled document runtime could not start. Reinstall the app.'
      : 'The local Python runtime could not start. Run uv sync --extra dev in this checkout.')));
    child.stdin.on('error', () => complete(new ProofError('bridge_failed', 'The local document process stopped.')));
    // Parser errors may contain local file paths or document text; do not forward/log stderr.
    child.stderr.on('data', () => {});
    const handle = async line => {
      if (finished || signal?.aborted) return;
      let event;
      try { event = JSON.parse(line); } catch { throw new ProofError('bridge_invalid', 'The local document response was invalid.'); }
      if (!event || typeof event !== 'object' || Array.isArray(event)) throw new ProofError('bridge_invalid', 'The local document response was invalid.');
      if (terminal) throw new ProofError('bridge_invalid', 'Unexpected data after the local result.');
      if (event.type === 'search_status') {
        if (inspect || !libraryPath || payload.operation !== 'ask' || payload.pages !== undefined || searchStatusSeen || calls ||
            Object.keys(event).length !== 2 || !validSearchStatus(event.value)) {
          throw new ProofError('bridge_invalid', 'The local search status was invalid.');
        }
        searchStatusSeen = true;
        onSearchStatus?.(event.value);
      } else if (event.type === 'generate') {
        if (inspect || (libraryPath && payload.operation !== 'ask') || !generate || ++calls > 2) throw new ProofError('bridge_invalid', 'Unexpected generation request.');
        if (libraryPath && payload.pages === undefined && !searchStatusSeen) throw new ProofError('bridge_invalid', 'The local search status is missing.');
        generating = true;
        let text;
        try { text = await generate(event.messages, generation.signal); }
        finally { generating = false; }
        if (finished || signal?.aborted) return;
        if (typeof text !== 'string' || text.length > 200000) throw new ProofError('response_too_large', 'The answer was too long. Try a smaller question.');
        child.stdin.write(`${JSON.stringify({ type: 'generated', text })}\n`);
      } else if (libraryPath && event.type === 'library' && validLibraryEvent(event, payload)) terminal = event;
      else if (!libraryPath && event.type === 'document' && inspect && validDocument(event)) terminal = event;
      else if (event.type === 'result' && !inspect && (!libraryPath || payload.operation === 'ask') && validResult(event.result)) {
        if (libraryPath && payload.pages === undefined && !searchStatusSeen) throw new ProofError('bridge_invalid', 'The local search status is missing.');
        terminal = event.result;
      }
      else if (event.type === 'error') {
        if (libraryPath) throw libraryFailure(event.code);
        throw new ProofError('document_error', 'This document or page selection could not be processed. Use a text-based PDF/PPTX, at most 10 MB and five pages.');
      }
      else throw new ProofError('bridge_invalid', 'The local document response was invalid.');
    };
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', buffer => {
      outputBytes += Buffer.byteLength(buffer);
      if (outputBytes > 2 * 1024 * 1024) { complete(new ProofError('bridge_limit', 'The local document response was too large.')); return; }
      buffered += buffer;
      let end;
      while ((end = buffered.indexOf('\n')) >= 0) {
        const line = buffered.slice(0, end); buffered = buffered.slice(end + 1);
        chain = chain.then(() => handle(line)).catch(error => complete(error));
      }
    });
    child.on('close', code => {
      // Do not wait on a provider promise once its local consumer has died.
      if (generating) complete(new ProofError('bridge_failed', 'The local document process stopped during generation.'));
      chain.then(() => {
        if (finished) return;
        if (signal?.aborted) abort();
        else if (code !== 0 || !terminal || buffered.trim()) complete(new ProofError('bridge_failed', 'The local document process did not finish successfully.'));
        else complete(null, terminal);
      }).catch(error => complete(error));
    });
    child.stdin.write(`${JSON.stringify(payload)}\n`);
  });
}

function libraryFailure(code) {
  const messages = {
    class_missing: 'This class is no longer in the library. Select another class.',
    document_missing: 'This document is no longer in the selected class.',
    citation_missing: 'This cited source is no longer available in the selected class.',
    invalid_name: 'Use a short, readable class or PDF/PPTX filename.',
    backup_failed: 'The backup could not finish. Choose a new filename; existing files are never overwritten.',
    newer_library: 'This library requires a newer GCT version. The library has been preserved.',
    invalid_library: 'The library needs recovery. Existing data has been preserved.',
    storage_unavailable: 'The local library could not be opened. Preserve it for recovery.',
    storage_error: 'The library action could not finish. Existing data has been preserved.',
    storage_unsafe: 'The library must use a private, regular file owned by this OS user.',
    empty_document: 'This document has no extractable text. Choose a text-based PDF or PPTX.',
    document_too_large: 'The extracted document exceeds the local library limits.',
    context_too_large: 'The selected evidence is too long. Choose fewer pages.',
    protected: 'Password-protected documents are not supported.',
    unparseable: 'This document could not be read. Choose a text-based PDF or PPTX.',
    unsupported: 'Choose a PDF or PPTX file.',
    file_too_large: 'Choose a PDF or PPTX of at most 10 MiB.',
    invalid_file: 'Choose a readable, regular PDF or PPTX file of at most 10 MiB.',
  };
  return new ProofError('library_error', messages[code] ?? 'The local library action could not complete. Check the selected class, document, and available disk space.');
}

export function runLibrary(repoRoot, libraryPath, payload, options = {}) {
  if (typeof libraryPath !== 'string' || !isAbsolute(libraryPath)) {
    return Promise.reject(new ProofError('invalid_library_path', 'The local library location is invalid.'));
  }
  return runBridge(repoRoot, payload, { timeoutMs: ['ask', 'prepare_search'].includes(payload.operation) ? 240000 : 30000,
    ...options, libraryPath, inspect: false }).then(value => payload.operation === 'ask' ? value : value.value);
}
