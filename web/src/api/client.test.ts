import { afterEach, describe, expect, it, vi } from 'vitest';

import { CLIENT_KINDS, createApiClient, type ApiClient, type ApiResult } from './client';
import type { AskResponse, FileStatusResponse } from './types';

interface Call {
  url: string;
  init: RequestInit;
}

// A fetch that records every call and answers with `respond`. No network, no globals.
function fakeFetch(respond: (call: Call) => Response | Promise<Response>) {
  const calls: Call[] = [];
  // The client always passes a string URL; a Request or URL object here would fail the type.
  const fetch = vi.fn((input: string, init?: RequestInit) => {
    const call = { url: input, init: init ?? {} };
    calls.push(call);
    return Promise.resolve(respond(call));
  });
  return { fetch: fetch as unknown as typeof globalThis.fetch, calls };
}

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const envelope = (status: number, kind: string, message: string, detail: unknown = null) =>
  json(status, { error: { kind, message, detail } });

const CLASS_ID = '3f2c8a1e-5b4d-4c6e-9a7b-1d2e3f4a5b6c';
const FILE_ID = '9b8a7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d';

function client(respond: (call: Call) => Response | Promise<Response>, baseUrl?: string) {
  const fake = fakeFetch(respond);
  const api = createApiClient(
    baseUrl === undefined ? { fetch: fake.fetch } : { fetch: fake.fetch, baseUrl },
  );
  return { api, calls: fake.calls };
}

function only<T>(items: T[]): T {
  expect(items).toHaveLength(1);
  return items[0] as T;
}

function jsonBody(call: Call): unknown {
  expect(typeof call.init.body).toBe('string');
  return JSON.parse(call.init.body as string);
}

function expectFailure<T>(result: ApiResult<T>) {
  if (result.ok) {
    throw new Error(`expected a failure, got ok ${JSON.stringify(result.data)}`);
  }
  return result;
}

const PDF = () =>
  new File([new Uint8Array([37, 80, 68, 70])], 'Lecture 1.pdf', { type: 'application/pdf' });

// One call per route, so a rule about EVERY route is checked on every route.
const ROUTES: [string, (api: ApiClient) => Promise<ApiResult<unknown>>][] = [
  ['createClass', (api) => api.createClass('Bio 101')],
  ['uploadFile', (api) => api.uploadFile(CLASS_ID, PDF())],
  ['getFileStatus', (api) => api.getFileStatus(FILE_ID)],
  ['ask', (api) => api.ask(CLASS_ID, 'What is osmosis?')],
];

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('construction', () => {
  it('sends relative paths by default, so a page goes through the dev proxy', async () => {
    const { api, calls } = client(() => json(201, { class_id: CLASS_ID, name: 'Bio 101' }));
    await api.createClass('Bio 101');
    expect(only(calls).url).toBe('/classes');
  });

  it('prefixes every path with baseUrl', async () => {
    const { api, calls } = client(() => json(200, {}), 'http://127.0.0.1:5173');
    for (const [, call] of ROUTES) {
      await call(api);
    }
    expect(calls.map((c) => c.url)).toStrictEqual([
      'http://127.0.0.1:5173/classes',
      'http://127.0.0.1:5173/files',
      `http://127.0.0.1:5173/files/${FILE_ID}`,
      'http://127.0.0.1:5173/ask',
    ]);
  });

  it('refuses a baseUrl that ends with a slash, and accepts one that does not', () => {
    expect(() => createApiClient({ baseUrl: 'http://127.0.0.1:5173/' })).toThrow(TypeError);
    expect(() => createApiClient({ baseUrl: '/' })).toThrow(/must not end with "\/"/);
    expect(() => createApiClient({ baseUrl: 'http://127.0.0.1:5173' })).not.toThrow();
    expect(() => createApiClient()).not.toThrow();
  });

  it('uses the injected fetch and never the global one', async () => {
    const global = vi.fn();
    vi.stubGlobal('fetch', global);
    const { api, calls } = client(() => json(201, { class_id: CLASS_ID, name: 'x' }));
    await api.createClass('x');
    expect(calls).toHaveLength(1);
    expect(global).not.toHaveBeenCalled();
  });

  it('falls back to the global fetch, looked up when the call is made', async () => {
    const api = createApiClient();
    const global = fakeFetch(() => json(201, { class_id: CLASS_ID, name: 'x' }));
    vi.stubGlobal('fetch', global.fetch);
    const result = await api.createClass('x');
    expect(result.ok).toBe(true);
    expect(only(global.calls).url).toBe('/classes');
  });

  it('mints only namespaced kinds, so none can equal a kind the API mints', () => {
    for (const kind of Object.values(CLIENT_KINDS)) {
      expect(kind).toMatch(/^client\.[a-z_]+$/);
    }
  });
});

