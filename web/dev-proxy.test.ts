import { createServer as createHttpServer, type IncomingMessage, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';

import { createServer, type ViteDevServer } from 'vite';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';

import {
  API_PATH_PATTERN,
  API_TARGET_ENV,
  DEFAULT_API_TARGET,
  apiProxy,
  apiTarget,
} from './dev-proxy.ts';
import config from './vite.config.ts';

describe('apiTarget', () => {
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

  it.each([
    ['empty', '', 'is not a URL'],
    ['no scheme', '127.0.0.1:8001', 'is not a URL'],
    ['a host read as a scheme', 'localhost:8001', 'is not an http or https URL'],
    ['garbage', 'not a url', 'is not a URL'],
    ['another scheme', 'ftp://127.0.0.1:8001', 'is not an http or https URL'],
    ['a websocket', 'ws://127.0.0.1:8001', 'is not an http or https URL'],
    ['a path', 'http://127.0.0.1:8001/api', 'has more than an origin'],
    ['a query', 'http://127.0.0.1:8001/?x=1', 'has more than an origin'],
    ['a fragment', 'http://127.0.0.1:8001/#x', 'has more than an origin'],
    ['a login', 'http://user:pw@127.0.0.1:8001', 'has more than an origin'],
  ])('refuses %s, naming the variable and the remedy', (_, value, problem) => {
    expect(() => apiTarget(value)).toThrow(problem);
    expect(() => apiTarget(value)).toThrow(`${API_TARGET_ENV}=${JSON.stringify(value)}`);
    expect(() => apiTarget(value)).toThrow("Set it to the API's origin only");
  });
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

  it('is what vite.config.ts installs', () => {
    expect(config.server?.proxy).toStrictEqual(apiProxy(process.env));
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
