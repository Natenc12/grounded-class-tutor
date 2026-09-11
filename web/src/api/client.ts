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
  /** `getFileStatus('')`: there is no id, and `/files/` would be a trailing slash. Nothing is sent. */
  missingFileId: 'client.missing_file_id',
} as const;

// Appended to every client-minted message on `POST /files`. Measured through the Vite dev proxy:
// an upload over the API's size bound gets either the API's own 413 `body_too_large` or, from
// run to run, a fetch rejection - the proxy resets the connection while the body is still being
// sent. So "the server is down" must never be the only reading an upload is given.
const UPLOAD_HINT =
  ' If the file is large, it may be over the upload size limit: try a smaller file.';

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
    send<T>(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

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
      if (fileId === '') {
        return Promise.resolve(
          failure(
            null,
            CLIENT_KINDS.missingFileId,
            'There is no file id to look up, so nothing was sent. Upload the file again to get one.',
          ),
        );
      }
      const path = ('/files/{file_id}' satisfies ApiPath).replace(
        '{file_id}',
        encodeURIComponent(fileId),
      );
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
    return failure(response.status, CLIENT_KINDS.network, networkMessage(hint));
  }
  const body = parseJson(text);

  if (response.ok) {
    if (isRecord(body)) {
      // The shape is not re-checked field by field: the API serialises through the very models
      // `types.ts` is generated from, and a snapshot test pins the two together.
      return { ok: true, status: response.status, data: body as T };
    }
    return failure(
      response.status,
      CLIENT_KINDS.badResponse,
      badResponseMessage(response.status, hint),
    );
  }
  const error = parseEnvelope(body);
  if (error !== null) {
    return { ok: false, status: response.status, error };
  }
  // Every 5xx the app produces is an envelope (api.md, *The error envelope*), so a 5xx without
  // one was written by something in front of it - the dev proxy answers 502 with an empty body
  // when uvicorn is not there.
  if (response.status >= 500) {
    return failure(
      response.status,
      CLIENT_KINDS.apiUnreachable,
      `The tutor's API did not answer; a server in front of it replied instead (HTTP ${response.status}). Check that the API is running, then try again.${hint}`,
    );
  }
  return failure(
    response.status,
    CLIENT_KINDS.badResponse,
    badResponseMessage(response.status, hint),
  );
}

function networkMessage(hint: string): string {
  return `The connection to the tutor's server failed before it answered. Check that the server is running and that you are online, then try again.${hint}`;
}

function badResponseMessage(status: number, hint: string): string {
  return `The server's reply (HTTP ${status}) is not one this page understands, so it cannot be shown. Reload the page and try again; if it keeps happening, the page and the API may be out of step.${hint}`;
}

function failure(status: number | null, kind: string, message: string): ApiResult<never> {
  return { ok: false, status, error: { kind, message, detail: null } };
}

/** The parsed body, or `undefined` when the text is not JSON (an empty body included). */
function parseJson(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}
