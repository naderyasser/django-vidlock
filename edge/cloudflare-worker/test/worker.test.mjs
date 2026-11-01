// npm test (node --test test/*.test.mjs)
import assert from 'node:assert/strict';
import { beforeEach, test } from 'node:test';

import worker, { parseRange, valid } from '../src/index.js';
import { FakeBucket, FakeCache, context, sign } from './fakes.mjs';

const BASE = 'https://media.example';
const KEY = 'lessons/1/0123456789abcdef.ts';
const SECRET = 'new-secret-0123456789abcdef0123456789';
const OLD = 'old-secret-0123456789abcdef0123456789';
const BYTES = Uint8Array.from({ length: 10_000 }, (_, i) => i % 251);

let env;
let cache;

beforeEach(() => {
  cache = new FakeCache();
  globalThis.caches = { default: cache };
  env = {
    BUCKET: new FakeBucket({ [KEY]: BYTES, 'lessons/1/source.mp4': BYTES, 'lessons/1/a.poster.jpg': BYTES }),
    VIDLOCK_EDGE_SECRETS: `${SECRET},${OLD}`,
    ALLOWED_ORIGINS: 'https://courses.example',
  };
});

async function get(url, headers = {}, method = 'GET') {
  const ctx = context();
  const response = await worker.fetch(new Request(url, { method, headers }), env, ctx);
  await ctx.settle();
  return response;
}

test('a signed range is served as 206, then from the cache', async () => {
  const url = await sign(BASE, KEY, SECRET);
  const first = await get(url, { Range: 'bytes=1000-1999', Origin: 'https://courses.example' });
  assert.equal(first.status, 206);
  assert.equal(first.headers.get('Content-Range'), 'bytes 1000-1999/10000');
  assert.equal(first.headers.get('Content-Length'), '1000');
  assert.equal(first.headers.get('Accept-Ranges'), 'bytes');
  assert.equal(first.headers.get('x-vidlock-cache'), 'MISS');
  assert.match(first.headers.get('x-vidlock-edge'), /^v1; cache=MISS/);
  assert.deepEqual(new Uint8Array(await first.arrayBuffer()), BYTES.slice(1000, 2000));
  assert.equal(env.BUCKET.gets, 1);

  // Another viewer, another token: the same cache entry.
  const other = await sign(BASE, KEY, SECRET, { viewer: 'fedcba987654' });
  const second = await get(other, { Range: 'bytes=1000-1999' });
  assert.equal(second.status, 206);
  assert.equal(second.headers.get('x-vidlock-cache'), 'HIT');
  assert.equal(second.headers.get('Content-Range'), 'bytes 1000-1999/10000');
  assert.deepEqual(new Uint8Array(await second.arrayBuffer()), BYTES.slice(1000, 2000));
  assert.equal(env.BUCKET.gets, 1, 'the hit never reached R2');
});

test('the cache key leaves the token out', async () => {
  await get(await sign(BASE, KEY, SECRET), { Range: 'bytes=0-99' });
  assert.deepEqual(cache.puts, [`${BASE}/__vidlock-cache/${KEY}?r=0-99`]);
  assert.ok(!cache.puts[0].includes('sig='));
});

test('tampered, expired, unsigned and foreign-secret URLs are refused before R2', async () => {
  const url = await sign(BASE, KEY, SECRET);
  const tampered = url.replace(/sig=([0-9a-f])/, (m, c) => `sig=${c === '0' ? '1' : '0'}`);
  const otherKey = url.replace(KEY, 'lessons/1/ffffffffffffffff.ts');
  const otherViewer = url.replace('v=abc123def456', 'v=000000000000');
  const expired = await sign(BASE, KEY, SECRET, { ttl: -10 });
  const tooLong = await sign(BASE, KEY, SECRET, { ttl: 8 * 24 * 3600 });
  const foreign = await sign(BASE, KEY, 'somebody-else-0123456789abcdef0123');
  for (const bad of [tampered, otherKey, otherViewer, expired, tooLong, foreign, `${BASE}/${KEY}`]) {
    const response = await get(bad, { Range: 'bytes=0-99' });
    assert.equal(response.status, 403, bad);
    assert.equal(response.headers.get('Cache-Control'), 'no-store');
  }
  assert.equal(env.BUCKET.gets, 0);
});

test('an old secret still works while it is listed, and not after', async () => {
  const url = await sign(BASE, KEY, OLD);
  assert.equal((await get(url, { Range: 'bytes=0-9' })).status, 206);
  env.VIDLOCK_EDGE_SECRETS = SECRET;
  assert.equal((await get(url, { Range: 'bytes=10-19' })).status, 403);
});

test('only sealed .ts objects: no source MP4, no previews, no tricks', async () => {
  for (const key of ['lessons/1/source.mp4', 'lessons/1/a.poster.jpg', 'lessons//x.ts']) {
    const response = await get(await sign(BASE, key, SECRET), { Range: 'bytes=0-9' });
    assert.equal(response.status, 404, key);
  }
  // URL parsing folds '..' away before the Worker sees it; the signature, made
  // over the raw key, then no longer matches.
  for (const key of ['lessons/../x.ts', 'lessons/%2e%2e/x.ts']) {
    const response = await get(await sign(BASE, key, SECRET), { Range: 'bytes=0-9' });
    assert.ok([403, 404].includes(response.status), `${key}: ${response.status}`);
  }
  assert.equal(env.BUCKET.gets, 0);
});

