// The real Worker (src/index.js) on a local port, with a folder standing in for
// the R2 bucket and an in-memory edge cache. For trying edge delivery against
// the example project's development storage, without a Cloudflare account:
//
//   node test/local-server.mjs --dir ../../example/media/vidlock --port 8787 \
//        --secrets "$EDGE_SECRET" --origins http://127.0.0.1:8000
//
// Production runs on Cloudflare (wrangler deploy); this is a test harness.
import { createReadStream, promises as fs } from 'node:fs';
import { createServer } from 'node:http';
import path from 'node:path';
import { Readable } from 'node:stream';

import worker from '../src/index.js';
import { FakeCache } from './fakes.mjs';

const args = Object.fromEntries(
  process.argv.slice(2).reduce((pairs, arg, i, all) => {
    if (arg.startsWith('--')) pairs.push([arg.slice(2), all[i + 1]]);
    return pairs;
  }, []),
);
const root = path.resolve(args.dir || '.');
const port = Number(args.port || 8787);

class FolderBucket {
  file(key) {
    const full = path.resolve(root, key);
    return full.startsWith(root + path.sep) ? full : null;
  }

  async head(key) {
    const full = this.file(key);
    try {
      const stat = await fs.stat(full);
      return { size: stat.size, httpEtag: `"${stat.size}-${Math.floor(stat.mtimeMs)}"` };
    } catch {
      return null;
    }
  }

  async get(key, options = {}) {
    const head = await this.head(key);
    if (!head) return null;
    let start = 0;
    let end = head.size - 1;
    const range = options.range;
    if (range) {
      if (range.suffix !== undefined) start = Math.max(0, head.size - range.suffix);
      else {
        start = range.offset ?? 0;
        if (start >= head.size) throw new RangeError('InvalidRange');
        if (range.length !== undefined) end = Math.min(head.size - 1, start + range.length - 1);
      }
    }
    const body = Readable.toWeb(createReadStream(this.file(key), { start, end }));
    return { ...head, body };
  }
}

globalThis.caches = { default: new FakeCache() };
const env = {
  BUCKET: new FolderBucket(),
  VIDLOCK_EDGE_SECRETS: args.secrets || process.env.VIDLOCK_EDGE_SECRETS || '',
  ALLOWED_ORIGINS: args.origins || '',
};

createServer(async (incoming, outgoing) => {
  const url = `http://${incoming.headers.host}${incoming.url}`;
  const request = new Request(url, { method: incoming.method, headers: incoming.headers });
  const pending = [];
  const response = await worker.fetch(request, env, { waitUntil: (p) => pending.push(p) });
  outgoing.writeHead(response.status, Object.fromEntries(response.headers));
  if (response.body && incoming.method !== 'HEAD') {
    for await (const chunk of response.body) outgoing.write(chunk);
  }
  outgoing.end();
  await Promise.all(pending);
}).listen(port, '127.0.0.1', () => console.log(`vidlock edge (local) on http://127.0.0.1:${port}, bucket ${root}`));