describe('POST /classes', () => {
  it('sends exactly {name} as JSON - no owner id - and returns the 201 body', async () => {
    const { api, calls } = client(() => json(201, { class_id: CLASS_ID, name: '  Bio 101 ' }));
    const result = await api.createClass('  Bio 101 ');
    const call = only(calls);
    expect(call.url).toBe('/classes');
    expect(call.init.method).toBe('POST');
    expect(call.init.headers).toStrictEqual({ 'Content-Type': 'application/json' });
    // Exactly the model's one field, spelt as typed: no owner_id, no trimming.
    expect(jsonBody(call)).toStrictEqual({ name: '  Bio 101 ' });
    expect(result).toStrictEqual({
      ok: true,
      status: 201,
      data: { class_id: CLASS_ID, name: '  Bio 101 ' },
    });
  });

  it("returns the route's own refusal as an error", async () => {
    const { api } = client(() => envelope(400, 'blank_name', 'name must not be blank.'));
    const result = expectFailure(await api.createClass('   '));
    expect(result.status).toBe(400);
    expect(result.error).toStrictEqual({
      kind: 'blank_name',
      message: 'name must not be blank.',
      detail: null,
    });
  });
});

describe('POST /files', () => {
  it('sends a multipart form of exactly class_id and file, with no hand-set Content-Type', async () => {
    const { api, calls } = client(() => json(202, { file_id: FILE_ID, filename: 'Lecture 1.pdf' }));
    const result = await api.uploadFile(CLASS_ID, PDF());
    const call = only(calls);
    expect(call.url).toBe('/files');
    expect(call.init.method).toBe('POST');
    expect(call.init.headers).toBeUndefined();

    const form = call.init.body;
    expect(form).toBeInstanceOf(FormData);
    expect([...(form as FormData).keys()]).toStrictEqual(['class_id', 'file']);
    expect((form as FormData).get('class_id')).toBe(CLASS_ID);
    const file = (form as FormData).get('file') as File;
    expect(file.name).toBe('Lecture 1.pdf');
    expect(new Uint8Array(await file.arrayBuffer())).toStrictEqual(
      new Uint8Array([37, 80, 68, 70]),
    );

    // What leaving the header alone buys: the runtime writes the boundary into it.
    const sent = new Request('http://127.0.0.1/files', call.init);
    expect(sent.headers.get('content-type')).toMatch(/^multipart\/form-data; boundary=\S+$/);

    expect(result).toStrictEqual({
      ok: true,
      status: 202,
      data: { file_id: FILE_ID, filename: 'Lecture 1.pdf' },
    });
  });

  it('returns 404 class_not_found as an error', async () => {
    const { api } = client(() => envelope(404, 'class_not_found', 'No such class.'));
    const result = expectFailure(await api.uploadFile(CLASS_ID, PDF()));
    expect([result.status, result.error.kind]).toStrictEqual([404, 'class_not_found']);
  });

  it('names the size limit in every message it writes itself - and only for an upload', async () => {
    const cases: [string, () => Response | Promise<Response>][] = [
      ['a network failure', () => Promise.reject(new TypeError('fetch failed'))],
      ['a proxy 502', () => new Response('', { status: 502 })],
      [
        'a non-envelope 413',
        () => new Response('<h1>413 Request Entity Too Large</h1>', { status: 413 }),
      ],
    ];
    for (const [, respond] of cases) {
      const upload = expectFailure(await client(respond).api.uploadFile(CLASS_ID, PDF()));
      expect(upload.error.message).toContain('over the upload size limit');
      const create = expectFailure(await client(respond).api.createClass('x'));
      expect(create.error.message).not.toContain('size limit');
      // The same failure, the same kind: the hint changes the words, not the switch.
      expect(upload.error.kind).toBe(create.error.kind);
    }
  });

  it("passes the API's own 413 through untouched - the hint is only for replies it could not read", async () => {
    const message = 'upload is larger than the 104865792-byte limit for a multipart request.';
    const { api } = client(() => envelope(413, 'body_too_large', message));
    const result = expectFailure(await api.uploadFile(CLASS_ID, PDF()));
    expect(result.error).toStrictEqual({ kind: 'body_too_large', message, detail: null });
  });
});

