// The error side of the API contract, hand-written from design/components/api.md's error-envelope
// section. It cannot be generated like the success side (`types.ts`): the published schema
// advertises FastAPI's default 422 body and no other error status, while the app answers EVERY
// non-2xx it produces with `{error: {kind, message, detail}}`.

/**
 * One failure, whoever produced it - the API's envelope, or the client for a failure that
 * carried none (`client.ts`). A surface switches on `kind` and shows `message`, which names the
 * remedy.
 */
export interface ApiError {
  /** Open on purpose: every route mints its own kinds, and a kind this client has never heard of is still an answer. */
  readonly kind: string;
  readonly message: string;
  /** Structure, when the producer has any: pydantic's per-field list on a 422. Otherwise null. */
  readonly detail: unknown;
}

/** The envelope's `error`, or null when `body` is not an envelope. Extra keys are ignored. */
export function parseEnvelope(body: unknown): ApiError | null {
  if (!isRecord(body) || !isRecord(body.error)) {
    return null;
  }
  const { kind, message, detail } = body.error;
  if (typeof kind !== 'string' || typeof message !== 'string') {
    return null;
  }
  // `detail` defaults to null in the API's own model (`ErrorBody`), so an absent key IS null;
  // any value that is present is passed through untouched.
  return { kind, message, detail: detail ?? null };
}

/** A JSON object: not null, not an array. */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
