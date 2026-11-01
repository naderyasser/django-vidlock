// vidlock edge: sealed media from R2, through Cloudflare's cache.
//
// Serves byte ranges of the encrypted .ts files vidlock writes, read through
// an R2 *binding* (the bucket stays private), to holders of a short-lived URL
// signed by Django (vidlock.edge):
//
//   https://media.example/<object key>?exp=<unix>&v=<viewer tag>&sig=<hex HMAC-SHA256>
//
// Each range is cached under a key without the token, so every viewer shares
// the cache: safe because the bytes are AES-128 encrypted and the keys never
// come here. This Worker never serves keys or playlists (Django does, after
// can_watch() and every other check), nor any object but a sealed .ts.
//
// Environment:
//   BUCKET                R2 binding to the bucket vidlock writes to.
//   VIDLOCK_EDGE_SECRETS  Secret (wrangler secret put): the EDGE_SECRETS, comma-separated.
//   ALLOWED_ORIGINS       Comma-separated origins allowed to read with CORS (your site). Never '*'.
//   CACHE_SECONDS         Optional: how long a range stays in the edge cache (default 30 days).

const VERSION = 'v1';
const MAX_TTL = 7 * 24 * 3600; // as vidlock.edge.MAX_TTL
const KEY = /^[A-Za-z0-9][A-Za-z0-9/_.-]*\.ts$/;
const MAX_CACHED_RANGE = 64 * 1024 * 1024;
const encoder = new TextEncoder();
const keyCache = new Map();

export default {
  async fetch(request, env, ctx) {
    try {
      return await handle(request, env, ctx);
    } catch (error) {
      console.error('vidlock edge:', error && error.stack ? error.stack : error);
      return text(502, 'Storage is not answering.', corsHeaders(request, env));
    }
  },
};

export async function handle(request, env, ctx) {
  const url = new URL(request.url);
  const cors = corsHeaders(request, env);
  if (request.method === 'OPTIONS') return preflight(request, cors);
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    return text(405, 'Method not allowed.', { ...cors, Allow: 'GET, HEAD, OPTIONS' });
  }

  let key;
  try {
    key = decodeURIComponent(url.pathname.slice(1));
  } catch {
    return text(404, 'Not found.', cors);
  }
  if (!KEY.test(key) || key.split('/').some((part) => part === '' || part === '.' || part === '..')) {
    return text(404, 'Not found.', cors);
  }
  const params = url.searchParams;
  if (!(await valid(env, key, params.get('exp'), params.get('v') ?? '', params.get('sig')))) {
    return text(403, 'Expired or invalid link.', cors);
  }

  const colo = (request.cf && request.cf.colo) || '';
  if (request.method === 'HEAD') {
    const head = await env.BUCKET.head(key);
    if (!head) return text(404, 'Not found.', cors);
    return new Response(null, {
      status: 200,
      headers: mediaHeaders(cors, { 'Content-Length': String(head.size), ETag: head.httpEtag }, 'BYPASS', colo),
    });
  }

  const range = parseRange(request.headers.get('Range'));
  if (range === INVALID) return text(416, 'Range not satisfiable.', cors);

  if (range && range.end !== undefined && range.end - range.start + 1 <= MAX_CACHED_RANGE) {
    return cachedRange(request, env, ctx, url, key, range, cors, colo);
  }
  return passThrough(env, key, range, cors, colo);
}

// -- signature ---------------------------------------------------------------