describe('GET /files/{file_id}', () => {
  it('GETs the id with no body and no headers, and returns every status as ok - failed included', async () => {
    const failed: FileStatusResponse = {
      filename: 'scan.pdf',
      status: 'failed',
      failed_reason: 'unparseable',
      message: 'We could not read any text out of this file.',
    };
    const { api, calls } = client(() => json(200, failed));
    const result = await api.getFileStatus(FILE_ID);
    const call = only(calls);
    expect(call.url).toBe(`/files/${FILE_ID}`);
    expect(call.init.method).toBe('GET');
    expect(call.init.body).toBeUndefined();
    expect(call.init.headers).toBeUndefined();
    expect(result).toStrictEqual({ ok: true, status: 200, data: failed });
  });

  it('percent-encodes the id, so it cannot change the path', async () => {
    const { api, calls } = client(() => envelope(400, 'bad_file_id', 'not a uuid'));
    await api.getFileStatus('a/b?c#d e');
    expect(only(calls).url).toBe('/files/a%2Fb%3Fc%23d%20e');
  });

  it('sends nothing for an empty id, which would be a trailing slash, and says so', async () => {
    const { api, calls } = client(() => json(200, {}));
    const result = expectFailure(await api.getFileStatus(''));
    expect(calls).toHaveLength(0);
    expect(result.status).toBeNull();
    expect(result.error.kind).toBe(CLIENT_KINDS.missingFileId);
    expect(result.error.message).toMatch(/nothing was sent/);
    await api.getFileStatus('x');
    expect(only(calls).url).toBe('/files/x');
  });

  it('returns 404 file_not_found as an error', async () => {
    const { api } = client(() => envelope(404, 'file_not_found', 'No such file.'));
    const result = expectFailure(await api.getFileStatus(FILE_ID));
    expect([result.status, result.error.kind]).toStrictEqual([404, 'file_not_found']);
  });
});

describe('POST /ask', () => {
  const answer = (state: AskResponse['state'], prose: string | null): AskResponse => ({
    state,
    answer_prose: prose,
    citations:
      prose === null
        ? []
        : [{ label: 'S1', file: 'Lecture 1.pdf', page_or_slide: 3, chunk_id: 'c-1' }],
    coverage: { complete: state === 'GROUNDED', gaps: state === 'GROUNDED' ? [] : ['a gap'] },
    integrity: { ok: state !== 'INTEGRITY_FLAGGED', reasons: [] },
  });

  it('sends exactly {class_id, question} as JSON - no owner id, no k', async () => {
    const { api, calls } = client(() => json(200, answer('GROUNDED', 'Water moves [S1].')));
    await api.ask(CLASS_ID, ' What is osmosis? ');
    const call = only(calls);
    expect(call.url).toBe('/ask');
    expect(call.init.method).toBe('POST');
    expect(call.init.headers).toStrictEqual({ 'Content-Type': 'application/json' });
    expect(jsonBody(call)).toStrictEqual({ class_id: CLASS_ID, question: ' What is osmosis? ' });
  });

  it.each([
    ['GROUNDED', 'Water moves across a membrane [S1].'],
    ['PARTIAL', 'Part of it is covered [S1].'],
    ['INTEGRITY_FLAGGED', 'An answer whose citations did not check out [S9].'],
    ['REFUSAL', null],
  ] as const)('returns a 200 %s as ok, never as an error', async (state, prose) => {
    const body = answer(state, prose);
    const { api } = client(() => json(200, body));
    expect(await api.ask(CLASS_ID, 'q')).toStrictEqual({ ok: true, status: 200, data: body });
  });

  it("returns 503 provider_transient as an error carrying the API's kind and message", async () => {
    const message = 'The answer service is busy right now. Wait a moment and ask again.';
    const { api } = client(() => envelope(503, 'provider_transient', message));
    const result = expectFailure(await api.ask(CLASS_ID, 'q'));
    expect(result.status).toBe(503);
    expect(result.error).toStrictEqual({ kind: 'provider_transient', message, detail: null });
  });

  it('returns 500 embedding_mismatch as an error, not as the client reading a proxy', async () => {
    const { api } = client(() => envelope(500, 'embedding_mismatch', 'Re-index this class.'));
    const result = expectFailure(await api.ask(CLASS_ID, 'q'));
    expect([result.status, result.error.kind]).toStrictEqual([500, 'embedding_mismatch']);
  });

  it("keeps a 422's field list, truncation marker included, as detail", async () => {
    const detail = [
      { type: 'string_too_long', loc: ['body', 'question'], msg: 'too long', input: 'x' },
      {
        type: 'gct.detail_truncated',
        loc: ['detail'],
        msg: 'cut',
        input: null,
        ctx: { dropped: 3 },
      },
    ];
    const { api } = client(() => envelope(422, 'validation', 'The request is invalid.', detail));
    const result = expectFailure(await api.ask(CLASS_ID, 'q'.repeat(2001)));
    expect(result.status).toBe(422);
    expect(result.error.detail).toStrictEqual(detail);
  });
});

