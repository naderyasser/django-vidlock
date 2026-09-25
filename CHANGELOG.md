# Changelog

## 0.3.0 — 2026-09-25

### Protection

* **Key rotation.** Every `KEY_ROTATION_SECONDS` (60) of video gets its own
  key and IV, still inside one `.ts` and still without re-encoding: ffmpeg
  remuxes, and vidlock encrypts each byte range.
* **Key pace.** A video's keys go out no faster than `KEY_PACE` (2×) playback
  speed per viewer, after a `KEY_BURST` for seeking. A new token does not
  refill the bucket.
* **Key exchange.** The bundled player makes an ECDH P-256 key pair per page,
  and each key comes back sealed to it (HKDF-SHA256, AES-GCM), so a key
  copied from DevTools is useless. `REQUIRE_WRAPPED_KEY` refuses raw keys to
  web tokens.
* **One screen at a time.** `MAX_STREAMS` limits the devices a viewer can
  play on at once. A new device takes the stream over and the old one stops
  with a message; the player's own renewals never take a stream back.
* **Risk score.** Refused keys, keys that are never played, raw keys,
  takeovers, many networks and tampered MediaSource functions add up per
  viewer and day. Past `RISK_THRESHOLD` vidlock sends `viewer_flagged` and
  can pause the account (`RISK_SUSPEND_SECONDS`).
* **Watermark.** Adds a six-letter code per viewer and day plus the time,
  and a faint full-frame copy that cropping cannot remove. It resists being
  hidden or faded from the console, and the player refuses iPhone's native
  fullscreen, where nothing can be drawn over the video.
  `manage.py vidlock_trace` turns a code back into the account.

### Everything else

* Heartbeat endpoint (`vidlock:heartbeat`); `playback_info` returns
  `heartbeat_url`, `heartbeat_interval` and `key_exchange`.
* `manage.py vidlock_risk` to see a score and lift a pause;
  `SealedBackend.client_ip()`; the player's `onEvicted` option.
* Depth is now counted per key and breadth per video. Rotation does not trip
  either limit.
* New check `vidlock.W007` for a cache that is not shared between processes.
* `cryptography` is a dependency.
* Videos sealed by 0.2 keep one key and keep playing.

## 0.2.0 — 2026-09-25

### Security

* Video keys are encrypted at rest under `KEY_ENCRYPTION_KEYS` (derived from
  `SECRET_KEY` when unset), with rotation and `manage.py vidlock_rewrap`.
  Keys stored raw by 0.1 still play.
* Web tokens are bound to the login session: logging out voids them. Every
  token is bound to the password hash: a password change voids them.
* Breadth limit `KEY_VIDEOS_PER_HOUR` catches a course harvested one lesson
  at a time, which the per-video limit never saw.
* Fetch Metadata: a web key opened in a tab or requested cross-site is
  refused; `STRICT_FETCH_METADATA` also refuses requests without the headers.
* Key and playlist responses send `nosniff` and `no-referrer`.

### Playback

* hls.js 1.7.3 ships inside the package; `{% vidlock_player %}` wires it up.
  `HLS_JS_URL` (unused in 0.1) now works, with `HLS_JS_INTEGRITY` for SRI.
* Safari/iOS native HLS no longer reloads every nine minutes: the playlist's
  media URL is signed for the whole video, and renewals leave it alone.
* Moving, self-repairing watermark from `SealedBackend.watermark()`, kept
  visible in fullscreen.
* Player messages in English, Arabic, French, Spanish, Portuguese, German and
  Turkish; `hlsConfig` and `onHls` options; `destroy()` now cleans up fully;
  renewal pauses while a paused tab is hidden.

### Sealing

* Probing uses `ffprobe` JSON (falls back to `ffmpeg -i`) and refuses H.264
  that browsers cannot decode (10-bit, 4:2:2, 4:4:4).
* `KEEP_SOURCE` keeps the uploaded MP4; `vidlock_export` decrypts a sealed
  video back to a playable MP4.
* `vidlock.signals.seal_finished` after every job.

### Operations

* Commands: `vidlock_seal`, `vidlock_status`, `vidlock_rewrap`, `vidlock_export`.
* `vidlock.admin.SealedVideoAdminMixin` with a status column and a "Seal again" action.
* System checks for missing or mistyped settings, ffmpeg and key secrets.
* `vidlock.storage.DjangoStorage` for Google Cloud Storage, Azure or any Django storage.
* `sealed_duration` on the model and `duration` in `playback_info`.
* Server and admin strings translated (ar, fr, es, pt, de, tr); type hints and `py.typed`.

### Project

* Tested on Python 3.10–3.14 with Django 4.2, 5.1, 5.2 and 6.0, with coverage,
  an end-to-end browser test (hls.js fetches the key and decrypts), and a
  release workflow for PyPI Trusted Publishing.

## 0.1.0 — 2026-09-25

First release, extracted from a production Egyptian course platform.

* Single-file AES-128 HLS sealing with `ffmpeg -c copy`; the source is deleted after sealing.
* Per-viewer, per-video playlist and key tokens with entitlement checked on every request.
* Web tokens need the viewer's session cookie for the key; hourly key-fetch limit with an abuse hook.
* S3-compatible storage (R2, S3, MinIO, B2) or your own class.
* `player.js`: hls.js / native HLS with signed-URL renewal that does not reload the stream.
