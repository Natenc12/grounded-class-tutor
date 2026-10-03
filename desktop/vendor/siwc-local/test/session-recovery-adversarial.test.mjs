import test from 'node:test';
import assert from 'node:assert/strict';
import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createChatGPT } from '../dist/index.js';
import { ConnectionStore } from '../dist/storage.js';

// Disposable synthetic accounts only. No Electron, OS keychain, browser, or network.
async function fixture(t) {
  const directory = await mkdtemp(join(tmpdir(), 'gct-session-recovery-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  t.mock.method(globalThis, 'fetch', () => assert.fail('Session restoration must remain local.'));
  const key = randomBytes(32);
  const control = { available: true, denied: false, encrypts: 0, decrypts: 0, browsers: 0 };
  const provider = {
    id: 'synthetic-session-recovery-aes-gcm',
    isAvailable: () => control.available,
    encrypt(plaintext) {
      control.encrypts++;
      const iv = randomBytes(12), cipher = createCipheriv('aes-256-gcm', key, iv);
      return Buffer.concat([iv, cipher.update(plaintext, 'utf8'), cipher.final(), cipher.getAuthTag()]);
    },
    decrypt(ciphertext) {
      control.decrypts++;
      if (control.denied) throw new Error('PRIVATE native provider diagnostic');
      const bytes = Buffer.from(ciphertext);
      const cipher = createDecipheriv('aes-256-gcm', key, bytes.subarray(0, 12));
      cipher.setAuthTag(bytes.subarray(-16));
      return Buffer.concat([cipher.update(bytes.subarray(12, -16)), cipher.final()]).toString('utf8');
    },
  };
  const store = new ConnectionStore(directory, provider);
  const saved = { version: 2, activeProfileId: 'synthetic-profile', pendingRegistrations: [], profiles: [{
    version: 1, id: 'synthetic-profile', label: 'Synthetic connection', clientId: 'synthetic-issued-client',
    subject: 'synthetic-subject', status: 'connected', scopes: ['openid', 'offline_access', 'chatgpt.tokens.use.direct'],
    savedAt: new Date().toISOString(), credentials: { accessToken: 'PRIVATE synthetic access',
      refreshToken: 'PRIVATE synthetic refresh', expiresAt: Date.now() + 3600000 },
  }] };
  await store.withLock(() => store.write(saved));
  const host = await store.withLock(() => store.getHostId());
  const client = () => createChatGPT({ appName: 'Synthetic session recovery', appId: 'synthetic-session-recovery',
    redirectPort: 0, storageDir: directory, credentialEncryption: provider,
    openBrowser: () => { control.browsers++; assert.fail('Restoring a saved connection cannot open OAuth.'); } });
  const bytes = () => readFile(join(directory, 'chatgpt-auth.json'));
  return { directory, store, saved, host, client, bytes, control };
}

function restored(session) {
  assert.equal(session.status, 'connected');
  assert.equal(session.sharing, true);
  assert.equal(session.profileId, 'synthetic-profile');
  assert.doesNotMatch(JSON.stringify(session), /PRIVATE|synthetic-issued-client|synthetic-subject/);
}

test('fresh runtime instances restore the encrypted profile and host without rewriting or OAuth', async t => {
  const f = await fixture(t), before = await f.bytes(), encrypts = f.control.encrypts;
  for (let launch = 0; launch < 3; launch++) {
    restored(await f.client().getSession());
    assert.equal(await f.store.withLock(() => f.store.getHostId()), f.host);
  }
  assert.deepEqual(await f.bytes(), before);
  assert.equal(f.control.encrypts, encrypts);
  assert.equal(f.control.browsers, 0);
});

for (const problem of ['unavailable', 'denied']) {
  test(`${problem} storage is recoverable without browser login or credential replacement`, async t => {
    const f = await fixture(t), client = f.client(), before = await f.bytes(), encrypts = f.control.encrypts;
    if (problem === 'unavailable') f.control.available = false;
    else f.control.denied = true;
    const blocked = await client.getSession();
    assert.equal(blocked.sharing, false);
    assert.notEqual(blocked.status, 'reauth_required', 'An OS storage failure is not proof that OAuth expired.');
    assert.equal(blocked.error.code, problem === 'unavailable' ? 'storage_encryption_unavailable' : 'storage_decryption_failed');
    assert.doesNotMatch(JSON.stringify(blocked), /PRIVATE/);
    assert.deepEqual(await f.bytes(), before);
    f.control.available = true; f.control.denied = false;
    restored(await client.getSession());
    restored(await f.client().getSession());
    assert.deepEqual(await f.bytes(), before);
    assert.equal(f.control.encrypts, encrypts);
    assert.equal(f.control.browsers, 0);
  });
}

test('expired access alone does not force browser reauthentication during a local session read', async t => {
  const f = await fixture(t);
  f.saved.profiles[0].credentials.expiresAt = Date.now() - 60000;
  await f.store.withLock(() => f.store.write(f.saved));
  const before = await f.bytes();
  restored(await f.client().getSession());
  assert.deepEqual(await f.bytes(), before);
  assert.equal(f.control.browsers, 0);
});

test('a saved genuinely revoked profile remains reauthentication-required with its registration preserved', async t => {
  const f = await fixture(t), profile = f.saved.profiles[0];
  profile.status = 'reauth_required'; profile.scopes = []; delete profile.credentials;
  await f.store.withLock(() => f.store.write(f.saved));
  const before = await f.bytes(), session = await f.client().getSession();
  assert.equal(session.status, 'reauth_required'); assert.equal(session.sharing, false);
  assert.equal(session.profileId, profile.id);
  const after = await f.store.withLock(() => f.store.read());
  assert.equal(after.profiles[0].clientId, 'synthetic-issued-client');
  assert.equal(await f.store.withLock(() => f.store.getHostId()), f.host);
  assert.deepEqual(await f.bytes(), before);
  assert.equal(f.control.browsers, 0);
});
