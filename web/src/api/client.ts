// The typed API client: the four routes design/components/api.md ships, and nothing else.
//
// Every call RESOLVES to an `ApiResult` and never rejects: a network failure, an envelope and a
// reply that is neither all come back as `{ok: false}`, so a surface cannot forget the failure
// branch - TypeScript will not let it read `data` without checking `ok`. A 200 from `POST /ask`
// is `ok` in all four grounding states, REFUSAL included (ADR 0016); only the envelope the
// route answers ERROR with (503/500) is a failure.
//
// The client never sends an owner id and never a field beyond the request model's: the V1 owner
// is server-side (ADR 0004), and both JSON models forbid extra fields, so either would be a 422.
import { isRecord, parseEnvelope, type ApiError } from './envelope';
import type {
  ApiPath,
  AskRequest,
  AskResponse,
  ClassCreated,
  FileStatusResponse,
  NewClass,
  UploadAccepted,
  UploadForm,
} from './types';

export type { ApiError } from './envelope';

export type ApiResult<T> =
  | { readonly ok: true; readonly status: number; readonly data: T }
  // `status` is null when no HTTP status line arrived at all.
  | { readonly ok: false; readonly status: number | null; readonly error: ApiError };

// The kinds this client mints, for failures that carry no envelope. Namespaced `client.` so none
// can equal a kind the API mints: those are bare tokens (`class_not_found`, `body_too_large`).
export const CLIENT_KINDS = {
  /** No complete reply: the request or the reply's body failed on the network. */
  network: 'client.network_error',
  /** A 5xx that is not the API's envelope: something in front of the API answered for it. */
  apiUnreachable: 'client.api_unreachable',
  /** Any other reply the contract does not describe - a non-envelope 4xx, a 2xx that is not a JSON object. */
  badResponse: 'client.bad_response',
  /** A file id no URL path can carry (see `pathSegment`). Nothing is sent. */
  unsendableFileId: 'client.unsendable_file_id',
} as const;

// Appended to every failure the client names itself on a route that SENDS A BODY, except a 2xx
// reply (a 2xx means the body was not refused). Measured through the Vite dev proxy: a body over
// the API's size bound - an upload, or JSON - gets either the API's own 413 `body_too_large` or,
// from run to run, a connection reset or a bare 502, because uvicorn answers and closes while the
// proxy is still sending the body. The client cannot tell that from an API that is down, so it
// names both remedies and asserts neither cause. GET sends no body and gets no hint.
const UPLOAD_HINT =
  ' If the file is large, it may be over the upload size limit: try a smaller file.';
const JSON_HINT = ' If you sent a lot of text, it may be over the request size limit: send less.';

export interface ApiClientOptions {
  /** Prepended to every path; no trailing slash. Default `''`: same origin, so a page goes through the dev proxy. */
  readonly baseUrl?: string;
  /** Default: the global `fetch`. Injected by tests. */
  readonly fetch?: typeof fetch;
}

export interface ApiClient {
  /** `POST /classes` -> 201 */
  createClass(name: string): Promise<ApiResult<ClassCreated>>;
  /** `POST /files` -> 202. The file is ACCEPTED, not ingested: poll `getFileStatus`. */
  uploadFile(classId: string, file: File): Promise<ApiResult<UploadAccepted>>;
  /** `GET /files/{file_id}` -> 200, for every status including `failed`. */
  getFileStatus(fileId: string): Promise<ApiResult<FileStatusResponse>>;
  /** `POST /ask` -> 200 in all four grounding states. */
  ask(classId: string, question: string): Promise<ApiResult<AskResponse>>;
}

