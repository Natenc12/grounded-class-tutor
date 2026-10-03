import test from 'node:test';
import assert from 'node:assert/strict';
import { streamResponse } from '../vendor/siwc-local/dist/responses.js';

const wire = events => new Response(events.map(event => `data: ${JSON.stringify(event)}\n\n`).join(''), {
  headers: { 'content-type': 'text/event-stream' },
});

test('actual SDK uses subscription route and accepts only completed text', async t => {
  let body;
  t.mock.method(globalThis, 'fetch', async (url, request) => {
    assert.equal(url, 'https://api.openai.com/v1/responses');
    assert.equal(request.headers.authorization, 'Bearer synthetic-oauth-token');
    body = JSON.parse(request.body);
    return wire([{ type: 'response.output_text.delta', delta: 'Example [S1]' }, {
      type: 'response.completed', response: { status: 'completed', error: null, output: [
        { type: 'message', role: 'assistant', content: [{ type: 'output_text', text: 'Example [S1]' }] },
      ] },
    }]);
  });
  const result = await streamResponse('synthetic-oauth-token', {
    model: 'account-model', instructions: 'Use sources.', input: [{ role: 'user', content: 'Source text.' }],
  }, new AbortController().signal);
  assert.equal(result.text, 'Example [S1]');
  assert.equal(body.store, false);
  assert.equal(body.stream, true);
  assert.deepEqual(Object.keys(body).sort(), ['input', 'instructions', 'model', 'store', 'stream']);
});

for (const chunking of ['coalesced', 'separate']) {
  test(`actual SDK accepts the observed sparse completion and ignores late data (${chunking})`, async t => {
    const events = [
      { type: 'response.output_text.delta', delta: 'Finished [S1]' },
      { type: 'response.completed', response: { status: 'completed', output: [] } },
      { type: 'response.output_text.delta', delta: ' UNTRUSTED LATE TEXT' },
    ];
    const chunks = events.map(event => `data: ${JSON.stringify(event)}\n\n`);
    let attempts = 0;
    t.mock.method(globalThis, 'fetch', async () => {
      attempts++;
      return chunking === 'coalesced' ? wire(events) : new Response(new ReadableStream({
        start(controller) {
          for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk));
          controller.close();
        },
      }), { headers: { 'content-type': 'text/event-stream' } });
    });
    assert.equal((await streamResponse('synthetic-oauth-token', {
      model: 'account-model', input: 'Question',
    }, new AbortController().signal)).text, 'Finished [S1]');
    assert.equal(attempts, 1);
  });
}

test('actual SDK rejects sparse completion without any streamed text', async t => {
  t.mock.method(globalThis, 'fetch', async () => wire([
    { type: 'response.completed', response: { status: 'completed', output: [] } },
  ]));
  await assert.rejects(streamResponse('synthetic-oauth-token', {
    model: 'account-model', input: 'Question',
  }, new AbortController().signal), error => error.code === 'invalid_stream');
});

for (const [name, ending] of [
  ['missing terminal event', []],
  ['incomplete response', [{ type: 'response.incomplete' }]],
  ['limit reached after deltas', [{ type: 'response.failed', response: { error: { code: 'subscription_sharing_usage_limit_exceeded' } } }]],
]) {
  test(`actual SDK rejects ${name} without retrying`, async t => {
    let attempts = 0;
    t.mock.method(globalThis, 'fetch', async () => {
      attempts++;
      return wire([{ type: 'response.output_text.delta', delta: 'Unfinished [S1]' }, ...ending]);
    });
    await assert.rejects(streamResponse('synthetic-oauth-token', {
      model: 'account-model', input: 'Question',
    }, new AbortController().signal));
    assert.equal(attempts, 1);
  });
}
