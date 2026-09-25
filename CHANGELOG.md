# Changelog

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
