// One request through the Worker, with a fake R2 bucket holding `key` (1000
// bytes) and an in-memory cache. Used by Python's tests (tests/test_edge.py) to
// check that URLs signed by vidlock.edge pass the real Worker code.
//
//   node serve-once.mjs '{"url": ..., "secrets": "a,b", "key": ..., "requests": [{"range": "bytes=0-9"}]}'
import worker from '../src/index.js';
import { FakeBucket, FakeCache, context } from './fakes.mjs';

const spec = JSON.parse(process.argv[2]);
globalThis.caches = { default: new FakeCache() };
const bucket = new FakeBucket({ [spec.key]: Uint8Array.from({ length: 1000 }, (_, i) => i % 256) });
const env = { BUCKET: bucket, VIDLOCK_EDGE_SECRETS: spec.secrets, ALLOWED_ORIGINS: spec.origins || '' };
const out = [];
for (const one of spec.requests || [{}]) {
  const headers = {};
  if (one.range) headers.Range = one.range;
  if (one.origin) headers.Origin = one.origin;
  const ctx = context();
  const response = await worker.fetch(new Request(one.url || spec.url, { headers }), env, ctx);
  const body = new Uint8Array(await response.arrayBuffer());
  await ctx.settle();
  out.push({ status: response.status, headers: Object.fromEntries(response.headers), bytes: body.length });
}
console.log(JSON.stringify({ responses: out, r2_gets: bucket.gets }));