export function createApiClient(options: ApiClientOptions = {}): ApiClient {
  const baseUrl = options.baseUrl ?? '';
  if (baseUrl.endsWith('/')) {
    throw new TypeError(
      `baseUrl must not end with "/" (got ${JSON.stringify(baseUrl)}): every path starts with one.`,
    );
  }
  // Looked up at call time, not captured: a bare `window.fetch` reference called as a method of
  // another object throws "Illegal invocation" in a browser.
  const fetchImpl = options.fetch ?? ((input, init) => fetch(input, init));

  const send = <T>(path: string, init: RequestInit, hint = '') =>
    request<T>(fetchImpl, baseUrl + path, init, hint);

  const sendJson = <T>(path: ApiPath, body: NewClass | AskRequest) =>
    send<T>(
      path,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      },
      JSON_HINT,
    );

  return {
    createClass: (name) => sendJson<ClassCreated>('/classes', { name } satisfies NewClass),

    uploadFile: (classId, file) => {
      const form = new FormData();
      form.append('class_id' satisfies keyof UploadForm, classId);
      form.append('file' satisfies keyof UploadForm, file);
      // No Content-Type: the runtime writes `multipart/form-data` WITH the boundary. A hand-set
      // header has no boundary, and the API cannot parse the body.
      return send<UploadAccepted>(
        '/files' satisfies ApiPath,
        { method: 'POST', body: form },
        UPLOAD_HINT,
      );
    },

    getFileStatus: (fileId) => {
      const segment = pathSegment(fileId);
      if (segment === null) {
        return Promise.resolve(
          failure(
            null,
            CLIENT_KINDS.unsendableFileId,
            `The file id ${JSON.stringify(fileId)} cannot be sent as part of a web address, so nothing was sent. Use the file id returned when the file was uploaded.`,
          ),
        );
      }
      const path = ('/files/{file_id}' satisfies ApiPath).replace('{file_id}', segment);
      return send<FileStatusResponse>(path, { method: 'GET' });
    },

    ask: (classId, question) =>
      sendJson<AskResponse>('/ask', { class_id: classId, question } satisfies AskRequest),
  };
}

async function request<T>(
  fetchImpl: typeof fetch,
  url: string,
  init: RequestInit,
  hint: string,
): Promise<ApiResult<T>> {
  let response: Response;
  try {
    response = await fetchImpl(url, init);
  } catch {
    return failure(null, CLIENT_KINDS.network, networkMessage(hint));
  }
  let text: string;
  try {
    text = await response.text();
  } catch {
    // No size hint after a 2xx: the status line already said the body was accepted.
    return failure(response.status, CLIENT_KINDS.network, networkMessage(response.ok ? '' : hint));
  }
  const body = parseJson(text);

  if (response.ok) {
    if (isRecord(body)) {
      // The shape is not re-checked field by field: the API serialises through the very models
      // `types.ts` is generated from, and a snapshot test pins the two together.
      return { ok: true, status: response.status, data: body as T };
    }
    // No size hint: a 2xx means whatever answered did not refuse the body.
    return failure(
      response.status,
      CLIENT_KINDS.badResponse,
      badResponseMessage(response.status, ''),
    );
  }
  const error = parseEnvelope(body);
  if (error !== null) {
    return { ok: false, status: response.status, error };
  }
  // Every 5xx the app produces is an envelope (api.md, *The error envelope*), so a 5xx without
  // one was written by something in front of it - the dev proxy answers 502 with an empty body
  // when uvicorn is not there, and also when it loses the API's reply to an oversize body.
  if (response.status >= 500) {
    return failure(
      response.status,
      CLIENT_KINDS.apiUnreachable,
      `A server in front of the tutor's API replied instead of the API (HTTP ${response.status}). If the API is not running, start it and try again.${hint}`,
    );
  }
  return failure(
    response.status,
    CLIENT_KINDS.badResponse,
    badResponseMessage(response.status, hint),
  );
}

function networkMessage(hint: string): string {
  return `The connection to the tutor's server failed before an answer arrived. If the server is not running or you are offline, fix that and try again.${hint}`;
}

function badResponseMessage(status: number, hint: string): string {
  return `The server's reply (HTTP ${status}) is not one this page understands, so it cannot be shown. Reload the page and try again; if it keeps happening, the page and the API may be out of step.${hint}`;
}

function failure(status: number | null, kind: string, message: string): ApiResult<never> {
  return { ok: false, status, error: { kind, message, detail: null } };
}

// `value` as one URL path segment, or null when no spelling of it survives as one. `''` would
// leave `/files/`, a trailing slash. `.` and `..` are dot segments the URL parser removes before
// the request goes out (`/files/..` is `/`), and percent-encoding them does not help: `%2e` is a
// dot segment too. `encodeURIComponent` leaves dots alone and encodes every `%`, so `.` and `..`
// are the only dot segments it can produce. A lone surrogate has no UTF-8 encoding, and
// `encodeURIComponent` throws on it. Anything else - whitespace included - is sent, and the API's
// own `bad_file_id` is the answer about whether it is an id.
function pathSegment(value: string): string | null {
  let encoded: string;
  try {
    encoded = encodeURIComponent(value);
  } catch {
    return null;
  }
  return encoded === '' || encoded === '.' || encoded === '..' ? null : encoded;
}

/** The parsed body, or `undefined` when the text is not JSON (an empty body included). */
function parseJson(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}
