import { app, BrowserWindow, dialog, ipcMain, safeStorage, shell } from 'electron';
import { createChatGPT, CHATGPT_USAGE_URL } from '@siwc/local';
import { mkdir } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { ProofError, responseOptions, runBridge, runLibrary, stopBridges, validateAsk } from './runtime.mjs';
import { createSourceStore } from './source-store.mjs';

const directory = dirname(fileURLToPath(import.meta.url));
const repoRoot = dirname(directory);
const runtimeOptions = app.isPackaged ? { packaged: true, resourcesPath: process.resourcesPath } : {};
const rendererURL = pathToFileURL(join(directory, 'renderer/index.html')).href;
app.setName('Grounded Class Tutor');
app.setPath('userData', join(app.getPath('appData'), 'Grounded Class Tutor Local Proof'));
if (!app.requestSingleInstanceLock()) { app.quit(); process.exit(0); }

let window, chatgpt, sourceStore, source, active, closing = false;
let modelRevision = 0, answerSource;
const sdkOperations = new Set();
const appLifetime = new AbortController();
const state = { session: { status: 'disconnected', sharing: false }, models: [], busy: null,
  library: { classes: [], selectedClassId: null, documents: [], selectedDocumentId: null } };
const snapshot = () => structuredClone(state);
const publish = () => {
  if (window && !window.isDestroyed()) window.webContents.send('gct:state', snapshot());
};

