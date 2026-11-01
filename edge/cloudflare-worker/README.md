# vidlock edge Worker (Cloudflare, in front of R2)

By default, a player reads a sealed video's bytes straight from R2 through
presigned URLs. Every byte range is a request to the bucket's region (a full
TLS round trip from Cairo or Riyadh) and a billed Class B operation, with no
cache in between.

This Worker puts Cloudflare's edge in front of R2 for **sealed media only**:

* It checks the HMAC-SHA256 signature Django put on the URL
  (`vidlock.edge`) and its expiry, and answers 403 otherwise. The check
  uses `crypto.subtle.verify`, which compares in constant time.
* It reads the object through an **R2 binding**, so the bucket stays
  private: no public access, no `r2.dev` URL.
* It answers Range requests: 206, `Content-Range`, `Accept-Ranges`, and 416
  past the end.
* It caches each **encrypted** range under a key **without the token**, so
  every viewer in a data centre shares the entry. That is safe because the
  bytes are AES-128 encrypted and the keys never pass through here.
* It **never** serves or caches keys or playlists (those are not in the
  bucket; Django serves them after `can_watch()`, the session, ECDH, pacing
  and leases, unchanged). It serves no object but a sealed `.ts`: not the
  uploaded MP4s, and not the previews.
* CORS goes only to the origins in `ALLOWED_ORIGINS`, never `*`.
  `x-vidlock-edge` and `x-vidlock-cache` (`HIT`, `MISS`, `BYPASS`) say what
  happened.

## Set it up

1. **Secrets.** Generate one:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

2. **Deploy the Worker.**

   ```bash
   cd edge/cloudflare-worker
   cp wrangler.toml.example wrangler.toml   # set the domain, bucket and origin
   npx wrangler secret put VIDLOCK_EDGE_SECRETS
   npx wrangler deploy
   ```

   The `routes` entry with `custom_domain = true` creates the DNS record
   (`media.courses.example`, proxied) and the certificate for you. The
   domain must be on a zone in the same Cloudflare account as the bucket.
   Don't use `*.workers.dev`: the Cache API does nothing there, and every
   request would go to R2.

3. **Point Django at it.**

   ```python
   VIDLOCK = {
       ...,
       'DELIVERY': 'edge',
       'EDGE_URL': 'https://media.courses.example',
       'EDGE_SECRETS': [env('VIDLOCK_EDGE_SECRET')],
   }
   ```

   `manage.py check` reports a missing URL or secret (`vidlock.E009`),
   plain HTTP (`W011`) and a short secret (`W012`). Uploads, sealing and the
   bucket's CORS for direct uploads are unchanged.

4. **Check it.** `manage.py vidlock_doctor` puts a small `.ts` in the bucket
   and reads it through the Worker: 206, a MISS then a HIT, and 403 for a
   tampered signature.

To go back, set `DELIVERY = 'direct'`. URLs already handed out keep working
until they expire.

## Rotating the secret

1. Put the new secret first in both places: `EDGE_SECRETS = [new, old]` in
   Django, and `VIDLOCK_EDGE_SECRETS = "new,old"` in the Worker (Worker first).
2. Deploy Django. New URLs are signed with `new`; old ones still pass.
3. After the longest URL lifetime has passed (`TOKEN_TTL` plus your longest
   video, at most 7 days), remove `old` from both.

## Verification checklist

With a playlist URL from `manage.py vidlock_bench_url courses.Lesson 42 --user bench`:

```bash
MEDIA=$(curl -s "$PLAYLIST" | grep -m1 '^https://media.')
curl -s -o /dev/null -D - -H 'Range: bytes=0-1023' "$MEDIA"     # 1st: 206, x-vidlock-cache: MISS
curl -s -o /dev/null -D - -H 'Range: bytes=0-1023' "$MEDIA"     # 2nd: 206, x-vidlock-cache: HIT
curl -s -o /dev/null -w '%{http_code}\n' "${MEDIA%?}0"            # tampered signature: 403
curl -s -o /dev/null -w '%{http_code}\n' "${MEDIA%%\?*}"          # no signature: 403
curl -s "$PLAYLIST" | grep EXT-X-KEY                              # keys: still your Django domain
```

- [ ] `206`, `Content-Range: bytes 0-1023/<size>`, `Accept-Ranges: bytes`
- [ ] `x-vidlock-cache: MISS`, then `HIT` (from the same data centre: same `colo=` in `x-vidlock-edge`)
- [ ] `403` for a tampered, expired or missing signature
- [ ] `EXT-X-KEY` URIs and the playlist itself on your Django domain, not the Worker's
- [ ] In the browser: media requests to `media.…` with `x-vidlock-edge`; key requests to your site

About `cf-cache-status`: Cloudflare sets it for its CDN cache, which a request
answered by a Worker does not go through. This Worker caches with the Cache
API instead and reports the result in `x-vidlock-cache`; the benchmark shows
whichever is there.

## Compare before and after

`scripts/vidlock_bench.py` prints per-request TTFB, status, cache status and
the data centre (`cf-ray`). Capture a baseline with `DELIVERY = 'direct'`
first, then again through the edge:

```bash
python3 vidlock_bench.py "$PLAYLIST" --label 'Cairo, direct' --json cairo-direct.json
python3 vidlock_bench.py "$PLAYLIST" --label 'Cairo, edge' --passes 2 --json cairo-edge.json
python3 vidlock_bench.py --compare cairo-direct.json cairo-edge.json
```

## What it costs, and what it does not do

* **Requests.** Every range a player asks for is one Worker request. The
  Workers Free plan allows 100,000 a day: about 370 full views of a
  45-minute lesson at 10-second segments. Workers Paid includes 10 million
  a month, then charges per million. A cache HIT costs no R2 operation; a MISS
  costs the one it would have cost anyway.
* **The cache is per data centre.** The Cache API is not tiered: Cairo and
  Jeddah each fill their own cache, so the first viewer of a segment in each
  city reads it from R2. Popular lessons gain the most.
* **A shared URL plays until it expires,** as a presigned URL does today:
  the Worker cannot check a Django session. The viewer tag `v` in the
  signature names who it was issued to. What protects the video is
  unchanged: no key without Django's checks.

## Tests

```bash
npm test                       # node --test, no dependencies (Node 18+)
node test/local-server.mjs --dir ../../example/media/vidlock --secrets "$SECRET" --origins http://127.0.0.1:8000
```

`test/local-server.mjs` runs this Worker on a local port, with a folder
standing in for R2. With it, the example project plays through the edge
(`EDGE_URL = 'http://127.0.0.1:8787'`) without a Cloudflare account.
