# Contributing

Thanks for helping. Issues and pull requests in English or Arabic are both fine.

## Set up

```bash
git clone https://github.com/naderyasser/django-vidlock
cd django-vidlock
python -m venv .venv && . .venv/bin/activate
pip install -e ".[test,s3]" ruff black
pytest
```

The packaging tests need `ffmpeg` with libx264 on your PATH (or point
`VIDLOCK_TEST_FFMPEG` at a binary); without it they are skipped, not failed.

## Before you open a pull request

* `ruff check . && black --check .`
* `pytest` passes, and a change in behaviour comes with a test. The tests that
  matter most here are negative ones: a token that must *not* open a key.
* Keep the three rules the design rests on:
  1. **Never re-encode.** Cheap sealing is the point; a source that cannot be
     remuxed is skipped, not transcoded.
  2. **A failure never takes a video down.** Anything that goes wrong leaves
     the uploaded file playing and no orphan object in the bucket.
  3. **Entitlement is asked on every playlist and key request**, not only on
     page load.

## Good first issues

Look for the `good first issue` label. Ideas that would help many people:
a Django admin action to re-seal, a management command for existing videos,
a Google Cloud Storage backend, and examples for RQ / Huey / Django-Q.

## Security

Please do not open a public issue for a way around the protection — see
[SECURITY.md](SECURITY.md).
