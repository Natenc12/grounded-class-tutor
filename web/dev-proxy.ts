// The dev-time origin (ADR 0032): the Vite dev server forwards the API's own paths to uvicorn, so
// the page and the API share one origin and `src/gct/api` needs no CORS middleware.
import type { ProxyOptions } from 'vite';

// The API's route roots, spelt as design/components/api.md spells them, so a path in the client
// is the path in the spec. No `/api` prefix and no rewrite: there is one spelling of each route.
export const API_ROUTE_ROOTS = ['/classes', '/files', '/ask', '/health'] as const;

// A root, then a `/`, a `?` or the end - and nothing else. Vite matches a plain key as a string
// PREFIX, so a plain `/files` key would also forward `/filesystem.svg` to the API. A key that
// starts with `^` is a RegExp to Vite; this one forwards `/files/<id>` and `/ask?x` but never a
// longer name that happens to start with a root.
export const API_PATH_PATTERN = `^(?:${API_ROUTE_ROOTS.join('|')})(?:[/?]|$)`;

// uvicorn's default bind is the IPv4 loopback. `localhost` is not used: Node may resolve it to
// `::1` first, where uvicorn is not listening.
export const DEFAULT_API_TARGET = 'http://127.0.0.1:8000';

// Points the proxy at another API - a second uvicorn on another port, against a scratch
// database - without editing this file.
export const API_TARGET_ENV = 'GCT_API_TARGET';

// The upstream the proxy forwards to: the default when the variable is unset, otherwise the
// variable's value, which must be an http(s) ORIGIN. Anything else is refused with the remedy,
// never repaired: a path on the target would be prepended to every API route, so
// `http://127.0.0.1:8001/api` would silently send every call to a 404.
export function apiTarget(value: string | undefined): string {
  if (value === undefined) {
    return DEFAULT_API_TARGET;
  }
  const url = URL.parse(value);
  if (url === null) {
    throw new Error(refusal(value, 'is not a URL'));
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new Error(refusal(value, 'is not an http or https URL'));
  }
  // An origin's href is the origin plus `/`. A path, a query, a fragment or credentials all
  // make the two differ, and each of them would change what the proxy sends.
  if (url.href !== `${url.origin}/`) {
    throw new Error(refusal(value, 'has more than an origin (a path, query, fragment or login)'));
  }
  return url.origin;
}

function refusal(value: string, problem: string): string {
  return (
    `${API_TARGET_ENV}=${JSON.stringify(value)} ${problem}. Set it to the API's origin only, ` +
    `for example ${API_TARGET_ENV}=http://127.0.0.1:8001, or unset it to use ${DEFAULT_API_TARGET}.`
  );
}

// `server.proxy` for vite.config.ts. The Host header is passed through unchanged (no
// `changeOrigin`), so a URL the API builds from it points back through this proxy.
export function apiProxy(env: Record<string, string | undefined>): Record<string, ProxyOptions> {
  return { [API_PATH_PATTERN]: { target: apiTarget(env[API_TARGET_ENV]) } };
}