test('a missing object is 404; a range past the end is 416', async () => {
  const missing = await sign(BASE, 'lessons/1/0000000000000000.ts', SECRET);
  assert.equal((await get(missing, { Range: 'bytes=0-9' })).status, 404);
  const url = await sign(BASE, KEY, SECRET);
  const past = await get(url, { Range: 'bytes=20000-20099' });
  assert.equal(past.status, 416);
  assert.equal(past.headers.get('Content-Range'), 'bytes */10000');
  assert.equal((await get(url, { Range: 'bytes=5-1' })).status, 416);
  assert.equal((await get(url, { Range: 'bytes=0-1,5-9' })).status, 416);
});

test('a range running past the end is cut to the object', async () => {
  const response = await get(await sign(BASE, KEY, SECRET), { Range: 'bytes=9990-10099' });
  assert.equal(response.status, 206);
  assert.equal(response.headers.get('Content-Range'), 'bytes 9990-9999/10000');
  assert.equal((await response.arrayBuffer()).byteLength, 10);
});

test('open-ended, suffix and whole-file reads go straight to R2, uncached', async () => {
  const url = await sign(BASE, KEY, SECRET);
  const open = await get(url, { Range: 'bytes=9000-' });
  assert.equal(open.status, 206);
  assert.equal(open.headers.get('Content-Range'), 'bytes 9000-9999/10000');
  assert.equal(open.headers.get('x-vidlock-cache'), 'BYPASS');
  const suffix = await get(url, { Range: 'bytes=-100' });
  assert.equal(suffix.headers.get('Content-Range'), 'bytes 9900-9999/10000');
  assert.equal((await suffix.arrayBuffer()).byteLength, 100);
  const whole = await get(url);
  assert.equal(whole.status, 200);
  assert.equal(whole.headers.get('Content-Length'), '10000');
  assert.equal((await whole.arrayBuffer()).byteLength, 10000);
  assert.deepEqual(cache.puts, []);
});

test('HEAD answers the size', async () => {
  const response = await get(await sign(BASE, KEY, SECRET), {}, 'HEAD');
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('Content-Length'), '10000');
  assert.equal(env.BUCKET.gets, 0);
});

test('CORS: the allowed origin only, never a wildcard', async () => {
  const url = await sign(BASE, KEY, SECRET);
  const allowed = await get(url, { Range: 'bytes=0-9', Origin: 'https://courses.example' });
  assert.equal(allowed.headers.get('Access-Control-Allow-Origin'), 'https://courses.example');
  assert.match(allowed.headers.get('Access-Control-Expose-Headers'), /Content-Range/);
  assert.equal(allowed.headers.get('Vary'), 'Origin');
  const stranger = await get(url, { Range: 'bytes=0-9', Origin: 'https://evil.example' });
  assert.equal(stranger.headers.get('Access-Control-Allow-Origin'), null);
  env.ALLOWED_ORIGINS = '*';
  const wildcard = await get(url, { Range: 'bytes=0-9', Origin: 'https://evil.example' });
  assert.equal(wildcard.headers.get('Access-Control-Allow-Origin'), null, "'*' is ignored");

  env.ALLOWED_ORIGINS = 'https://courses.example';
  const ok = await get(url, { Origin: 'https://courses.example' }, 'OPTIONS');
  assert.equal(ok.status, 204);
  assert.equal(ok.headers.get('Access-Control-Allow-Headers'), 'Range');
  assert.equal((await get(url, { Origin: 'https://evil.example' }, 'OPTIONS')).status, 403);
});

test('media headers: private browser caching, nosniff, the debug header', async () => {
  const response = await get(await sign(BASE, KEY, SECRET), { Range: 'bytes=0-9' });
  assert.equal(response.headers.get('Cache-Control'), 'private, max-age=3600');
  assert.equal(response.headers.get('X-Content-Type-Options'), 'nosniff');
  assert.equal(response.headers.get('Content-Type'), 'video/mp2t');
  assert.ok(response.headers.get('x-vidlock-edge'));
});

test('other methods are refused', async () => {
  const response = await get(await sign(BASE, KEY, SECRET), {}, 'POST');
  assert.equal(response.status, 405);
});

test('storage errors become a 502, not a crash', async () => {
  env.BUCKET.get = async () => {
    throw new Error('R2 down');
  };
  const original = console.error;
  console.error = () => {};
  try {
    const response = await get(await sign(BASE, KEY, SECRET), { Range: 'bytes=0-9' });
    assert.equal(response.status, 502);
  } finally {
    console.error = original;
  }
});

test('parseRange', () => {
  assert.deepEqual(parseRange('bytes=0-99'), { start: 0, end: 99 });
  assert.deepEqual(parseRange('bytes=5-'), { start: 5 });
  assert.deepEqual(parseRange('bytes=-5'), { suffix: 5 });
  assert.equal(parseRange(null), null);
  assert.equal(typeof parseRange('bytes=-'), 'symbol');
  assert.equal(typeof parseRange('items=0-1'), 'symbol');
});

test('valid() rejects malformed parameters without trying a secret', async () => {
  for (const [exp, viewer, sig] of [
    ['', '', 'a'.repeat(64)],
    ['12x', '', 'a'.repeat(64)],
    ['9999999999', 'NOT-HEX', 'a'.repeat(64)],
    ['9999999999', '', 'A'.repeat(64)],
    ['9999999999', '', 'a'.repeat(63)],
  ]) {
    assert.equal(await valid(env, KEY, exp, viewer, sig), false);
  }
});
