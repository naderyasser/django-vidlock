# Upgrading from 0.3 to 0.8

What changes for a project that upgrades vidlock from 0.3.0 to 0.8.x. This
covers projects that use vidlock end to end and projects that only call
`vidlock.packager` from their own views, tasks and player.

Each entry says which version brought it, who it affects, whether it
**breaks sessions during a deploy**, and what to do. Most of 0.4–0.8 is
opt-in and changes nothing until you set it. The entries that matter at
deploy time are at the top.

Every claim about tokens and watermark codes below was checked by minting
them with one version's code and verifying them with another's.

---

## First: which 0.3.0 do you have?

Two different builds carry `__version__ = '0.3.0'`. Two fixes landed after
the version bump, with the version string unchanged: SHA-256 pinned for
fingerprints and watermark codes, and the `shared_session` risk event. Check
the installed copy:

```bash
python -c "import vidlock, os; print(os.path.dirname(vidlock.__file__))"
```
```bash
grep -c "algorithm='sha256'" <that folder>/tokens.py
```

* **`0`: the early 0.3.0.** Fingerprints and watermark codes use Django's
  default `salted_hmac` algorithm (SHA-1 up to Django 6.x). Entries
  [1](#1-token-fingerprints-move-to-sha-256) and
  [3](#3-watermark-codes-change) apply to you.
* **`1` or more: the later 0.3.0.** Skip entries 1 and 3.

---

## At a glance

| # | Change | Since | Affects | Breaks sessions during deploy? |
|---|---|---|---|---|
| 1 | Token fingerprints use SHA-256 | 0.3.0 (later build) | everyone on the early 0.3.0 | **Yes**: every token in flight (≤ `TOKEN_TTL`) |
| 2 | Tokens sign a JSON list | 0.5 | everyone | **Only in a rolling deploy or a rollback** |
| 3 | Watermark codes use SHA-256 | 0.3.0 (later build) | early 0.3.0 | No; 0.8.1's `vidlock_trace` still finds codes recorded before the upgrade (0.8.0's does not) |
| 4 | Heartbeat checks the session, 1 request/s | 0.5 | custom players, app clients | No for the bundled player; maybe for a custom one |
| 5 | Per-range IV, no `IV=` attribute | 0.5 | code that parses or rewrites playlists | No; old videos keep their IVs |
| 6 | ffmpeg/ffprobe open inputs as MP4/MOV only | 0.5 | direct `packager` callers | No; affects new seals |
| 7 | `shared_session` and `many_ips` observed, not scored | 0.8.1 | everyone | No; 0.8.0 scores them and can pause phones that switch Wi-Fi ↔ 4G |
| 8 | Key pace taken under a lock | 0.5 | everyone | No |
| 9 | Malformed key-exchange header → 400 | 0.5 | custom players | No |
| 10 | vidlock's own tables (migrations 0001–0004) | 0.4, 0.6, 0.8 | everyone | No; run `migrate` before the new code serves |
| 11 | New settings (36), none renamed or removed | 0.5–0.8.1 | — | No; all opt-in or same defaults |
| 12 | New endpoints and `playback_info` keys | 0.6–0.8 | custom views and players | No; additive |
| 13 | `transcribe` extra caps PyAV below 19 | 0.8 | `TRANSCRIBER` users | No |

---

## Deploy-time breaks

### 1. Token fingerprints move to SHA-256

*Early 0.3.0 only. **Breaks sessions during the deploy.***

Every playlist and key token carries a fingerprint of the viewer's password
hash, and web tokens also carry one of the session key. The early 0.3.0
computed both with `salted_hmac`'s default algorithm (SHA-1 up to Django
6.x); later builds pin SHA-256, so Django 7's new default cannot change them
silently.

**What happens:** a token minted before the deploy is refused after it,
because the account fingerprint no longer matches. Key and playlist
requests get `403 This link has expired`, and heartbeats get
`403 {"error": "expired"}`. This lasts until those tokens would have
expired anyway: `TOKEN_TTL`, 10 minutes by default.

**What to do:**

* **Bundled player** (`player.js`): nothing. A refused key is a fatal hls.js
  error. The player re-fetches your playback endpoint, gets a fresh token,
  and carries on from the same position. Viewers may see a short stall.
