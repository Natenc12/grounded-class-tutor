// Rewrites `src/api/schema.gen.ts` from `openapi.json`. Run by `npm run api:refresh`, after the
// snapshot itself is rewritten; `api-types.mjs` owns what the output is.
import { readFile, writeFile } from 'node:fs/promises';

import { GENERATED_URL, SNAPSHOT_URL, generate } from './api-types.mjs';

await writeFile(GENERATED_URL, await generate(await readFile(SNAPSHOT_URL, 'utf8')));
console.log(`wrote ${GENERATED_URL.pathname}`);
