import test from 'node:test';
import assert from 'node:assert/strict';
import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createChatGPT } from '../dist/index.js';
import { ConnectionStore } from '../dist/storage.js';

// Tests own every byte and never access Electron or the system credential store.
async function fixture(t) {
  const directory = await mkdtemp(join(tmpdir(), 'gct-saved-session-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const key = randomBytes(32), control = { denied: false, decrypts: 0 };
  const provider = {
    id: 'synthetic-saved-session-aes-gcm', isAvailable: () => true,
    encrypt(plaintext) {
      const iv = randomBytes(12), cipher = createCipheriv('aes-256-gcm', key, iv);
      return Buffer.concat([iv, cipher.update(plaintext, 'utf8'), cipher.final(), cipher.getAuthTag()]);
    },
    decrypt(value) {
      control.decrypts++;
      if (control.denied) throw new Error('PRIVATE synthetic OS diagnostic');
      const bytes = Buffer.from(value), cipher = createDecipheriv('aes-256-gcm', key, bytes.subarray(0, 12));
      cipher.setAuthTag(bytes.subarray(-16));
      return Buffer.concat([cipher.update(bytes.subarray(12, -16)), cipher.final()]).toString('utf8');
    },
  };
  const store = new ConnectionStore(directory, provider);
  const saved = { version: 2, activeProfileId: 'synthetic-profile', pendingRegistrations: [], profiles: [{
    version: 1, id: 'synthetic-profile', label: 'Synthetic connection', clientId: 'synthetic-client',
    subject: 'synthetic-subject', status: 'connected', scopes: ['openid', 'chatgpt.tokens.use.direct'],
    savedAt: new Date().toISOString(), credentials: { accessToken: 'PRIVATE synthetic access',
      refreshToken: 'PRIVATE synthetic refresh', expiresAt: Date.now() + 3600000 },
  }] };
  await store.withLock(() => store.write(saved));
  const client = createChatGPT({ appName: 'Saved Session Test', appId: 'saved-session-test', redirectPort: 0,
    storageDir: directory, credentialEncryption: provider,
    openBrowser: () => assert.fail('These recovery tests must not open a browser.') });
  const seen = [];
  client.subscribe(session => seen.push(session));
  return { client, control, seen, bytes: () => readFile(join(directory, 'chatgpt-auth.json')) };
}

test('successful local restoration clears a prior catalog error without network or credential writes', async t => {
  const f = await fixture(t), before = await f.bytes();
  await f.client.getSession();
  t.mock.method(globalThis, 'fetch', async () => Response.json({ error: {
    code: 'subscription_sharing_usage_unavailable', message: 'PRIVATE provider echo',
  } }, { status: 503 }));
  await assert.rejects(f.client.listModels(), error => error.code === 'subscription_sharing_usage_unavailable');
  assert.equal(f.seen.at(-1).error.code, 'subscription_sharing_usage_unavailable');
  t.mock.method(globalThis, 'fetch', () => assert.fail('getSession must not use the network.'));
  const recovered = await f.client.getSession();
  assert.equal(recovered.status, 'connected');
  assert.equal(recovered.profileId, 'synthetic-profile');
  assert.equal(recovered.error, undefined);
  assert.deepEqual(await f.bytes(), before);
  assert.doesNotMatch(JSON.stringify(f.seen), /PRIVATE/);
});

for (const action of ['getSession', 'listModels', 'signIn']) {
  test(`${action} reports denied storage without immediately repeating the OS read`, async t => {
    const f = await fixture(t), before = await f.bytes();
    await f.client.getSession();
    f.control.denied = true;
    const reads = f.control.decrypts;
    t.mock.method(globalThis, 'fetch', () => assert.fail('Denied storage cannot use the network.'));
    if (action === 'getSession') await f.client.getSession();
    else await assert.rejects(f.client[action](), error => error.code === 'storage_decryption_failed');
    assert.equal(f.control.decrypts - reads, 1);
    assert.equal(f.seen.at(-1).status, 'storage_unavailable');
    assert.equal(f.seen.at(-1).error.code, 'storage_decryption_failed');
    assert.doesNotMatch(JSON.stringify(f.seen), /PRIVATE/);
    f.control.denied = false;
    const recovered = await f.client.getSession();
    assert.equal(recovered.status, 'connected');
    assert.equal(recovered.error, undefined);
    assert.deepEqual(await f.bytes(), before);
  });
}