* **Your own player or app:** on a 403 from the playlist, key or heartbeat,
  call your playback endpoint again for a fresh token and resume. If it
  shows an error instead, deploy in a quiet hour.
* Passwords, sessions and stored keys are untouched; only tokens in flight
  are affected.

### 2. Tokens sign a JSON list (0.5)

*Everyone. **Breaks sessions only in a rolling deploy or a rollback.***

0.5 signs `["video", "user", "channel", "account", "session", "lease"]`
instead of `video|user|…`, so an id containing `|` cannot shift the fields.

* **0.3/0.4 tokens are accepted by 0.8** until they expire.
* **0.8 tokens are refused by 0.3/0.4.** If old and new workers serve at
  the same time, or you roll back, a token minted by a new worker fails on
  an old one.

**What to do:** switch all web workers at once, not in a rolling deploy, or
accept up to `TOKEN_TTL` of refused tokens (the player recovers as in
entry 1). After a rollback, the same applies in reverse.

### 3. Watermark codes change

*Early 0.3.0 only. No session impact.*

The six-letter code in the watermark is a `salted_hmac` of user and day.
SHA-256 is now pinned, so **every viewer's code changes at the upgrade.**
Recordings made **before** the upgrade show SHA-1 codes.

* **0.8.1 and later:** `manage.py vidlock_trace` finds those too, under
  `SECRET_KEY` and every `SECRET_KEY_FALLBACKS` entry, and marks the match
  `[legacy SHA-1 code: …]`. Nothing to do; leaks recorded before the
  upgrade keep tracing. `--no-legacy` searches current codes only.
* **0.8.0:** its `vidlock_trace` does not find SHA-1 codes. Upgrade to
  0.8.1, or compute them by hand for the day of the recording:

  ```python
  import base64, datetime
  from django.contrib.auth import get_user_model
  from django.utils.crypto import salted_hmac

  def legacy_code(user_id, day):
      digest = salted_hmac('vidlock.trace', f'{user_id}:{day.isoformat()}', algorithm='sha1').digest()
      return base64.b32encode(digest).decode()[:6]

  day = datetime.date(2026, 10, 1)
  [pk for pk in get_user_model().objects.values_list('pk', flat=True) if legacy_code(pk, day) == 'K7QMZ4']
  ```

With django-tenants, trace inside the tenant's schema (`tenant_command
vidlock_trace … --schema=<schema>`, or `schema_context` around the snippet):
user ids repeat across schemas.

---

## Integrators who call the packager directly

`packager.package(source, workdir) -> (ts_path, playlist, keys)` and
`packager.unpackage(...)` keep their signatures. The playlist still carries
the placeholders `__VIDLOCK_MEDIA__` and `__VIDLOCK_KEY__` / `__VIDLOCK_KEY__:n`.
`keys` is still one 16-byte key per rotation period, concatenated, and
`vidlock.keys.wrap/unwrap` are unchanged. A sealed video from 0.3 and one
from 0.8 are stored the same way.

**New in 0.8.1: `vidlock.transcode.encode()`.** Before 0.8.1, a source the
packager cannot take (HEVC from an iPhone, 10-bit, VP9, Opus sound) had no
supported way in, and projects tended to serve the clear file instead.
`encode(source, output, on_progress=…, timeout=…)` turns a local file into
an MP4 that `package()` takes, the way vidlock's own pipeline does, with no
model, task or storage involved. It returns what it did: `'copied'`,
`'audio'` or `'reencoded'`, plus the encoder used. See the README, under
*Calling it from your own pipeline*.

### 5. Per-range IV (0.5)

Each byte range is now encrypted with its media sequence number as the IV,
which is the HLS default. The `#EXT-X-KEY` lines therefore carry **no `IV=`
attribute**: `#EXT-X-KEY:METHOD=AES-128,URI="__VIDLOCK_KEY__"`.

* **Videos sealed by 0.3** keep their explicit `IV=0x…` attributes and play
  as before. Nothing to re-seal.
* **hls.js, Safari, ExoPlayer and AVPlayer** all apply the sequence-number
  IV when the attribute is absent.
* **Your code:** if anything parses or rewrites stored playlists expecting
  `IV=0x`, make it accept lines without it. If you decrypt ranges yourself,
  the IV of range *n* is `(EXT-X-MEDIA-SEQUENCE + n).to_bytes(16, 'big')`.

