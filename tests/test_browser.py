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
    settings.VIDLOCK = {
        **settings.VIDLOCK,
        'STORAGE': 'tests.testapp.storage.LiveStorage',
        # Three keys for the 25-second sample, one device at a time, quick heartbeats.
        'KEY_ROTATION_SECONDS': 10,
        'MAX_STREAMS': 1,
        'HEARTBEAT_SECONDS': 2,
    }
    # The bucket on another origin than the page, as in production.
    storage.BASE['url'] = live_server.url.replace('localhost', '127.0.0.1')
    backend.WATERMARK.append(True)
    storage.OBJECTS['lessons/1/upload.mp4'] = sample
    lesson = Lesson.objects.create(title='Lenz', video_key='lessons/1/upload.mp4')
    assert seal(Lesson, lesson.pk, lesson.video_key) == 'sealed'
    lesson.refresh_from_db()
    assert lesson.key_count == 3
    student = get_user_model().objects.create_user('amira', password='pw')
    lesson.students.add(student)
    client.force_login(student)
    first = client.cookies['sessionid'].value
    other = client_class()
    other.force_login(student)  # the same account on a second device
    yield lesson, first, other.cookies['sessionid'].value
    backend.WATERMARK.clear()


def client_class():
    from django.test import Client

    return Client()


def open_page(browser, base, session, lesson):
    context = browser.new_context()
    context.add_cookies([{'name': 'sessionid', 'value': session, 'url': base}])
    page = context.new_page()
    seen = {'keys': [], 'shares': 0, 'beats': []}

    def on_request(request):
        if f'/sealed/{lesson.pk}/key' in request.url and request.headers.get('x-vidlock-key-share'):
            seen['shares'] += 1

    def on_response(response):
        if f'/sealed/{lesson.pk}/key' in response.url:
            seen['keys'].append((response.status, response.headers.get('content-type')))
        if f'/sealed/{lesson.pk}/heartbeat' in response.url:
            seen['beats'].append(response.status)

    page.on('request', on_request)
    page.on('response', on_response)
    page.goto(f'{base}/watch/{lesson.pk}/')
    return context, page, seen


def test_a_sealed_video_is_keyed_decrypted_and_watermarked(watching, live_server, browser):
    # The database work (in `watching`) runs before Playwright starts its event loop.
    lesson, session, second_session = watching
    base = live_server.url
    context, page, seen = open_page(browser, base, session, lesson)
    page.wait_for_function('vidlockTest.parsed > 0 || vidlockTest.errors.length > 0', timeout=20000)
    state = page.evaluate('vidlockTest')

    # The key crossed sealed to this page, and hls.js decrypted the stream with it.
    assert seen['keys'][0] == (200, 'application/vnd.vidlock.wrapped-key'), seen
    assert seen['shares'] == len(seen['keys'])
    assert state['parsed'] > 0, state
    assert any(codec.startswith('avc1') for codec in state['codecs']), state

    mark = page.inner_text('.vidlock-mark')
    assert mark.startswith('amira · ') and len(mark.split(' · ')[1]) == 6, mark
    assert page.query_selector('.vidlock-pattern') is not None

    playable = page.evaluate(
        """() => MediaSource.isTypeSupported('video/mp4; codecs="avc1.64001e,mp4a.40.2"')"""
    )
    if playable:
        # Google Chrome: it plays, through the keys of later periods too.
        page.evaluate("document.getElementById('player').play()")
        page.wait_for_function("document.getElementById('player').currentTime > 12", timeout=30000)
        assert len({status for status, _ in seen['keys']}) == 1 and len(seen['keys']) >= 2, seen

    for _ in range(40):
        if 200 in seen['beats']:
            break
        page.wait_for_timeout(250)
    assert 200 in seen['beats'], 'the heartbeat keeps the stream'

    # The same key URL pasted into another client, without the session: refused.
    key_url = page.evaluate(
        "performance.getEntriesByType('resource').map(e => e.name).find(u => u.includes('/key?'))"
    )
    assert browser.new_context().request.get(key_url).status == 403

    # The same account opens the video on a second device: the first one stops.
    other_context, other_page, _ = open_page(browser, base, second_session, lesson)
    other_page.wait_for_function('vidlockTest.parsed > 0 || vidlockTest.errors.length > 0', timeout=20000)
    page.wait_for_function(
        "document.getElementById('status').textContent.includes('another device')", timeout=10000
    )
    assert 409 in seen['beats']

    other_page.evaluate('player.destroy()')
    assert other_page.query_selector('.vidlock-mark') is None
    context.close()
    other_context.close()