const messages = {
  cancelled: 'Cancelled. No incomplete answer has been accepted.',
  subscription_sharing_user_not_eligible: 'This ChatGPT account is not eligible for plan sharing. Check the account and workspace you selected.',
  subscription_sharing_usage_limit_exceeded: 'Your ChatGPT plan or app limit was reached. Open Manage usage. GCT will not switch to an API key.',
  subscription_sharing_usage_unavailable: 'ChatGPT could not check your allowance. Try again later.',
  subscription_sharing_unsupported_capability: 'The selected model does not support this request through plan sharing.',
  subscription_sharing_route_not_supported: 'This request is unavailable through ChatGPT plan sharing.',
  sharing_not_enabled: 'Enable ChatGPT plan usage for GCT before asking a question.',
  sign_in_required: 'Continue with ChatGPT to connect your account.',
  reauth_required: 'Your ChatGPT connection needs to be renewed. Sign in again.',
  access_denied: 'Sign-in was not completed. You can try again when ready.',
  storage_encryption_unavailable: 'Unlock your operating system credential storage and try again. Saved credentials have been preserved.',
  storage_decryption_failed: 'The saved connection could not be decrypted. Restore access to your operating system credential storage and try again. Saved credentials have been preserved.',
  storage_provider_mismatch: 'The saved connection needs the credential storage provider that created it. Saved credentials have been preserved.',
  storage_encrypted_invalid: 'The encrypted connection file could not be read. Restore the saved connection file before signing in. Existing credentials have been preserved.',
  storage_invalid: 'The saved connection could not be read. Check its storage permissions or restore the connection file. Existing credentials have been preserved.',
  storage_unsafe: 'The saved connection must use a private, owner-only storage location. Check its storage permissions. Existing credentials have been preserved.',
  storage_error: 'The connection could not be saved securely. Check the app storage permissions and try again.',
  storage_busy: 'Another process is updating this ChatGPT connection. Try again shortly.',
  storage_lock_lost: 'Credential storage was interrupted. Close other copies of GCT and reconnect.',
  identity_verification_unavailable: 'ChatGPT identity verification is temporarily unavailable. Your connection has been preserved. Try again shortly.',
  revocation_failed: 'Signed out locally, but remote disconnection could not be confirmed. Disconnect GCT in ChatGPT Settings to finish revoking access.',
  stream_interrupted: 'The connection ended before the answer completed. No incomplete answer has been accepted.',
  response_incomplete: 'The answer did not finish. No incomplete answer has been accepted.',
  invalid_stream: 'The answer could not be verified as complete. Try again. No incomplete answer has been accepted.',
};
function safeMessage(error) {
  if (error instanceof ProofError) return error.message;
  if (error?.name === 'AbortError') return messages.cancelled;
  return messages[error?.code] ?? 'This action could not complete. Try again, or check the ChatGPT connection and usage settings.';
}
function clearAnswer() { answerSource = undefined; delete state.result; delete state.citation; delete state.notice; }
function cancelCurrent() { active?.abort(); }
function runSDK(operation) {
  if (closing) return Promise.reject(new ProofError('cancelled', 'GCT is closing.'));
  const pending = (async () => operation())();
  sdkOperations.add(pending);
  pending.then(() => sdkOperations.delete(pending), () => sdkOperations.delete(pending));
  return pending;
}
function setSession(value) {
  if (closing) return;
  const changed = state.session.profileId !== value.profileId;
  state.session = {
    status: value.status, sharing: value.sharing === true,
    ...(value.profileId ? { profileId: value.profileId } : {}),
    ...(value.profileLabel ? { profileLabel: value.profileLabel } : {}),
    ...(value.identity ? { identity: { name: value.identity.name, email: value.identity.email } } : {}),
  };
  if (value.error) state.notice = safeMessage(value.error);
  if (changed || value.status !== 'connected' || !value.sharing) {
    modelRevision++;
    if (state.busy === 'ask') cancelCurrent();
    state.models = [];
    answerSource = undefined;
    delete state.result;
    delete state.citation;
  }
  publish();
}
function assertIdle() {
  if (closing) throw new ProofError('cancelled', 'GCT is closing.');
  if (state.busy) throw new ProofError('busy', 'Finish or cancel the current action first.');
}
async function refreshModels() {
  const revision = ++modelRevision;
  state.models = [];
  if (closing || state.session.status !== 'connected' || !state.session.sharing) { publish(); return; }
  const profile = state.session.profileId;
  const models = await runSDK(() => chatgpt.listModels({ signal: appLifetime.signal }));
  if (closing || revision !== modelRevision || state.session.profileId !== profile ||
      state.session.status !== 'connected' || !state.session.sharing) return;
  state.models = models.map(model => ({ slug: model.slug, display_name: model.displayName }));
  publish();
}
async function connect() {
  assertIdle();
  clearAnswer();
  state.busy = 'signin'; publish();
  try {
    // Pressing Continue with ChatGPT is the user's explicit consent to start OAuth.
    const session = await runSDK(() => chatgpt.signIn({ signal: appLifetime.signal,
      reconsent: state.session.status === 'connected' && !state.session.sharing }));
    if (closing) return;
    setSession(session);
    await refreshModels();
    state.notice = session.sharing
      ? 'Connected. Requests use your ChatGPT plan. In Manage usage, leave credits disabled if you want no additional spending.'
      : 'Signed in. Enable ChatGPT plan usage to ask a question.';
  } finally { state.busy = null; publish(); }
}
function libraryCall(payload, signal) {
  return runLibrary(repoRoot, join(app.getPath('userData'), 'library.sqlite3'), payload, { ...runtimeOptions, signal });
}
function assertActive(controller) {
  if (controller.signal.aborted || closing || active !== controller) throw new ProofError('cancelled', messages.cancelled);
}
function actionObject(value, fields) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).length !== fields.length || fields.some(field => typeof value[field] !== 'string') ||
      Object.keys(value).some(field => !fields.includes(field))) {
    throw new ProofError('invalid_request', 'Check the selected class or document.');
  }
  return value;
}
async function localAction(kind, operation, { clear = true } = {}) {
  assertIdle();
  const controller = new AbortController();
  active = controller;
  state.busy = kind;
  if (clear) clearAnswer();
  publish();
  try { await operation(controller); }
  finally {
    controller.abort();
    if (active === controller) active = undefined;
    state.busy = null; publish();
  }
}
async function reloadLibrary(controller, preferred = state.library.selectedClassId, preserveSource = false) {
  const classes = await libraryCall({ operation: 'list_classes' }, controller.signal);
  assertActive(controller);
  const classId = classes.some(entry => entry.id === preferred) ? preferred : (classes[0]?.id ?? null);
  const documents = classId ? await libraryCall({ operation: 'list_documents', class_id: classId }, controller.signal) : [];
  assertActive(controller);
  const previous = state.library;
  const selectedDocumentId = previous.selectedClassId === classId && documents.some(entry => entry.id === previous.selectedDocumentId)
    ? previous.selectedDocumentId : null;
  state.library = { classes, selectedClassId: classId, documents, selectedDocumentId };
  if (!preserveSource && source && !source.sample && (!selectedDocumentId || source.document_id !== selectedDocumentId)) {
    source = undefined; delete state.document;
  }
}
async function loadStoredDocument(controller, classId, documentId) {
  const value = await libraryCall({ operation: 'get_document', class_id: classId, document_id: documentId }, controller.signal);
  assertActive(controller);
  const previous = source;
  source = { class_id: classId, document_id: documentId };
  state.library.selectedDocumentId = documentId;
  state.document = { id: documentId, filename: value.preview.filename, page_count: value.preview.page_count,
    pages: value.preview.pages, sample: false };
  await sourceStore.discard(previous);
}
async function createClass(value) {
  const { name } = actionObject(value, ['name']);
  await localAction('library', async controller => {
    const created = await libraryCall({ operation: 'create_class', name }, controller.signal);
    assertActive(controller);
    await reloadLibrary(controller, created.id);
    source = undefined; delete state.document;
  });
}
async function selectClass(value) {
  const { classId } = actionObject(value, ['classId']);
  if (!state.library.classes.some(entry => entry.id === classId)) throw new ProofError('invalid_class', 'Choose an existing class.');
  await localAction('library', async controller => {
    await reloadLibrary(controller, classId);
    source = undefined; delete state.document;
    state.library.selectedDocumentId = null;
  });
}
async function selectStoredDocument(value) {
  const { documentId } = actionObject(value, ['documentId']);
  const classId = state.library.selectedClassId;
  if (!classId || !state.library.documents.some(entry => entry.id === documentId)) throw new ProofError('invalid_document', 'Choose a document in the selected class.');
  await localAction('document', controller => loadStoredDocument(controller, classId, documentId));
}
async function selectDocument(sample) {
  if (!sample && !state.library.selectedClassId) throw new ProofError('missing_class', 'Create or select a class before importing course material.');
  await localAction('document', async controller => {
    let selected;
    try {
      if (sample) {
        const document = await runBridge(repoRoot, { sample: true }, { ...runtimeOptions, inspect: true, signal: controller.signal });
        assertActive(controller);
        const previous = source;
        source = { sample: true };
        state.library.selectedDocumentId = null;
        state.document = { filename: document.filename, page_count: document.page_count, pages: document.pages,
          sample: true, ...(document.suggested_question ? { suggested_question: document.suggested_question } : {}) };
        await sourceStore.discard(previous);
      } else {
        const classId = state.library.selectedClassId;
        const choice = await dialog.showOpenDialog(window, {
          title: 'Import course material', properties: ['openFile'],
          filters: [{ name: 'Course documents', extensions: ['pdf', 'pptx'] }],
        });
        if (choice.canceled || !choice.filePaths[0]) return;
        assertActive(controller);
        selected = await sourceStore.stage(choice.filePaths[0]);
        assertActive(controller);
        const imported = await libraryCall({ operation: 'import_document', class_id: classId, file_path: selected.file_path }, controller.signal);
        assertActive(controller);
        await reloadLibrary(controller, classId, true);
        await loadStoredDocument(controller, classId, imported.id);
      }
    } finally { await sourceStore.discard(selected); }
  });
}
async function deleteDocument() {
  const { selectedClassId: classId, selectedDocumentId: documentId } = state.library;
  if (!classId || !documentId) throw new ProofError('missing_document', 'Select a saved document first.');
  await localAction('library', async controller => {
    const response = await dialog.showMessageBox(window, { type: 'warning', buttons: ['Cancel', 'Delete from library'],
      defaultId: 0, cancelId: 0, message: 'Delete this saved document?', detail: 'Its library copy and search index will be removed. Your original file is preserved.' });
    assertActive(controller);
    if (response.response !== 1) return;
    await libraryCall({ operation: 'delete_document', class_id: classId, document_id: documentId }, controller.signal);
    assertActive(controller);
    await reloadLibrary(controller, classId);
  });
}
async function deleteClass() {
  const classId = state.library.selectedClassId;
  if (!classId) throw new ProofError('missing_class', 'Select a class first.');
  await localAction('library', async controller => {
    const response = await dialog.showMessageBox(window, { type: 'warning', buttons: ['Cancel', 'Delete class'],
      defaultId: 0, cancelId: 0, message: 'Delete this class and its saved documents?', detail: 'Only library copies will be removed. Your original files are preserved.' });
    assertActive(controller);
    if (response.response !== 1) return;
    await libraryCall({ operation: 'delete_class', class_id: classId }, controller.signal);
    assertActive(controller);
    source = undefined; delete state.document;
    await reloadLibrary(controller);
  });
}
async function backupLibrary() {
  await localAction('library', async controller => {
    const choice = await dialog.showSaveDialog(window, { title: 'Save a new library backup',
      defaultPath: 'Grounded-Class-Tutor-backup.sqlite3', buttonLabel: 'Save new backup',
      filters: [{ name: 'GCT library backup', extensions: ['sqlite3'] }] });
    assertActive(controller);
    if (choice.canceled || !choice.filePath) return;
    await libraryCall({ operation: 'backup', output_path: choice.filePath }, controller.signal);
    assertActive(controller);
    state.notice = 'Library backup saved. It includes course documents and excludes account credentials.';
  });
}
async function showCitation(value) {
  const { chunkId } = actionObject(value, ['chunkId']);
  const currentResult = state.result, currentAnswerSource = answerSource;
  const citation = state.result?.citations?.find(entry => entry.chunk_id === chunkId);
  if (!citation || answerSource?.sample || answerSource?.classId !== state.library.selectedClassId || !state.library.selectedClassId) throw new ProofError('invalid_citation', 'Choose a citation from the current saved-class answer.');
  await localAction('citation', async controller => {
    const cited = await libraryCall({ operation: 'citation', class_id: state.library.selectedClassId, chunk_id: chunkId }, controller.signal);
    assertActive(controller);
    if (state.result !== currentResult || answerSource !== currentAnswerSource) throw new ProofError('cancelled', 'The answer changed before this citation finished opening.');
    if (cited.filename !== citation.file || cited.page_or_slide !== citation.page_or_slide) throw new ProofError('invalid_citation', 'This citation no longer matches the saved source.');
    state.citation = cited;
  }, { clear: false });
}
async function ask(value) {
  assertIdle();
  if (state.session.status !== 'connected' || !state.session.sharing) throw new ProofError('sign_in_required', 'Connect ChatGPT and enable plan usage first.');
  const request = validateAsk(value, state.document, state.models, state.library);
  const scope = request.scope ?? 'pages';
  const sample = source?.sample && scope === 'pages';
  if (!sample && (!state.library.selectedClassId || (scope !== 'class' && !state.library.selectedDocumentId))) {
    throw new ProofError('missing_source', 'Select a saved class or document, or use the sample.');
  }
  const profile = state.session.profileId;
  await localAction('ask', async controller => {
    const assertCurrent = () => {
      assertActive(controller);
      if (state.session.profileId !== profile || !state.session.sharing || state.session.status !== 'connected') {
        throw new ProofError('cancelled', messages.cancelled);
      }
    };
    const options = { ...runtimeOptions, signal: controller.signal,
      generate: async (messages, generationSignal) => {
        assertCurrent();
        const response = await runSDK(() => chatgpt.streamResponse(responseOptions(messages, request.model, generationSignal)));
        assertCurrent();
        return response.text;
      },
    };
    const result = sample
      ? await runBridge(repoRoot, { sample: true, question: request.question, pages: request.pages }, options)
      : await runLibrary(repoRoot, join(app.getPath('userData'), 'library.sqlite3'), {
        operation: 'ask', class_id: state.library.selectedClassId, question: request.question,
        ...(scope === 'class' ? {} : { document_id: state.library.selectedDocumentId }),
        ...(scope === 'pages' ? { pages: request.pages } : {}),
      }, options);
    assertCurrent();
    state.result = result;
    answerSource = { sample: Boolean(sample), classId: state.library.selectedClassId };
  });
}

