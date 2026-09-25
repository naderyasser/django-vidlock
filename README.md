# django-vidlock

[![CI](https://github.com/naderyasser/django-vidlock/actions/workflows/ci.yml/badge.svg)](https://github.com/naderyasser/django-vidlock/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/django-vidlock.svg)](https://pypi.org/project/django-vidlock/)
[![Python](https://img.shields.io/pypi/pyversions/django-vidlock.svg)](https://pypi.org/project/django-vidlock/)
[![Django](https://img.shields.io/badge/django-4.2%20%7C%205.x%20%7C%206.0-0C4B33.svg)](https://www.djangoproject.com/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Protect your course videos without a DRM budget.** vidlock turns every
upload into encrypted HLS stored as a single file. Someone who downloads that
file gets nothing playable without its key. The key goes only to a viewer
you approve, for a few minutes, and download tools are refused.

It needs no DRM licence, no video service and no re-encoding. Each upload
takes one `ffmpeg -c copy` run and ends up as one object in your bucket.
Viewers watch in hls.js, Safari, iOS, ExoPlayer or AVPlayer.

[العربية ↓](#بالعربي)

---

## The problem

A signed MP4 URL stops casual link-sharing, but anyone with a download
manager catches the URL while it is valid and walks away with a clean,
playable copy. Paid-course platforms lose their catalogue this way.

## What vidlock does

1. **Seals every upload.** ffmpeg remuxes the MP4 into **one** AES-128
   encrypted MPEG-TS and a playlist of byte ranges. It does not re-encode: a
   78 MB lesson takes about a second and under 50 MB of RAM. The sealed file
   replaces the MP4 in your bucket. **Every minute of video has its own
   key.** The keys and the playlist live in your database, where the keys are
   themselves encrypted.
2. **Hands the key only to a viewer you approve.** Playlist and key URLs carry
   a token bound to one viewer, one video, one login session and ten minutes.
   Your `can_watch()` runs again on every playlist and key request, so a
   refund, a revoked enrolment, a logout or a password change takes effect
   immediately.
3. **Refuses keys to download tools.**
   * A browser token opens a key only alongside the same session cookie, on a
     same-site fetch, so a URL pasted into yt-dlp or N_m3u8DL-RE is refused.
   * **The key never crosses the network in the clear.** The page makes an
     ECDH key pair whose private half never leaves the browser's crypto
     engine, and each key is sealed to it. Copying "the key" from DevTools
     into a downloader, the most common rip, gets nothing usable.
   * **Keys go out no faster than someone could watch**: at most twice
     playback speed, per viewer and video. A tool that wants a whole lesson
     waits most of its length, and a copied key opens one minute.
   * Limits on *depth* (the same key over and over) and *breadth* (many
     videos in an hour, which is how a course gets harvested).
4. **Stops account sharing.** With `MAX_STREAMS = 1`, a second device that
   opens a video takes the stream over, and the first one stops with a
   message. A heartbeat keeps each stream's lease.
5. **Scores suspicion.** Refused keys, keys fetched but never played, raw key
   fetches, takeovers, too many networks in a day, and "save the stream"
   extensions detected by the player all add to a daily score per viewer.
   Past a threshold you get a signal, and optionally the account is paused
   automatically.
6. **Names the recorder.** A moving watermark with the viewer's name, a
   six-letter code and the time is drawn over the video, and a faint copy
   covers the whole frame, fullscreen included. `manage.py vidlock_trace
   <code>` turns a leaked recording back into the account that made it.
7. **Plays everywhere.** The bundled script uses hls.js wherever MediaSource
   exists and native HLS on Safari/iOS. Mobile apps hand the same URL to
   ExoPlayer or AVPlayer. Signed URLs are renewed before they expire without
   reloading the stream.

### How it fits together

```
upload.mp4 ──► vidlock.pipeline.seal  (your worker: Celery, RQ, Django Tasks…)
                 ffprobe → ffmpeg -c copy -hls_flags single_file + AES-128
                 ├── bucket:   <random>.ts          (encrypted, one object)
                 └── database: playlist + wrapped key

page ──► your playback view ──► playback_info()  {url, key_url, heartbeat_url, watermark…}
player ──► /video/<id>/media.m3u8?t=…      can_watch() ✓  token ✓  stream lease ✓
       ──► /video/<id>/key?t=…&k=<n>       can_watch() ✓  token ✓  session ✓  lease ✓
                                            pace ✓  limits ✓  → key n, sealed to this page (ECDH)
       ──► /video/<id>/heartbeat?t=…       keeps the lease, proves playback, reports tampering
       ──► bucket (signed Range GETs)      encrypted bytes, decrypted in the browser
```

### Why one file

`-hls_flags single_file` writes a single `.ts` plus a playlist of
`#EXT-X-BYTERANGE`s into it, and each range can be decrypted on its own.
Compared with the usual hundreds of segment files:

* one upload, one signed URL and one delete, so your storage accounting and
  cleanup code see one key per video, exactly as they did for the MP4;
* no per-segment URL signing, and no playlist full of object names.

### What it is not

**It is not DRM.** The keys have to reach the browser for the video to
play, so someone who reverse-engineers the page, or an extension that copies
what the player hands to MediaSource, can still end up with the video. What
vidlock does is:

* make the downloaded file useless;
* make the easy tools fail;
* make bulk ripping slow and noisy;
* make sharing an account stop working;
* make a screen recording point back to the account.

If you need more than that, you need Widevine/FairPlay/PlayReady and their
licence fees.

## Requirements

* Python 3.10+ and Django 4.2, 5.x or 6.0
* `ffmpeg` and `ffprobe` on the worker that seals (any recent build; the web
  servers don't need them)
* A **private** bucket: any S3-compatible service (AWS S3, Cloudflare R2,
  MinIO, Backblaze B2, Wasabi, DigitalOcean Spaces…) with
  `pip install django-vidlock[s3]`, or any Django storage backend, such as
  Google Cloud Storage or Azure through django-storages
* Sources in H.264 8-bit 4:2:0 with AAC or MP3 audio, which is what nearly
  every camera, phone export and screen recorder produces. Anything else
  (HEVC from an iPhone, 10-bit H.264, WebM) is left as uploaded and marked
  `skipped`, with the reason. Sealing never re-encodes.

## Quick start

```bash
pip install "django-vidlock[s3]"
```

```python
# settings.py
INSTALLED_APPS = [..., 'vidlock']

VIDLOCK = {
    'BACKEND': 'courses.video.LessonBackend',
    'S3_BUCKET': 'my-private-videos',
    'S3_ENDPOINT_URL': 'https://<account>.r2.cloudflarestorage.com',
    'S3_ACCESS_KEY_ID': env('R2_KEY'),
    'S3_SECRET_ACCESS_KEY': env('R2_SECRET'),
    # A secret of its own for the video keys (see "Keys at rest").
    'KEY_ENCRYPTION_KEYS': [env('VIDLOCK_KEK')],
}
```

```python
# models.py: your model gains video_key, sealed_state, sealed_playlist, sealed_key…
from vidlock.models import SealedVideoMixin

class Lesson(SealedVideoMixin):
    course = models.ForeignKey(Course, on_delete=models.CASCADE)
```

```python
# courses/video.py: the questions only your project can answer
from vidlock.backend import SealedBackend

class LessonBackend(SealedBackend):
    def get_video(self, video_id):
        return Lesson.objects.filter(pk=video_id).first()

    def can_watch(self, user, video):
        return video.course.enrolments.filter(user=user, active=True).exists()

    def watermark(self, user, video):          # optional
        return user.email
```

```python
# urls.py
path('video/', include('vidlock.urls')),
```

Then run `python manage.py makemigrations && python manage.py migrate`, and
`python manage.py check` to see whether anything is missing.

### After an upload lands in the bucket

Record the key and queue the sealing in one call:

```python
from vidlock.pipeline import seal_later

seal_later(lesson, uploaded_key)   # saves the row, queues after the commit
```

It uses `VIDLOCK['ENQUEUE_SEAL']`, and ready-made wrappers ship with
vidlock:

```python
VIDLOCK = {..., 'ENQUEUE_SEAL': 'vidlock.contrib.celery.enqueue'}        # Celery
VIDLOCK = {..., 'ENQUEUE_SEAL': 'vidlock.contrib.django_tasks.enqueue'}  # Django 6 Tasks
```

Without it, `seal_later` seals inline after the commit. Or do the
bookkeeping yourself:

```python
from django.db import transaction
from vidlock.pipeline import seal, wants

lesson.forget_seal()           # a replaced video must not keep the old seal
lesson.video_key = uploaded_key
lesson.sealed_state = Lesson.STATE_PENDING if wants(uploaded_key) else ''
lesson.save()

if wants(uploaded_key):
    # .delay(...) with Celery, .enqueue(...) with Django Tasks
    transaction.on_commit(lambda: seal_task.delay(lesson.pk, uploaded_key))
```

`seal` is synchronous and idempotent, so any task runner works:

```python
# Celery
@shared_task(acks_late=True)
def seal_task(pk, key):
    return seal(Lesson, pk, key)

# Django 6.0 Tasks
from django.tasks import task

@task
def seal_task(pk, key):
    return seal(Lesson, pk, key)

# RQ:        queue.enqueue(seal, Lesson, pk, key)
# Huey:      @db_task() def seal_task(pk, key): return seal(Lesson, pk, key)
# Django-Q2: async_task('vidlock.pipeline.seal', Lesson, pk, key)
```

The MP4 keeps playing until sealing finishes. If a new upload replaces the
video while ffmpeg is running, the job notices and throws its work away. If
anything fails, the upload stays as it was.

### Your playback endpoint

Check access the way you always have, then:

```python
from vidlock.views import playback_info

@login_required
def playback(request, pk):
    lesson = get_object_or_404(Lesson, pk=pk)
    if not LessonBackend().can_watch(request.user, lesson):
        return HttpResponseForbidden()
    return JsonResponse(playback_info(request, lesson))
```

```json
{"format": "hls", "url": "https://…/video/42/media.m3u8?t=…",
 "media_url": "https://bucket…", "key_url": "https://…/video/42/key?t=…",
 "expires_in": 600, "duration": 2712.4, "watermark": "amira@example.com"}
```

### The page

```html
{% load vidlock %}
<video id="player" controls playsinline></video>
{% vidlock_player %}
<script>
  VidLock.attach(document.getElementById('player'), {
    endpoint: '/lessons/42/playback/',
    onStatus: (message) => { /* show or clear a message */ },
  });
</script>
```

`{% vidlock_player %}` loads `player.js` and the copy of hls.js bundled with
vidlock (1.7.3, from your own static files, so there is no third-party CDN
in the path). `attach` also accepts these options:

| Option | |
|---|---|
| `lang` | Status messages in `en`, `ar`, `fr`, `es`, `pt`, `de` or `tr`. Default: `<html lang>`, then the browser's language. |
| `messages` | Your own wording, e.g. `{failed: '…', offline: '…'}`. |
| `watermark` | `false` to hide it, or `{text, opacity, interval}` to override the server's. |
| `hlsConfig` | Merged into hls.js's config (buffer sizes, ABR tuning…). |
| `onEvicted` | `(message) => {}`, called when the stream moved to another device or the account was paused. `reload()` takes it back. |
| `onHls` | `(hls) => {}`, called with the hls.js instance before it loads, e.g. for analytics. |
| `headers` | Extra headers for the playback request, e.g. a CSRF or auth header. |

`attach` returns `{reload(), destroy()}`.

### The watermark

Return text from `SealedBackend.watermark(user, video)`. The player then
draws it over the video with a six-letter code and the current time, at 40%
opacity, moving to a new spot every eight seconds. A faint copy is tiled
across the whole frame, so cropping the moving one out leaves the other
behind. Deleting, hiding or fading either of them from the developer console
puts it straight back. In fullscreen, the player fullscreens a frame around
the video so the watermark stays visible.

iPhone's own fullscreen player draws nothing over the video, so the player
leaves it straight away. Pass `watermark: {allowNativeFullscreen: true}` to
allow it anyway. Other watermark options: `opacity`, `patternOpacity`,
`pattern: false`, `clock: false` and `interval`.

### Tracing a leak

The code changes every day and for every viewer, and means nothing without
your `SECRET_KEY`:

```bash
manage.py vidlock_trace K7QMZ4               # searches the last 60 days
manage.py vidlock_trace K7QMZ4 --day 2026-09-25
```

## One screen at a time

```python
VIDLOCK = {..., 'MAX_STREAMS': 1}
```

A stream belongs to a device: its browser session, or its app login. Two
tabs of the same browser share one. When a second device opens a video past
the limit, it takes the stream over. The first device's next key request or
heartbeat (every 30 seconds) is refused, and its player stops with *"This
account is watching on another device"*; `reload()` takes the stream back.
The player's own automatic renewals never take a stream back, so two devices
cannot keep stealing it from each other. Copying the session cookie itself
to a second device shares one stream. The risk score catches that as
`shared_session`, when one stream heartbeats from two networks at once. A stream whose heartbeats stop, for
example on a laptop that went to sleep, lapses after `STREAM_TIMEOUT` and
comes back by itself unless another device took its place.

## The risk score

Each of these adds its weight to a viewer's score for the day:

| Event | Weight | |
|---|---|---|
| `depth` / `breadth` / `pace` | 3 / 5 / 3 | A key limit refused a key. |
| `fetch_metadata` | 2 | A web key request that came cross-site or as a page load. |
| `raw_key` | 1 | A web key fetched without the page's key exchange: a downloader fed a copied cookie (or an iPhone older than iOS 17.1). |
| `key_without_playback` | 3 | The browser got a key and never reported playing it. |
| `takeover` | 2 | A device took the stream from another. |
| `many_ips` | 1 | Each network past `MAX_NETWORKS_PER_DAY`. |
| `tamper` | 4 | The player found MediaSource functions replaced, which is how "save the stream" extensions work. |
| `shared_session` | 4 | One stream heartbeating from two networks at once: the session cookie was copied to another device, a way of sharing an account that `MAX_STREAMS` alone cannot see. |

When the score reaches `RISK_THRESHOLD` (10), vidlock logs a warning and
sends `vidlock.signals.viewer_flagged`. With `RISK_SUSPEND_SECONDS`, it also
refuses that viewer keys for that long, and their player says the account is
paused. `manage.py vidlock_risk <user>` shows the score and
`manage.py vidlock_risk <user> --clear` lifts the pause. Tune the weights
with `RISK_WEIGHTS`. Behind a proxy or CDN, override
`SealedBackend.client_ip(request)`.

```python
from django.dispatch import receiver
from vidlock.signals import viewer_flagged

@receiver(viewer_flagged)
def review(sender, user, score, events, **kwargs):
    Flag.objects.create(user=user, score=score, events=events)   # your own model
```

## Storage

**Any S3-compatible bucket** (the default): fill in the `S3_*` settings.
hls.js reads byte ranges with XHR, so the bucket's CORS must allow it. The
signed URL is still the only way in; CORS only lets the browser read the
answer.

```json
[{"AllowedOrigins": ["https://your-site.example"], "AllowedMethods": ["GET", "HEAD"],
  "AllowedHeaders": ["range"], "ExposeHeaders": ["content-length", "content-range"],
  "MaxAgeSeconds": 3600}]
```

**Google Cloud Storage, Azure Blob, or any Django storage:**

```python
STORAGES = {
    ...,
    'videos': {'BACKEND': 'storages.backends.gcloud.GoogleCloudStorage',
               'OPTIONS': {'bucket_name': 'my-private-videos', 'querystring_auth': True}},
}
VIDLOCK = {..., 'STORAGE': 'vidlock.storage.DjangoStorage', 'DJANGO_STORAGE': 'videos'}
```

**Anything else:** write a class with `download`, `upload`, `signed_url` and
`delete` (see `vidlock/storage.py`) and point `STORAGE` at it.

## Mobile apps

Authenticate the playback request with a Bearer token (DRF, SimpleJWT and
django-ninja all set `request.auth`), and vidlock issues an **app** token,
which needs no cookie. Hand `url` to ExoPlayer's `HlsMediaSource` or to
`AVPlayer`; both fetch the key themselves. Before `expires_in` runs out, ask
the endpoint again and reload at the current position. App tokens are
protected by the fetch limits, the key pace and the password binding.

**Heartbeat.** With `MAX_STREAMS` set, the app must also POST to
`heartbeat_url` every `heartbeat_interval` seconds, with a JSON body such as
`{"playing": true}`. A `409` answer means the stream moved to another
device, so stop playback.

**Block screen recording.** On Android, set `FLAG_SECURE` on the player's
window (`window.setFlags(FLAG_SECURE, FLAG_SECURE)`). On iOS, where only
FairPlay content is blacked out by the system, watch
`UIScreen.main.isCaptured` (`UIScreen.capturedDidChangeNotification`) and
pause while it is true.

## Security model

| Layer | What it stops |
|---|---|
| Encrypted file in the bucket | A leaked or scraped media URL: the bytes are AES-128, and the key is not in the bucket. |
| **Keys at rest** | A leaked database dump: every video key is encrypted under `KEY_ENCRYPTION_KEYS` (encrypt-then-MAC, standard library only). The dump alone opens nothing. |
| Per-viewer, per-video, 10-minute tokens | Sharing a link: it opens one video for one viewer, briefly, and `can_watch()` is asked again every time. |
| **Session binding** (web) | A key URL pasted into a download tool: it has no session cookie. Logging out voids the session's tokens. |
| **Password binding** (web and app) | A password change voids every token that viewer already holds. |
| **Fetch Metadata** (web) | A key opened in a tab or requested cross-site is refused. With `STRICT_FETCH_METADATA`, so is any request without the `Sec-Fetch-*` headers every current browser sends. |
| **Key exchange** (web) | Copying a key from DevTools into a downloader: what crosses the network is sealed to that page's private key, which never leaves WebCrypto. `REQUIRE_WRAPPED_KEY` refuses raw keys to web tokens altogether, which stops generic tools fed a copied cookie. A tool written against vidlock's own (open) protocol can still do the exchange, and then runs into the pace and the risk score. |
| **Key rotation and pace** | Ripping a lesson at once: every minute has its own key, handed out no faster than 2× playback. |
| **Depth and breadth limits** | Repeated key pulls, and harvesting a whole course one lesson at a time. |
| **One screen at a time** | Sharing an account: the second device takes over, and the first stops. |
| **Risk score** | Everything else a tool gives away, adding up to a flag or an automatic pause. |
| **Watermark with a code** | Screen recording, which no web technology can prevent, becomes traceable to an account. |

When a limit is crossed, vidlock answers 429, logs a warning, sends
`vidlock.signals.key_abuse` (with `reason`) and calls `ON_KEY_ABUSE`, at most
once per viewer and day. It is up to you whether to alert, suspend or ignore.

```python
from django.dispatch import receiver
from vidlock.signals import key_abuse

@receiver(key_abuse)
def alert(sender, request, user, video, reason, **kwargs):
    mail_admins('vidlock', f'{user} crossed the {reason} limit on {video}')
```

## Operating it

**Management commands**

```bash
manage.py vidlock_status                      # videos per state, keys waiting for a rewrap
manage.py vidlock_seal                        # seal existing videos (unsealed, pending, failed)
manage.py vidlock_seal courses.Lesson --state failed --limit 50
manage.py vidlock_seal --queue                # hand them to VIDLOCK['ENQUEUE_SEAL'] instead
manage.py vidlock_rewrap                      # re-encrypt keys under the first KEY_ENCRYPTION_KEYS
manage.py vidlock_export courses.Lesson 42 lesson-42.mp4   # decrypt back to a playable MP4
manage.py vidlock_trace K7QMZ4                # which account a watermark code belongs to
manage.py vidlock_risk amira --clear          # a viewer's risk score; lift a pause
```

**Admin**

```python
from vidlock.admin import SealedVideoAdminMixin

@admin.register(Lesson)
class LessonAdmin(SealedVideoAdminMixin, admin.ModelAdmin):
    list_display = ('title', 'seal_status')
    list_filter = ('sealed_state',)
```

The mixin adds a *Protection* column, a read-only duration and error, and a
*Seal again* action. The action goes through `ENQUEUE_SEAL` when that is set,
and seals inline otherwise.

**System checks.** `manage.py check` reports:

* a missing backend, storage or ffmpeg;
* mistyped setting names;
* keys still derived from `SECRET_KEY`;
* a cache that is not shared between processes. Limits, leases and scores
  live in the cache, so use Redis, Memcached or the database cache in
  production.

**Signals.** `vidlock.signals.seal_finished(sender=Model, pk, state, error,
video_key)` fires after every sealing job. `key_abuse` and `viewer_flagged`
are described above.

**Rotating the key-encryption key.** Put the new secret first
(`'KEY_ENCRYPTION_KEYS': [new, old]`), deploy, run `vidlock_rewrap`, and
remove `old` once `vidlock_status` reports no keys waiting. Without
`KEY_ENCRYPTION_KEYS`, keys are derived from `SECRET_KEY` (and
`SECRET_KEY_FALLBACKS`). In that case, rotating `SECRET_KEY` without keeping
the old value as a fallback makes every sealed video unplayable. That is why
`manage.py check` asks you to set a separate secret.

**Backups.** Once sealed, a video is only playable with its key, and the key
lives in your database. Back up the database like the valuable thing it now
is. If you would also like the originals kept, set `KEEP_SOURCE = True`, or
recover any video later with `vidlock_export`.

## Settings

| Key | Default | |
|---|---|---|
| `BACKEND` | — | Dotted path to your `SealedBackend` subclass. |
| `STORAGE` | S3Storage | Dotted path to a storage class; `vidlock.storage.DjangoStorage` wraps a Django storage. |
| `S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_REGION` | | For the default storage. |
| `DJANGO_STORAGE` | `'default'` | Alias in `settings.STORAGES` used by `DjangoStorage`. |
| `FFMPEG_BINARY` / `FFPROBE_BINARY` | `ffmpeg` / beside ffmpeg | |
| `SEGMENT_SECONDS` | `10` | Each range is one GET on the bucket. `30` means a third of the billed requests and coarser seeking. |
| `KEY_ROTATION_SECONDS` | `60` | Seconds of video per key. `None`: one key per video. |
| `KEY_PACE` / `KEY_BURST` | `2.0` / `6` | Keys of a video go out no faster than this multiple of playback speed, after a burst of this many (for seeking). `0` turns pacing off. |
| `KEEP_SOURCE` | `False` | Keep the uploaded MP4 after sealing. |
| `TOKEN_TTL` | `600` | Seconds a token and a signed media URL live. |
| `KEY_FETCHES_PER_HOUR` | `20` | Depth limit: key fetches per viewer, video and hour. |
| `KEY_VIDEOS_PER_HOUR` | `30` | Breadth limit: different videos keyed per viewer and hour. `None` turns it off. |
| `STRICT_FETCH_METADATA` | `False` | Also refuse web key requests that carry no `Sec-Fetch-*` headers. Test with Safari before you enable it. |
| `REQUIRE_WRAPPED_KEY` | `False` | Refuse web keys outside the page's key exchange: generic download tools fed a copied cookie, and iPhones older than iOS 17.1. |
| `MAX_STREAMS` | `None` | Devices that may play at once per viewer; `1` stops account sharing. |
| `STREAM_TIMEOUT` / `HEARTBEAT_SECONDS` | `90` / `30` | A stream lapses after this long without a heartbeat / how often the player sends one. |
| `RISK_THRESHOLD` / `RISK_SUSPEND_SECONDS` / `RISK_WEIGHTS` | `10` / `0` / `{}` | The daily score that flags a viewer, how long a flagged viewer is paused (`0`: flag only), and weight overrides. |
| `MAX_NETWORKS_PER_DAY` | `6` | Networks (/24, /48) a viewer may use in a day before each new one adds to the score. |
| `WATERMARK_CODE` | `True` | Add the traceable code (and time) to the watermark. |
| `ON_KEY_ABUSE` | — | Dotted path to `fn(request, user, video)`, called once a day per viewer past a limit. |
| `KEY_ENCRYPTION_KEYS` | from `SECRET_KEY` | Secrets that encrypt the stored video keys; the first one encrypts. |
| `ENQUEUE_SEAL` | — | Dotted path to `fn(model, pk, video_key)` that queues sealing; used by the admin and `vidlock_seal --queue`. |
| `HLS_JS_URL` / `HLS_JS_INTEGRITY` | bundled copy | Load hls.js from elsewhere, with a Subresource Integrity hash. |

If several tenants or sites share one cache, override
`SealedBackend.namespace(request)` so their fetch counters stay apart.

## Translations

Server messages and the admin come in English, Arabic, French, Spanish,
Portuguese, German and Turkish, following Django's active language; the
player follows `<html lang>`. Corrections and new languages are welcome:
they live in `src/vidlock/locale/` and in `MESSAGES` inside `player.js`.

## Upgrading

**From 0.2 to 0.3**

* `cryptography` is now a dependency (for the key exchange and rotation).
* Videos sealed before 0.3 have one key and keep playing as they are. To give
  them rotating keys, re-seal them from their source: keep the MP4 around
  (`KEEP_SOURCE`) or recover it with `vidlock_export`.
* The player sends a heartbeat to the new `heartbeat` URL in `vidlock.urls`,
  which comes with the same `include()`.
* Use a shared cache (Redis, Memcached or the database cache) if you run more
  than one process. The new check `vidlock.W007` says so.
* The player needs WebCrypto for the key exchange: serve the site over HTTPS
  (or `localhost`). Over plain HTTP it falls back to raw keys, which count
  towards the risk score.

**From 0.1**

* Keys sealed by 0.1 are stored raw. They keep working; run
  `manage.py vidlock_rewrap` once to encrypt them.
* Tokens now carry session and account fingerprints. Tokens issued by 0.1
  keep working until they expire (ten minutes).
* The player now loads the bundled hls.js 1.7.3. Use `{% vidlock_player %}`,
  or set `HLS_JS_URL` to keep a CDN.
* `packager.probe()` returns a `Probe` object. It still unpacks as
  `video, audio = probe(path)`.
* New checks may skip 10-bit and 4:4:4 H.264 that 0.1 sealed but browsers
  could not play.

## Costs

On Cloudflare R2 (free egress), a 45-minute lesson at 10-second segments
costs about 270 class-B reads per full view, so the free 10 million a month
cover roughly 37,000 full views. Sealing costs about one second of one CPU
core per upload.

## FAQ

**Adaptive bitrate (several qualities)?** Not in vidlock. It would mean
re-encoding every upload, which is exactly the cost vidlock exists to avoid.
Upload at a sensible bitrate (for example 720p at 1.5–2.5 Mbps for lectures),
or encode your renditions before uploading.

**Can a determined user still get the video?** Yes; see *What it is not*.
The rotation, pace and risk score make it slow and noisy, one screen at a
time makes sharing the account pointless, and the watermark code makes a
leak point back to them.

**What about real DRM?** Pairing vidlock with Widevine/FairPlay via Shaka
Packager (CMAF, `cbcs`, still no re-encode) and a licence service such as
EZDRM or PallyCon is on the roadmap as an optional backend. It is the only
thing that also stops extensions from copying what the player hands to
MediaSource.

## Contributing

Issues and pull requests are welcome, in any language you are comfortable
with. See [CONTRIBUTING.md](CONTRIBUTING.md). For security problems, please
read [SECURITY.md](SECURITY.md) first and do not open a public issue.

## License

MIT © Nader Yasser. The bundled hls.js is © Dailymotion, Apache-2.0 (see
`src/vidlock/static/vidlock/vendor/hls.js.LICENSE`).

---

## بالعربي

**اقفل فيديوهات كورساتك من غير ميزانية DRM.** مكتبة Django بتحوّل كل فيديو
بيترفع لملف واحد مشفّر (HLS بـAES-128). اللي ينزّل الفيديو ياخد ملف مالوش
لازمة من غير المفتاح، والمفتاح بيروح بس للطالب اللي إنت سامحله يتفرج، ولمدة
١٠ دقايق.

**بتعمل إيه:**
- **بتشفّر الفيديو لوحدها بعد الرفع** بأمر ffmpeg واحد من غير إعادة ترميز،
  والـMP4 الأصلي بيتمسح (أو بيفضل لو فعّلت `KEEP_SOURCE`).
- **المفتاح نفسه متشفّر في الداتابيز،** فلو نسخة من الداتابيز اتسرّبت مش
  هتفتح أي فيديو.
- **الصلاحية بتتفحص مع كل طلب:** دالتك `can_watch` بتتسأل كل مرة، فلو
  الاشتراك اتلغى أو الطالب عمل تسجيل خروج أو غيّر الباسورد، بيتقفل عليه فورًا.
- **برامج التحميل مابتاخدش المفتاح:** زي yt-dlp وN_m3u8DL-RE، لأن التوكن
  مربوط بجلسة الطالب نفسها. واللي بيطلب مفاتيح كتير في الساعة بيترفض، سواء
  كرر نفس الفيديو أو حاول يسحب كورس كامل درس درس، وإنت بيوصلك تنبيه.
- **كل دقيقة من الفيديو ليها مفتاح،** والمفاتيح بتتسلّم بسرعة المشاهدة بس
  (بحد أقصى ضعف السرعة)، فاللي عايز يسحب درس كامل هيستنى طول الدرس تقريبًا،
  والمفتاح المنسوخ بيفتح دقيقة واحدة.
- **المفتاح عمره ما بيعدّي على الشبكة مكشوف:** بيتشفّر لصفحة الطالب نفسها
  (ECDH)، فنسخه من DevTools وإدخاله في برنامج تحميل مابيجيبش حاجة.
- **شاشة واحدة بس:** مع `MAX_STREAMS = 1`، لو الحساب اتفتح على جهاز تاني،
  الجهاز الأول بيقف برسالة. كده مشاركة الحساب مابقاش ليها لازمة.
- **درجة شك لكل طالب:** مفاتيح اتطلبت ومااتشغلتش، وإضافات «حفظ الفيديو»،
  وشبكات كتير في يوم واحد، وتبديل أجهزة… لما الدرجة توصل للحد بيوصلك تنبيه،
  وممكن الحساب يتوقف أوتوماتيك.
- **علامة مائية متحركة** باسم الطالب، ومعاها كود من ٦ حروف والوقت، ونسخة
  باهتة مالية الشاشة كلها، حتى في وضع ملء الشاشة. أمر `vidlock_trace` بيحوّل
  الكود اللي ظاهر في أي فيديو متسرّب للحساب اللي سرّبه.
- **بتشتغل على كل حاجة:** كروم وفايرفوكس بـhls.js (متضمّن جوه المكتبة)،
  وسفاري والآيفون، والأبلكيشن بـExoPlayer أو AVPlayer.
- **أي تخزين:** Cloudflare R2 وS3 وMinIO وB2، أو Google Cloud وAzure عن طريق
  django-storages.
- **أدوات تشغيل:** أوامر `vidlock_seal` و`vidlock_status` و`vidlock_rewrap`
  و`vidlock_export` و`vidlock_trace` و`vidlock_risk`، وإجراء «تشفير مرة أخرى» في لوحة الأدمن، وفحوصات
  `manage.py check`.
- **رسائل بالعربي** في السيرفر والأدمن والمشغّل، ومعاها الإنجليزي والفرنساوي
  والإسباني والبرتغالي والألماني والتركي.

**مش DRM:** حد فاهم ومصمّم يقدر في الآخر يوصل للفيديو. الهدف إن الملف المتحمّل
مايشتغلش، وإن البرامج السهلة تفشل، وإن السحب الكامل يبقى بطيء ومكشوف، وإن
مشاركة الحساب ماتنفعش، وإن أي تسريب يبان مين وراه.

**مهم:** بعد التشفير، المفتاح موجود في الداتابيز بس، فخلّي عندك نسخة احتياطية
منها. وحط `KEY_ENCRYPTION_KEYS` بسر مستقل عن `SECRET_KEY`.

المشاركة مرحّب بيها بالعربي أو بالإنجليزي: افتح Issue أو Pull Request،
والتفاصيل في [CONTRIBUTING.md](CONTRIBUTING.md).
