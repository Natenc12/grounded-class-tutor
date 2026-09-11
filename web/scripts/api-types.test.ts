// The generated half of the drift pin (ADR 0033): the committed TypeScript is exactly what the
// generator makes from the committed snapshot. The other half - the snapshot vs the live API -
// is tests/gct/api/test_openapi_snapshot.py, which CI runs.
import { execFile } from 'node:child_process';
import { copyFile, mkdir, mkdtemp, readFile, rm, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

import { describe, expect, it } from 'vitest';

import { GENERATED_URL, SNAPSHOT_URL, generate } from './api-types.mjs';

const WEB = fileURLToPath(new URL('..', import.meta.url));

describe('src/api/schema.gen.ts', () => {
  it('is what the generator makes from openapi.json', async () => {
    const expected = await generate(await readFile(SNAPSHOT_URL, 'utf8'));
    const committed = await readFile(GENERATED_URL, 'utf8');
    expect(
      committed === expected,
      'src/api/schema.gen.ts is not generated from openapi.json. Run `npm run api:refresh` and commit both files.',
    ).toBe(true);
  });

  it('is rewritten by the refresh script', async () => {
    // A copy of web/'s layout, so the script's paths (relative to itself) land in a temp dir.
    const root = await mkdtemp(join(tmpdir(), 'gct-api-types-'));
    try {
      await mkdir(join(root, 'scripts'));
      await mkdir(join(root, 'src', 'api'), { recursive: true });
      for (const name of ['api-types.mjs', 'refresh-api-types.mjs']) {
        await copyFile(join(WEB, 'scripts', name), join(root, 'scripts', name));
      }
      await copyFile(join(WEB, 'openapi.json'), join(root, 'openapi.json'));
      await symlink(join(WEB, 'node_modules'), join(root, 'node_modules'), 'dir');

      await promisify(execFile)(process.execPath, [join(root, 'scripts', 'refresh-api-types.mjs')]);

      const written = await readFile(join(root, 'src', 'api', 'schema.gen.ts'), 'utf8');
      expect(written).toBe(await generate(await readFile(SNAPSHOT_URL, 'utf8')));
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });
});