function validateSender(event) {
  if (closing || !window || event.sender !== window.webContents ||
      event.senderFrame !== window.webContents.mainFrame || event.senderFrame.url !== rendererURL) {
    throw new Error('This action is available only in GCT.');
  }
}
function registerActions() {
  const actions = {
    getState: () => {},
    signIn: connect,
    cancelSignIn: () => chatgpt.cancelSignIn(),
    signOut: async () => {
      assertIdle(); clearAnswer(); state.busy = 'signin'; publish();
      try {
        await runSDK(() => chatgpt.disconnect());
        if (!closing) setSession(await runSDK(() => chatgpt.getSession()));
      }
      finally { state.busy = null; publish(); }
    },
    listModels: async () => { assertIdle(); await refreshModels(); },
    chooseFile: () => selectDocument(false),
    useSample: () => selectDocument(true),
    ask, createClass, selectClass, selectStoredDocument, deleteDocument, deleteClass, backupLibrary, showCitation,
    cancelAsk: () => cancelCurrent(),
    openUsage: () => shell.openExternal(CHATGPT_USAGE_URL),
  };
  for (const [name, action] of Object.entries(actions)) {
    ipcMain.handle(`gct:${name}`, async (event, ...args) => {
      validateSender(event);
      const acceptsValue = ['ask', 'createClass', 'selectClass', 'selectStoredDocument', 'showCitation'].includes(name);
      if ((acceptsValue && args.length !== 1) || (!acceptsValue && args.length)) throw new Error('Unexpected action arguments.');
      try { await action(...args); }
      catch (error) { state.notice = safeMessage(error); publish(); }
      return snapshot();
    });
  }
}