describe('the shared reply parser, on every route', () => {
  it.each(ROUTES)(
    '%s: body_too_large (413) and body_too_nested (400) are envelopes',
    async (_, call) => {
      for (const [status, kind] of [
        [413, 'body_too_large'],
        [400, 'body_too_nested'],
      ] as const) {
        const { api } = client(() => envelope(status, kind, `refused: ${kind}`));
        const result = expectFailure(await call(api));
        expect(result.status).toBe(status);
        expect(result.error).toStrictEqual({ kind, message: `refused: ${kind}`, detail: null });
      }
    },
  );

  it.each(ROUTES)('%s: a fetch rejection is a network failure with no status', async (_, call) => {
    const { api } = client(() => Promise.reject(new TypeError('fetch failed')));
    const result = expectFailure(await call(api));
    expect(result.status).toBeNull();
    expect(result.error.kind).toBe(CLIENT_KINDS.network);
    expect(result.error.message).toMatch(/Check that the server is running/);
    expect(result.error.detail).toBeNull();
  });

  it.each(ROUTES)('%s: a reply whose body breaks off is a network failure', async (_, call) => {
    const broken = () =>
      new Response(
        new ReadableStream({
          start(controller) {
            controller.error(new Error('connection reset'));
          },
        }),
        { status: 200 },
      );
    const result = expectFailure(await call(client(broken).api));
    expect(result.status).toBe(200);
    expect(result.error.kind).toBe(CLIENT_KINDS.network);
  });

  it.each(ROUTES)(
    '%s: a 5xx that is not an envelope is the API not answering - the dev proxy 502 included',
    async (_, call) => {
      for (const status of [500, 502, 503, 504]) {
        const reply = () => new Response('', { status, headers: { 'content-type': 'text/plain' } });
        const result = expectFailure(await call(client(reply).api));
        expect(result.status).toBe(status);
        expect(result.error.kind).toBe(CLIENT_KINDS.apiUnreachable);
        expect(result.error.message).toContain(`HTTP ${status}`);
        expect(result.error.message).toMatch(/Check that the API is running/);
      }
    },
  );

  it.each(ROUTES)(
    '%s: a 4xx that is not an envelope is a reply it cannot read',
    async (_, call) => {
      const replies = [
        () => new Response('Invalid HTTP request received.', { status: 400 }),
        () => new Response('', { status: 499 }),
        // FastAPI's default 422 body - what the published schema advertises, and not an envelope.
        () => json(422, { detail: [{ loc: ['body'], msg: 'Field required', type: 'missing' }] }),
      ];
      for (const reply of replies) {
        const result = expectFailure(await call(client(reply).api));
        expect(result.error.kind).toBe(CLIENT_KINDS.badResponse);
        expect(result.error.message).toMatch(/not one this page understands/);
      }
    },
  );

  it.each(ROUTES)(
    '%s: a 2xx that is not a JSON object is a reply it cannot read',
    async (_, call) => {
      const replies = [
        () => new Response('<!doctype html><title>Grounded Class Tutor</title>', { status: 200 }),
        () => json(200, null),
        () => json(200, [1, 2]),
        () => new Response('', { status: 201 }),
      ];
      for (const reply of replies) {
        const result = expectFailure(await call(client(reply).api));
        expect(result.error.kind).toBe(CLIENT_KINDS.badResponse);
      }
    },
  );

  it('keeps the status of a reply it cannot read', async () => {
    const result = expectFailure(
      await client(() => new Response('nope', { status: 404 })).api.createClass('x'),
    );
    expect(result.status).toBe(404);
    expect(result.error.message).toContain('HTTP 404');
  });
});
