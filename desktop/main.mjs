import { app, BrowserWindow, dialog, ipcMain, safeStorage, shell } from 'electron';
import { createChatGPT, CHATGPT_USAGE_URL } from '@siwc/local';
import { mkdir } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { ProofError, responseOptions, runBridge, stopBridges, validateAsk } from './runtime.mjs';
import { createSourceStore } from './source-store.mjs';

const directory = dirname(fileURLToPath(import.meta.url));
const repoRoot = dirname(directory);
const rendererURL = pathToFileURL(join(directory, 'renderer/index.html')).href;
app.setName('Grounded Class Tutor');
app.setPath('userData', join(app.getPath('appData'), 'Grounded Class Tutor Local Proof'));
if (!app.requestSingleInstanceLock()) { app.quit(); process.exit(0); }

let window, chatgpt, sourceStore, source, active, closing = false;
let modelRevision = 0;
const sdkOperations = new Set();
const appLifetime = new AbortController();
const state = { session: { status: 'disconnected', sharing: false }, models: [], busy: null };
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
function clearAnswer() { delete state.result; delete state.notice; }
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
    cancelCurrent();
    state.models = [];
    delete state.result;
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
async function selectDocument(sample) {
  assertIdle();
  const controller = new AbortController();
  active = controller;
  state.busy = 'document'; clearAnswer(); publish();
  let selected, committed = false;
  try {
    if (sample) selected = { sample: true };
    else {
      const choice = await dialog.showOpenDialog(window, {
        title: 'Choose course material', properties: ['openFile'],
        filters: [{ name: 'Course documents', extensions: ['pdf', 'pptx'] }],
      });
      if (choice.canceled || !choice.filePaths[0]) return;
      if (controller.signal.aborted || closing) throw new ProofError('cancelled', messages.cancelled);
      selected = await sourceStore.stage(choice.filePaths[0]);
    }
    const document = await runBridge(repoRoot, selected, { inspect: true, signal: controller.signal });
    if (controller.signal.aborted || closing) throw new ProofError('cancelled', messages.cancelled);
    const previous = source;
    source = selected;
    state.document = { filename: document.filename, page_count: document.page_count,
      pages: document.pages, sample: sample === true,
      ...(document.suggested_question ? { suggested_question: document.suggested_question } : {}) };
    committed = true;
    await sourceStore.discard(previous);
  } finally {
    controller.abort();
    try { if (!committed) await sourceStore.discard(selected); }
    finally {
      if (active === controller) active = undefined;
      state.busy = null; publish();
    }
  }
}
async function ask(value) {
  assertIdle();
  if (state.session.status !== 'connected' || !state.session.sharing) throw new ProofError('sign_in_required', 'Connect ChatGPT and enable plan usage first.');
  const request = validateAsk(value, state.document, state.models);
  if (!source) throw new ProofError('missing_source', 'Choose a document or the sample first.');
  const profile = state.session.profileId;
  const controller = new AbortController();
  active = controller;
  state.busy = 'ask'; clearAnswer(); publish();
  const assertCurrent = () => {
    if (controller.signal.aborted || active !== controller || state.session.profileId !== profile || !state.session.sharing) {
      throw new ProofError('cancelled', messages.cancelled);
    }
  };
  try {
    const result = await runBridge(repoRoot, { ...source, question: request.question, pages: request.pages }, {
      signal: controller.signal,
      generate: async (messages, generationSignal) => {
        assertCurrent();
        const response = await runSDK(() => chatgpt.streamResponse(responseOptions(messages, request.model, generationSignal)));
        assertCurrent();
        return response.text;
      },
    });
    assertCurrent();
    state.result = result;
  } finally {
    controller.abort();
    if (active === controller) active = undefined;
    state.busy = null;
    publish();
  }
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
    ask,
    cancelAsk: () => cancelCurrent(),
    openUsage: () => shell.openExternal(CHATGPT_USAGE_URL),
  };
  for (const [name, action] of Object.entries(actions)) {
    ipcMain.handle(`gct:${name}`, async (event, ...args) => {
      validateSender(event);
      if ((name === 'ask' && args.length !== 1) || (name !== 'ask' && args.length)) throw new Error('Unexpected action arguments.');
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
  try { setSession(await runSDK(() => chatgpt.getSession())); await refreshModels(); }
  catch (error) { state.notice = safeMessage(error); publish(); }
}

// Do not await readiness at module scope: Electron must finish loading this ESM
// entrypoint before it can emit ready.
startApp().catch(() => { console.error('GCT could not open its local preview.'); app.quit(); });