app.on('second-instance', () => { window?.show(); window?.focus(); });
app.on('window-all-closed', () => app.quit());
app.on('before-quit', event => {
  if (closing) { if (!shutdownComplete) event.preventDefault(); return; }
  closing = true; event.preventDefault();
  appLifetime.abort();
  chatgpt?.cancelSignIn(); cancelCurrent();
  // SDK cancellation may still be finishing a refresh-token rotation and its
  // encrypted checkpoint under the SDK's own bounded network deadlines. Wait
  // for those promises too: a bridge can reject before its SDK call settles.
  // runSDK refuses new operations once closing, so this snapshot is complete.
  Promise.allSettled([stopBridges(), ...sdkOperations]).then(() => sourceStore?.close())
    .catch(() => {})
    .finally(() => { shutdownComplete = true; app.quit(); });
});

let shutdownComplete = false;

async function startApp() {
  await app.whenReady();
  if (closing) process.exit(0);
  await mkdir(app.getPath('userData'), { recursive: true, mode: 0o700 });
  sourceStore = await createSourceStore(join(app.getPath('userData'), 'document-staging'));
  if (closing) { await sourceStore.close(); return; }
  chatgpt = createChatGPT({
    appName: 'Grounded Class Tutor', appId: 'grounded-class-tutor', redirectPort: 0,
    storageDir: join(app.getPath('userData'), 'chatgpt'), sendHostId: true,
    credentialEncryption: {
      id: 'electron-safe-storage-v1',
      isAvailable: () => safeStorage.isEncryptionAvailable() &&
        (process.platform !== 'linux' || ['gnome_libsecret', 'kwallet', 'kwallet5', 'kwallet6'].includes(safeStorage.getSelectedStorageBackend())),
      encrypt: plaintext => safeStorage.encryptString(plaintext),
      decrypt: bytes => safeStorage.decryptString(Buffer.from(bytes)),
    },
    openBrowser: async value => {
      const url = new URL(value);
      if (url.origin !== 'https://auth.openai.com') throw new ProofError('auth_destination', 'Unexpected sign-in destination.');
      await shell.openExternal(url.href);
    },
  });
  chatgpt.subscribe(setSession);
  window = new BrowserWindow({ width: 1120, height: 850, minWidth: 720, minHeight: 620,
    title: 'Grounded Class Tutor', backgroundColor: '#f5f3ed',
    webPreferences: { preload: join(directory, 'preload.cjs'), nodeIntegration: false,
      contextIsolation: true, sandbox: true, webSecurity: true },
  });
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', event => event.preventDefault());
  window.webContents.on('will-attach-webview', event => event.preventDefault());
  window.webContents.session.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
  window.webContents.session.setPermissionCheckHandler(() => false);
  registerActions();
  await window.loadFile(join(directory, 'renderer/index.html'));
  try { await localAction('library', controller => reloadLibrary(controller)); }
  catch (error) { state.notice = safeMessage(error); publish(); }
  try { setSession(await runSDK(() => chatgpt.getSession())); await refreshModels(); }
  catch (error) { state.notice = safeMessage(error); publish(); }
}

// Do not await readiness at module scope: Electron must finish loading this ESM
// entrypoint before it can emit ready.
startApp().catch(() => { console.error('GCT could not open its local preview.'); app.quit(); });
