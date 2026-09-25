"""End to end in a real browser: page → playback endpoint → playlist → key →
encrypted byte ranges from a cross-origin "bucket" → hls.js decrypts them.

Needs ffmpeg and Playwright. Point VIDLOCK_TEST_BROWSER at a Chromium binary,
or VIDLOCK_TEST_BROWSER_CHANNEL at an installed channel ('chrome'). Open-source
Chromium builds cannot decode H.264, so there the test stops once hls.js
has decrypted and demuxed the stream;
Google Chrome goes on to play.
"""

import os

import pytest
from django.contrib.auth import get_user_model

from tests.testapp import backend, storage
from tests.testapp.models import Lesson
from vidlock.pipeline import seal

playwright = pytest.importorskip('playwright.sync_api')

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def browser():
    options = {}
    if os.environ.get('VIDLOCK_TEST_BROWSER'):
        options['executable_path'] = os.environ['VIDLOCK_TEST_BROWSER']
    if os.environ.get('VIDLOCK_TEST_BROWSER_CHANNEL'):
        options['channel'] = os.environ['VIDLOCK_TEST_BROWSER_CHANNEL']
    with playwright.sync_playwright() as p:
        try:
            instance = p.chromium.launch(**options)
        except Exception as exc:
            pytest.skip(f'no browser to test with: {str(exc).splitlines()[0]}')
        yield instance
        instance.close()


@pytest.fixture
def watching(sample, live_server, settings, client):
    settings.VIDLOCK = {**settings.VIDLOCK, 'STORAGE': 'tests.testapp.storage.LiveStorage'}
    # The bucket on another origin than the page, as in production.
    storage.BASE['url'] = live_server.url.replace('localhost', '127.0.0.1')
    backend.WATERMARK.append(True)
    storage.OBJECTS['lessons/1/upload.mp4'] = sample
    lesson = Lesson.objects.create(title='Lenz', video_key='lessons/1/upload.mp4')
    assert seal(Lesson, lesson.pk, lesson.video_key) == 'sealed'
    student = get_user_model().objects.create_user('amira', password='pw')
    lesson.students.add(student)
    client.force_login(student)
    yield lesson, client.cookies['sessionid'].value
    backend.WATERMARK.clear()


def test_a_sealed_video_is_keyed_decrypted_and_watermarked(watching, live_server, browser):
    # The database work (in `watching`) runs before Playwright starts its event loop.
    lesson, session = watching
    base = live_server.url
    context = browser.new_context()
    context.add_cookies([{'name': 'sessionid', 'value': session, 'url': base}])
    page = context.new_page()
    keys = []
    page.on('response', lambda r: keys.append(r.status) if f'/sealed/{lesson.pk}/key' in r.url else None)

    page.goto(f'{base}/watch/{lesson.pk}/')
    page.wait_for_function('vidlockTest.parsed > 0 || vidlockTest.errors.length > 0', timeout=20000)
    state = page.evaluate('vidlockTest')

    assert keys == [200], 'one key fetch, granted'
    assert state['parsed'] > 0, state
    assert any(codec.startswith('avc1') for codec in state['codecs']), state
    assert page.inner_text('.vidlock-mark') == 'amira'

    playable = page.evaluate(
        """() => MediaSource.isTypeSupported('video/mp4; codecs="avc1.64001e,mp4a.40.2"')"""
    )
    if playable:
        page.evaluate("document.getElementById('player').play()")
        page.wait_for_function("document.getElementById('player').currentTime > 1", timeout=20000)

    # The same key URL pasted into another client, without the session: refused.
    key_url = page.evaluate(
        "performance.getEntriesByType('resource').map(e => e.name).find(u => u.includes('/key?'))"
    )
    other = browser.new_context().request.get(key_url)
    assert other.status == 403

    page.evaluate('player.destroy()')
    assert page.query_selector('.vidlock-mark') is None
    context.close()