### 6. Inputs are opened as MP4/MOV only (0.5)

ffmpeg and ffprobe now run with `-f mov -protocol_whitelist file`.
`packager.input_args(path)` returns that, or a Matroska demuxer for
`.mkv`/`.webm`. This stops an "MP4" that is really an HLS playlist from
making older ffmpeg builds fetch URLs or read local files.

* **Your code:** pass only real MP4/MOV files to `package()`. A file in
  another container now fails with a `PackagingError` instead of being
  sealed. Use `packager.probe()` first; 0.8's `Probe` has `.problem`,
  `.video_ok`, `width`, `height`, `color_transfer` and `describe()`, and
  still unpacks as `video, audio = probe(path)`.
* `unpackage()` now writes the clear keys to a private temporary folder
  instead of next to the output.

---

## Endpoints and the player

### 4. Heartbeat (0.5)

`vidlock:heartbeat` now:

* demands, for **web** tokens, the same logged-in user and session as the
  token, like a key request;
* checks that the video is sealed and `can_watch()`;
* accepts **one request per second per stream** (`429 {"error": "slow down"}`).

The request body is unchanged, with one optional addition: `{"position",
"playing", "tamper"}` plus `"watched": [[start, end], …]`, which feeds
watch progress (0.4).

* **Bundled player:** nothing to do; 0.3's and 0.8's both work with it.
* **Your own player or an app:** send the session cookie with web-channel
  heartbeats (app tokens need none). Never send more than one a second.
  Treat 403 as "renew the token", 409 as "watching elsewhere".

### 9. Key exchange header (0.5)

The ECDH protocol (`X-Vidlock-Key-Share`, P-256, HKDF-SHA256, AES-GCM) is
unchanged. A malformed header is now refused with **400** before it spends
the viewer's key allowance. A correct client never sees it.

### 12. New endpoints and `playback_info` keys

All additive; `include('vidlock.urls')` brings them:

| URL name | Since | Notes |
|---|---|---|
| `vidlock:captions` | 0.6 | WebVTT, playlist's token |
| `vidlock:upload` | 0.7 | direct uploads, CSRF-protected |
| `vidlock:poster`, `vidlock:thumbnail`, `vidlock:thumbnails` | 0.8 | previews, playlist's token (`POSTER_PUBLIC` for the first two) |

`playback_info()` may now also return `captions_url`, `captions_language`,
`poster_url` and `thumbnails_url`, only when those features are on.
Ignore unknown keys in your player.

**player.js** gained progress reporting (0.4), captions (0.6), the poster
and `thumbnailAt()` (0.8). It still loads the same bundled hls.js 1.7.3.
A cached 0.3 `player.js` keeps working against a 0.8 server during the
deploy.

---

## Abuse detection

### 7. `shared_session` and `many_ips`: observed, not scored (0.8.1)

A stream that heartbeats from two networks (/24, /48) within 45 seconds is
`shared_session`, which catches a session cookie copied to another device.
Each network past `MAX_NETWORKS_PER_DAY` in a day is `many_ips`. Both also
fire for honest students: a phone moving between Wi-Fi and mobile data
mid-lesson, or Egyptian mobile carriers changing addresses behind CGNAT.

* **0.8.1 and later:** both are in `RISK_OBSERVE_ONLY` by default. They are
  recorded, logged, sent as `vidlock.signals.risk_observed`, shown by
  `manage.py vidlock_risk` as `(observed, not scored)`, and listed in
  `viewer_flagged`'s events. But they add nothing to the score, so they can
  never flag or pause anyone, whatever `RISK_WEIGHTS` says. Nothing to do.
* **To score them** once you trust them on your audience, take them off the
  list: `'RISK_OBSERVE_ONLY': ['many_ips']` scores `shared_session` again,
  and `()` scores everything.
* **0.8.0 and the later 0.3.0 build** score `shared_session` (4 points) and
  `many_ips` (1). With `RISK_SUSPEND_SECONDS` > 0, three Wi-Fi ↔ 4G
  switches in a day pause an honest account at the default threshold of
  10. If you stay on 0.8.0, set `'RISK_WEIGHTS': {'shared_session': 0}`.

