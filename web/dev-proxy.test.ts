import { createServer as createHttpServer, type IncomingMessage, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';

import { createServer, type ViteDevServer } from 'vite';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import {
  API_PATH_PATTERN,
  API_TARGET_ENV,
  DEFAULT_API_TARGET,
  apiProxy,
  apiTarget,
} from './dev-proxy.ts';

describe('apiTarget', () => {
  it('reads the variable ADR 0033 documents, by that name', () => {
    expect(API_TARGET_ENV).toBe('GCT_API_TARGET');
  });

  it('is uvicorn on the IPv4 loopback when the variable is unset', () => {
    expect(apiTarget(undefined)).toBe('http://127.0.0.1:8000');
    expect(DEFAULT_API_TARGET).toBe('http://127.0.0.1:8000');
  });

  it.each([
    ['http://127.0.0.1:8001', 'http://127.0.0.1:8001'],
    ['http://127.0.0.1:8001/', 'http://127.0.0.1:8001'],
    ['https://api.example.test', 'https://api.example.test'],
  ])('takes an http(s) origin: %s', (value, origin) => {
    expect(apiTarget(value)).toBe(origin);
  });

  const NOT_A_URL = 'is not a URL';
  const NOT_HTTP = 'is not an http or https URL';
  const NOT_AN_ORIGIN = 'has more than an origin (a path, query, fragment or login)';

  it.each([
    ['empty', '', NOT_A_URL],
    ['no scheme', '127.0.0.1:8001', NOT_A_URL],
    ['a host read as a scheme', 'localhost:8001', NOT_HTTP],
    ['garbage', 'not a url', NOT_A_URL],
    ['another scheme', 'ftp://127.0.0.1:8001', NOT_HTTP],
    ['a websocket', 'ws://127.0.0.1:8001', NOT_HTTP],
    ['a path', 'http://127.0.0.1:8001/api', NOT_AN_ORIGIN],
    ['a query', 'http://127.0.0.1:8001/?x=1', NOT_AN_ORIGIN],
    ['a fragment', 'http://127.0.0.1:8001/#x', NOT_AN_ORIGIN],
    ['a login', 'http://user:pw@127.0.0.1:8001', NOT_AN_ORIGIN],
  ])(
    'refuses %s with the whole sentence: the variable, the problem, the remedy',
    (_, value, problem) => {
      let message = '';
      try {
        apiTarget(value);
      } catch (err) {
        message = (err as Error).message;
      }
      expect(message).toBe(
        `GCT_API_TARGET=${JSON.stringify(value)} ${problem}. Set it to the API's origin only, ` +
          'for example GCT_API_TARGET=http://127.0.0.1:8001, or unset it to use http://127.0.0.1:8000.',
      );
    },
  );
});

describe('API_PATH_PATTERN', () => {
  const forwards = (url: string) => new RegExp(API_PATH_PATTERN).test(url);

  it.each(['/classes', '/classes/', '/files', '/files/abc', '/ask', '/ask?x=1', '/health'])(
    'forwards the API path %s',
    (url) => {
      expect(forwards(url)).toBe(true);
    },
  );

  it.each([
    '/',
    '/index.html',
    '/src/main.tsx',
    '/@vite/client',
    '/classesroom',
    '/filesystem.svg',
    '/asking',
    '/healthz',
    '/api/classes',
    '/src/files/x',
  ])('does not forward %s', (url) => {
    expect(forwards(url)).toBe(false);
  });
});

describe('apiProxy', () => {
  it('forwards the API paths to the default target when the variable is unset', () => {
    expect(apiProxy({})).toStrictEqual({ [API_PATH_PATTERN]: { target: DEFAULT_API_TARGET } });
  });

  it('forwards them to the variable when it is set, and refuses a bad one', () => {
    expect(apiProxy({ [API_TARGET_ENV]: 'http://127.0.0.1:8001' })).toStrictEqual({
      [API_PATH_PATTERN]: { target: 'http://127.0.0.1:8001' },
    });
    expect(() => apiProxy({ [API_TARGET_ENV]: 'http://127.0.0.1:8001/api' })).toThrow(
      API_TARGET_ENV,
    );
  });

  describe('as vite.config.ts installs it', () => {
    afterEach(() => {
      vi.unstubAllEnvs();
    });

    // The config is re-imported after each stub: it reads the environment when it is evaluated.
    const loadConfig = async () => {
      vi.resetModules();
      return (await import('./vite.config.ts')).default;
    };

    it('forwards to GCT_API_TARGET from the environment when it is set', async () => {
      vi.stubEnv('GCT_API_TARGET', 'http://127.0.0.1:8001');
      expect((await loadConfig()).server?.proxy).toStrictEqual({
        [API_PATH_PATTERN]: { target: 'http://127.0.0.1:8001' },
      });
    });

    it('forwards to the default when GCT_API_TARGET is unset', async () => {
      vi.stubEnv('GCT_API_TARGET', undefined);
      expect((await loadConfig()).server?.proxy).toStrictEqual({
        [API_PATH_PATTERN]: { target: 'http://127.0.0.1:8000' },
      });
    });
  });
});

// The rule above run through Vite itself, against a stand-in upstream: this is what makes "a `^`
// key is a RegExp to Vite" and "the Host header passes through" tested facts, not comments.
describe('the proxy, live', () => {
  const seen: { method: string; url: string; host: string; body: string }[] = [];
  let upstream: Server;
  let vite: ViteDevServer;
  let origin: string;

  beforeAll(async () => {
    upstream = createHttpServer((req: IncomingMessage, res) => {
      let body = '';
      req.on('data', (chunk: Buffer) => (body += chunk.toString()));
      req.on('end', () => {
        seen.push({
          method: req.method ?? '',
          url: req.url ?? '',
          host: req.headers.host ?? '',
          body,
        });
        res.writeHead(200, { 'content-type': 'application/json' }).end('{"from":"upstream"}');
      });
    });
    await new Promise<void>((resolve) => upstream.listen(0, '127.0.0.1', resolve));
    const { port } = upstream.address() as AddressInfo;

    vite = await createServer({
      configFile: false,
      logLevel: 'silent',
      server: {
        host: '127.0.0.1',
        port: 0,
        proxy: apiProxy({ [API_TARGET_ENV]: `http://127.0.0.1:${port}` }),
      },
    });
    await vite.listen();
    const address = vite.httpServer?.address() as AddressInfo;
    origin = `http://127.0.0.1:${address.port}`;
  });

  afterAll(async () => {
    await vite.close();
    await new Promise((resolve) => upstream.close(resolve));
  });

  it('forwards an API route with its method, path, query, body and Host', async () => {
    const res = await fetch(`${origin}/files/abc?x=1`, { method: 'POST', body: 'payload' });
    expect(await res.json()).toStrictEqual({ from: 'upstream' });
    expect(seen.at(-1)).toStrictEqual({
      method: 'POST',
      url: '/files/abc?x=1',
      host: new URL(origin).host,
      body: 'payload',
    });
  });

  it('does not forward a path that only starts like one', async () => {
    const before = seen.length;
    const res = await fetch(`${origin}/filesystem.svg`);
    await res.arrayBuffer();
    expect(seen).toHaveLength(before);
  });
});
