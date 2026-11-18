import { describe, expect, it } from 'vitest';

import { isRecord, parseEnvelope } from './envelope';

const envelope = (error: unknown) => ({ error });

describe('parseEnvelope', () => {
  it('reads kind, message and detail out of an envelope', () => {
    expect(
      parseEnvelope(envelope({ kind: 'class_not_found', message: 'No such class.', detail: null })),
    ).toStrictEqual({ kind: 'class_not_found', message: 'No such class.', detail: null });
  });

  it('passes a present detail through untouched, falsy values included', () => {
    const list = [{ type: 'missing', loc: ['body', 'name'], msg: 'Field required', input: {} }];
    for (const detail of [list, 0, false, '', 'text']) {
      expect(parseEnvelope(envelope({ kind: 'k', message: 'm', detail }))?.detail).toBe(detail);
    }
  });

  it('reads an absent detail as null, the default of the API model', () => {
    const parsed = parseEnvelope(envelope({ kind: 'k', message: 'm' }));
    expect(parsed).toStrictEqual({ kind: 'k', message: 'm', detail: null });
  });

  it('ignores keys it does not know', () => {
    expect(
      parseEnvelope({ error: { kind: 'k', message: 'm', detail: null, extra: 1 }, also: 2 }),
    ).toStrictEqual({ kind: 'k', message: 'm', detail: null });
  });

  it('accepts a kind it has never heard of', () => {
    expect(parseEnvelope(envelope({ kind: 'a_brand_new_kind', message: 'm' }))?.kind).toBe(
      'a_brand_new_kind',
    );
  });

  it.each([
    ['null', null],
    ['undefined (a body that was not JSON)', undefined],
    ['a string', 'Internal Server Error'],
    ['a number', 502],
    ['an array', [{ error: { kind: 'k', message: 'm' } }]],
    ["FastAPI's default 422 body, which the schema advertises", { detail: [{ msg: 'x' }] }],
  ])('is null for a body that is %s', (_, body) => {
    expect(parseEnvelope(body)).toBeNull();
  });

  it.each([
    ['null', null],
    ['a string', 'boom'],
    ['an array', [{ kind: 'k', message: 'm' }]],
  ])('is null when error is %s', (_, error) => {
    expect(parseEnvelope(envelope(error))).toBeNull();
  });

  it('is null unless BOTH kind and message are strings', () => {
    expect(parseEnvelope(envelope({ kind: 7, message: 'm' }))).toBeNull();
    expect(parseEnvelope(envelope({ kind: 'k', message: 7 }))).toBeNull();
    expect(parseEnvelope(envelope({ message: 'm' }))).toBeNull();
    expect(parseEnvelope(envelope({ kind: 'k' }))).toBeNull();
    expect(parseEnvelope(envelope({ kind: 'k', message: 'm' }))).not.toBeNull();
  });
});

describe('isRecord', () => {
  it('is true for a JSON object and false for everything else JSON can be', () => {
    expect(isRecord({})).toBe(true);
    expect(isRecord({ a: 1 })).toBe(true);
    for (const value of [null, [], [1], 'x', 1, true, undefined]) {
      expect(isRecord(value)).toBe(false);
    }
  });
});