function secrets(env) {
  return String(env.VIDLOCK_EDGE_SECRETS || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

async function hmacKey(secret) {
  let found = keyCache.get(secret);
  if (!found) {
    found = crypto.subtle.importKey('raw', encoder.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, [
      'verify',
    ]);
    keyCache.set(secret, found);
  }
  return found;
}

function hexBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

export async function valid(env, key, exp, viewer, sig, now = Math.floor(Date.now() / 1000)) {
  if (!/^\d{1,12}$/.test(exp || '') || !/^[0-9a-f]{64}$/.test(sig || '') || !/^[0-9a-f]{0,32}$/.test(viewer)) {
    return false;
  }
  const expires = Number(exp);
  if (expires < now || expires > now + MAX_TTL) return false;
  const data = encoder.encode(`${VERSION}\n${key}\n${expires}\n${viewer}`);
  const signature = hexBytes(sig);
  for (const secret of secrets(env)) {
    // crypto.subtle.verify compares in constant time.
    if (await crypto.subtle.verify('HMAC', await hmacKey(secret), signature, data)) return true;
  }
  return false;
}

// -- ranges ------------------------------------------------------------------

const INVALID = Symbol('invalid');

export function parseRange(header) {
  if (!header) return null;
  const found = /^bytes=(\d*)-(\d*)$/.exec(header.trim());
  if (!found || (found[1] === '' && found[2] === '')) return INVALID; // several ranges, or junk
  if (found[1] === '') return { suffix: Number(found[2]) };
  const start = Number(found[1]);
  if (found[2] === '') return { start };
  const end = Number(found[2]);
  return end < start ? INVALID : { start, end };
}

function r2Range(range) {
  if (!range) return undefined;
  if (range.suffix !== undefined) return { suffix: range.suffix };
  if (range.end === undefined) return { offset: range.start };
  return { offset: range.start, length: range.end - range.start + 1 };
}

async function cachedRange(request, env, ctx, url, key, range, cors, colo) {
  // The token is not part of the cache key: every viewer shares the entry.
  const cacheKey = new Request(`${url.origin}/__vidlock-cache/${encodeURI(key)}?r=${range.start}-${range.end}`);
  const cache = caches.default;
  const hit = await cache.match(cacheKey);
  if (hit) {
    return partial(hit.body, hit.headers, cors, 'HIT', colo);
  }
  let object;
  try {
    object = await env.BUCKET.get(key, { range: r2Range(range) });
  } catch (error) {
    // R2 refuses a range past the end of the object: say how big it is.
    const head = await env.BUCKET.head(key);
    if (!head) return text(404, 'Not found.', cors);
    if (range.start >= head.size) {
      return text(416, 'Range not satisfiable.', { ...cors, 'Content-Range': `bytes */${head.size}` });
    }
    throw error;
  }
  if (!object) return text(404, 'Not found.', cors);
  if (range.start >= object.size) {
    return text(416, 'Range not satisfiable.', { ...cors, 'Content-Range': `bytes */${object.size}` });
  }
  const end = Math.min(range.end, object.size - 1);
  const stored = {
    'Content-Type': 'video/mp2t',
    'Content-Length': String(end - range.start + 1),
    'Cache-Control': `public, max-age=${Number(env.CACHE_SECONDS) || 30 * 24 * 3600}`,
    ETag: object.httpEtag,
    'x-vidlock-range': `${range.start}-${end}`,
    'x-vidlock-size': String(object.size),
  };
  const [forCache, forViewer] = object.body.tee();
  // A cache entry must be a 200; it is turned back into a 206 when served.
  ctx.waitUntil(cache.put(cacheKey, new Response(forCache, { status: 200, headers: stored })));
  return partial(forViewer, new Headers(stored), cors, 'MISS', colo);
}

function partial(body, stored, cors, status, colo) {
  const [start, end] = stored.get('x-vidlock-range').split('-');
  return new Response(body, {
    status: 206,
    headers: mediaHeaders(
      cors,
      {
        'Content-Range': `bytes ${start}-${end}/${stored.get('x-vidlock-size')}`,
        'Content-Length': stored.get('Content-Length'),
        ETag: stored.get('ETag') || '',
      },
      status,
      colo,
    ),
  });
}

async function passThrough(env, key, range, cors, colo) {
  // Open-ended and suffix ranges (Safari's first probe) and whole-file reads:
  // straight from R2, not cached.
  const object = await env.BUCKET.get(key, range ? { range: r2Range(range) } : {});
  if (!object) return text(404, 'Not found.', cors);
  if (!range) {
    return new Response(object.body, {
      status: 200,
      headers: mediaHeaders(cors, { 'Content-Length': String(object.size), ETag: object.httpEtag }, 'BYPASS', colo),
    });
  }
  let start;
  let end;
  if (range.suffix !== undefined) {
    start = Math.max(0, object.size - range.suffix);
    end = object.size - 1;
  } else {
    start = range.start;
    end = object.size - 1;
  }
  if (start >= object.size || end < start) {
    return text(416, 'Range not satisfiable.', { ...cors, 'Content-Range': `bytes */${object.size}` });
  }
  return new Response(object.body, {
    status: 206,
    headers: mediaHeaders(
      cors,
      {
        'Content-Range': `bytes ${start}-${end}/${object.size}`,
        'Content-Length': String(end - start + 1),
        ETag: object.httpEtag,
      },
      'BYPASS',
      colo,
    ),
  });
}

// -- headers -----------------------------------------------------------------

function allowedOrigins(env) {
  return String(env.ALLOWED_ORIGINS || '')
    .split(',')
    .map((s) => s.trim().replace(/\/$/, ''))
    .filter((s) => s && s !== '*');
}

export function corsHeaders(request, env) {
  const origin = request.headers.get('Origin');
  const headers = { Vary: 'Origin' };
  if (origin && allowedOrigins(env).includes(origin)) {
    headers['Access-Control-Allow-Origin'] = origin;
    headers['Access-Control-Expose-Headers'] =
      'Content-Range, Content-Length, Accept-Ranges, ETag, x-vidlock-edge, x-vidlock-cache';
    headers['Timing-Allow-Origin'] = origin;
  }
  return headers;
}

function preflight(request, cors) {
  if (!cors['Access-Control-Allow-Origin']) return text(403, 'Origin not allowed.', cors);
  return new Response(null, {
    status: 204,
    headers: {
      ...cors,
      'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
      'Access-Control-Allow-Headers': 'Range',
      'Access-Control-Max-Age': '86400',
    },
  });
}

function mediaHeaders(cors, extra, cacheStatus, colo) {
  const headers = {
    ...cors,
    'Content-Type': 'video/mp2t',
    'Accept-Ranges': 'bytes',
    // The URL carries a viewer's token: the browser may keep it, shared caches may not.
    'Cache-Control': 'private, max-age=3600',
    'X-Content-Type-Options': 'nosniff',
    'x-vidlock-cache': cacheStatus,
    'x-vidlock-edge': `${VERSION}; cache=${cacheStatus}${colo ? `; colo=${colo}` : ''}`,
    ...extra,
  };
  if (!headers.ETag) delete headers.ETag;
  return headers;
}

function text(status, body, headers) {
  return new Response(body, {
    status,
    headers: { ...headers, 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' },
  });
}
