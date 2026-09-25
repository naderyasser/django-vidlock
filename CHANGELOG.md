# Changelog

## 0.1.0 — 2026-09-25

First release, extracted from a production Egyptian course platform.

* Single-file AES-128 HLS sealing with `ffmpeg -c copy`; the source is deleted after sealing.
* Per-viewer, per-video playlist and key tokens with entitlement checked on every request.
* Web tokens need the viewer's session cookie for the key; hourly key-fetch limit with an abuse hook.
* S3-compatible storage (R2, S3, MinIO, B2) or your own class.
* `player.js`: hls.js / native HLS with signed-URL renewal that does not reload the stream.
