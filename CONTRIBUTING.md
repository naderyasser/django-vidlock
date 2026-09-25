# Contributing

Thanks for helping. Issues and pull requests are welcome in English, Arabic,
or whatever language you write best — we will manage.

## Set up

```bash
git clone https://github.com/naderyasser/django-vidlock
cd django-vidlock
python -m venv .venv && . .venv/bin/activate
pip install -e ".[test,s3,browser]" ruff==0.15.8 black==24.8.0
pytest
```

The packaging tests need `ffmpeg` (with libx264) and `ffprobe` on your PATH,
or point `VIDLOCK_TEST_FFMPEG` at a binary; without them they are skipped,
not failed.

The browser test (`tests/test_browser.py`) drives hls.js in a real browser.
Set `VIDLOCK_TEST_BROWSER_CHANNEL=chrome` to use an installed Google Chrome,
which also plays the video, or `VIDLOCK_TEST_BROWSER=/path/to/chromium`.
Open-source Chromium cannot decode H.264, so there the test stops once the
stream is decrypted and demuxed.

## Before you open a pull request

* `ruff check . && black --check .`
* `node --check src/vidlock/static/vidlock/player.js`
* `pytest` passes, and a change in behaviour comes with a test. The tests that
  matter most here are negative ones: a token that must *not* open a key.
* Keep the three rules the design rests on:
  1. **Never re-encode.** Cheap sealing is the point; a source that cannot be
     remuxed is skipped, not transcoded.
  2. **A failure never takes a video down.** Anything that goes wrong leaves
     the uploaded file playing and no orphan object in the bucket.
  3. **Entitlement is asked on every playlist and key request**, not only on
     page load.

## Translations

Server and admin strings live in `src/vidlock/locale/`. To add a language:

```bash
cd src/vidlock
DJANGO_SETTINGS_MODULE=tests.settings PYTHONPATH=../..:.. django-admin makemessages -l <code>
# translate locale/<code>/LC_MESSAGES/django.po, then
DJANGO_SETTINGS_MODULE=tests.settings PYTHONPATH=../..:.. django-admin compilemessages
```

and add the player's five messages to `MESSAGES` in `player.js`.

## Updating hls.js

Replace `src/vidlock/static/vidlock/vendor/hls.light.min.js` with the new
release's `dist/hls.light.min.js` (and its LICENSE), update the version in
the README and in `player.js`'s CDN fallback, and run the browser test.

## Releasing

Bump `__version__` in `src/vidlock/__init__.py`, add a CHANGELOG entry, merge,
then push a tag `v<version>`. The release workflow publishes to PyPI through
Trusted Publishing and creates the GitHub release.

## Good first issues

Look for the `good first issue` label. Ideas that would help many people:
more translations, examples for more task runners and hosting platforms, and
a player integration for Video.js or Plyr.

## Security

Please do not open a public issue for a way around the protection — see
[SECURITY.md](SECURITY.md).