### 8. Key pace under a lock (0.5)

The per-viewer key bucket is now taken under a cache lock, so parallel
requests can no longer all read a full bucket. This needs a shared cache
(Redis, Memcached or the database cache) in production, as before (check
`vidlock.W007`). A request that waits more than 0.25 s for the lock is
refused like a paced one (429).

---

## Database

### 10. Migrations

0.3 had no models of its own. 0.8 has four migrations:

| Migration | Since | Table |
|---|---|---|
| `0001_initial` | 0.4 | `WatchProgress` (foreign key to your user model) |
| `0002_transcript…` | 0.6 | `Transcript` |
| `0003_transcode` | 0.8 | `Transcode` |
| `0004_preview` | 0.8 | `Preview` |

Run `manage.py migrate` **before** the new code serves traffic.
`SealedVideoMixin`'s fields did not change between 0.3 and 0.8, so your own
models need no migration.

**django-tenants:** these tables belong with the videos and users they
point at. With users per schema, put `'vidlock'` in `TENANT_APPS` (and run
`migrate_schemas`). With shared users and per-tenant videos, `WatchProgress`
needs the user table in its schema too, so keep vidlock in `TENANT_APPS` and
make sure `AUTH_USER_MODEL`'s app is shared to every tenant schema.

---

## Settings

### 11. Added, none renamed or removed

No 0.3 setting was renamed or removed, and no default changed.
`'PROFILE': 'balanced'` (new, the default) is exactly the 0.3 defaults.

| Added | Since | Off / default |
|---|---|---|
| `PROFILE`, `ENFORCE` | 0.5 | `'balanced'`, `True` |
| `RISK_OBSERVE_ONLY` | 0.8.1 | `('shared_session', 'many_ips')`: recorded, never scored |
| `TRACK_PROGRESS` | 0.4 | `True` (progress from the heartbeat) |
| `TRANSCRIBER`, `TRANSCRIBE_MODEL`, `TRANSCRIBE_LANGUAGE` | 0.6 | off |
| `NOTIFIER`, `NOTIFY_WEBHOOK_URL`, `WHATSAPP_*` | 0.6 | off |
| `MAX_UPLOAD_BYTES`, `UPLOAD_PREFIX`, `UPLOAD_TTL` | 0.7 | used only by direct uploads |
| `TRANSCODE*`, `ENQUEUE_TRANSCODE` | 0.8 | off |
| `THUMBNAILS`, `THUMBNAIL_OFFSET`, `SPRITE_*`, `POSTER_PUBLIC` | 0.8 | off |
| `DELIVERY`, `EDGE_URL`, `EDGE_SECRETS` | 0.8.1 | `'direct'` |

`manage.py check` warns about any key it does not know (`vidlock.W001`).

### 13. Dependencies

* `cryptography` is a dependency (since 0.3).
* `pip install django-vidlock[transcribe]` now caps PyAV below 19: PyAV 19
  removed an argument faster-whisper 1.2.1 still passes.

---

## A deploy plan

1. Check which 0.3.0 you run ([above](#first-which-030-do-you-have)). If
   it is the early one, upgrade to 0.8.1 or later rather than 0.8.0, so
   watermark codes recorded before the upgrade still trace.
2. Upgrade the package on every machine and run `manage.py migrate`
   (`migrate_schemas` with django-tenants).
3. Switch **all** web workers together, in a quiet hour. Expect up to
   `TOKEN_TTL` of 403s on tokens minted before the switch (entries 1 and 2).
   The bundled player recovers; check that yours does.
4. Run `manage.py vidlock_doctor` and `manage.py check`.
5. Consider `'ENFORCE': False` for a few days to see what the rules would
   refuse, then turn it back on. `shared_session` and `many_ips` are only
   observed from 0.8.1 on (entry 7); read them in `manage.py vidlock_risk`.
6. Your sealed videos need nothing: 0.3 seals keep their keys, IVs and
   playlists.

**Rolling back** to 0.3: tokens minted by 0.8 are refused by 0.3 for up to
`TOKEN_TTL` (entry 2). The new tables stay and are ignored. Videos sealed by
0.8 play on 0.3: same storage, same key format, and a playlist without `IV=`
is standard HLS.
