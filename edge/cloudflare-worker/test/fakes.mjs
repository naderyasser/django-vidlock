// An in-memory R2 binding and Cache API, enough to run the Worker under Node.

export class FakeBucket {
  constructor(objects = {}) {
    this.objects = new Map(Object.entries(objects));
    this.gets = 0;
    this.heads = 0;
  }

  async head(key) {
    this.heads++;
    const found = this.objects.get(key);
    return found ? { size: found.length, httpEtag: `"etag-${key.length}"` } : null;
  }

  async get(key, options = {}) {
    this.gets++;
    const found = this.objects.get(key);
    if (!found) return null;
    let start = 0;
    let end = found.length;
    const range = options.range;
    if (range) {
      if (range.suffix !== undefined) {
        start = Math.max(0, found.length - range.suffix);
      } else {
        start = range.offset ?? 0;
        if (start >= found.length) throw new RangeError('InvalidRange');
        end = range.length !== undefined ? Math.min(found.length, start + range.length) : found.length;
      }
    }
    const slice = found.slice(start, end);
    return {
      size: found.length,
      httpEtag: `"etag-${key.length}"`,
      body: new Response(slice).body,
    };
  }
}

export class FakeCache {
  constructor() {
    this.entries = new Map();
    this.puts = [];
  }

  async match(request) {
    const found = this.entries.get(typeof request === 'string' ? request : request.url);
    if (!found) return undefined;
    return new Response(found.body.slice(), { status: found.status, headers: found.headers });
  }

  async put(request, response) {
    if (response.status === 206) throw new TypeError('Cache API refuses 206 responses');
    const body = new Uint8Array(await response.arrayBuffer());
    const url = typeof request === 'string' ? request : request.url;
    this.puts.push(url);
    this.entries.set(url, { status: response.status, headers: new Headers(response.headers), body });
  }
}

export function context() {
  const pending = [];
  return {
    waitUntil(promise) {
      pending.push(promise);
    },
    settle() {
      return Promise.all(pending);
    },
  };
}

const encoder = new TextEncoder();

// As vidlock.edge.url_for, in JavaScript.
export async function sign(base, key, secret, { ttl = 600, viewer = 'abc123def456', now } = {}) {
  const expires = Math.floor((now ?? Date.now()) / 1000) + ttl;
  const cryptoKey = await crypto.subtle.importKey(
    'raw',
    encoder.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const mac = new Uint8Array(
    await crypto.subtle.sign('HMAC', cryptoKey, encoder.encode(`v1\n${key}\n${expires}\n${viewer}`)),
  );
  const sig = [...mac].map((b) => b.toString(16).padStart(2, '0')).join('');
  return `${base}/${key}?exp=${expires}&v=${viewer}&sig=${sig}`;
}
