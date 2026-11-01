"""Edge delivery (vidlock.edge): signed URLs, the views, the checks, and the
real Worker code verifying what Python signs."""

import json
import os
import re
import shutil
import subprocess
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory

from tests.test_views import PLAYLIST
from tests.testapp.models import Lesson
from vidlock import edge, tokens
from vidlock.views import playback_info

pytestmark = pytest.mark.django_db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, 'edge', 'cloudflare-worker', 'test', 'serve-once.mjs')
SECRET = 'new-secret-0123456789abcdef0123456789'
OLD = 'old-secret-0123456789abcdef0123456789'
KEY = 'lessons/1/abc.ts'


@pytest.fixture
def on(settings):
    settings.VIDLOCK = {
        **settings.VIDLOCK,
        'DELIVERY': 'edge',
        'EDGE_URL': 'https://media.example/',
        'EDGE_SECRETS': [SECRET, OLD],
    }
    return settings


@pytest.fixture
def student():
    return get_user_model().objects.create_user('student', password='pw')


@pytest.fixture
def lesson(student):
    lesson = Lesson.objects.create(
        title='x', video_key=KEY, sealed_state='sealed', sealed_playlist=PLAYLIST, sealed_key=b'k' * 16
    )
    lesson.students.add(student)
    return lesson


def parts(url):
    split = urlsplit(url)
    query = {k: v[0] for k, v in parse_qs(split.query, keep_blank_values=True).items()}
    return split, query


def test_a_signed_url(on):
    url = edge.url_for(KEY, 600, 'abc123def456', now=1_000_000)
    split, query = parts(url)
    assert f'{split.scheme}://{split.netloc}{split.path}' == f'https://media.example/{KEY}'
    assert query['exp'] == '1000600' and query['v'] == 'abc123def456' and len(query['sig']) == 64
    assert edge.verify(KEY, 1000600, 'abc123def456', query['sig'], now=1_000_000)
    assert not edge.verify(KEY, 1000600, '000000000000', query['sig'], now=1_000_000)
    assert not edge.verify('lessons/1/other.ts', 1000600, 'abc123def456', query['sig'], now=1_000_000)
    assert not edge.verify(KEY, 1000600, 'abc123def456', query['sig'], now=1_000_601), 'expired'


def test_rotation(on):
    url = edge.url_for(KEY, 600, '', now=1_000_000)
    sig = parts(url)[1]['sig']
    on.VIDLOCK = {**on.VIDLOCK, 'EDGE_SECRETS': ['brand-new-secret-0123456789abcdef0123', SECRET]}
    assert edge.verify(KEY, 1000600, '', sig, now=1_000_000), 'still listed'
    assert parts(edge.url_for(KEY, 600, '', now=1_000_000))[1]['sig'] != sig, 'the new one signs'
    on.VIDLOCK = {**on.VIDLOCK, 'EDGE_SECRETS': ['brand-new-secret-0123456789abcdef0123']}
    assert not edge.verify(KEY, 1000600, '', sig, now=1_000_000)


def test_only_sealed_objects_and_capped_lifetimes(on):
    with pytest.raises(ValueError):
        edge.url_for('lessons/1/upload.mp4', 600)
    with pytest.raises(ValueError, match='viewer tag'):
        edge.url_for(KEY, 600, 'doctor')  # the Worker would refuse it
    expires = int(parts(edge.url_for(KEY, 30 * 24 * 3600, now=0))[1]['exp'])
    assert expires == edge.MAX_TTL


def test_misconfigured(settings):
    settings.VIDLOCK = {**settings.VIDLOCK, 'DELIVERY': 'edge', 'EDGE_URL': 'https://m.example'}
    with pytest.raises(ImproperlyConfigured, match='EDGE_SECRETS'):
        edge.url_for(KEY, 60)
    settings.VIDLOCK = {**settings.VIDLOCK, 'EDGE_URL': '', 'EDGE_SECRETS': [SECRET]}
    with pytest.raises(ImproperlyConfigured, match='EDGE_URL'):
        edge.url_for(KEY, 60)


def test_viewer_tags_name_a_stream_without_naming_the_user():
    tag = edge.viewer_tag(42, 'lease-a')
    assert re.fullmatch(r'[0-9a-f]{12}', tag)
    assert (
        tag
        == edge.viewer_tag(42, 'lease-a')
        != edge.viewer_tag(42, 'lease-b')
        != edge.viewer_tag(43, 'lease-a')
    )


# -- the views ------------------------------------------------------------------------


def info(lesson, user):
    request = RequestFactory().get('/')
    request.user = user
    return playback_info(request, lesson)


def test_direct_by_default(lesson, student):
    assert info(lesson, student)['media_url'] == f'https://bucket.example/{KEY}?sig=1'


def test_edge_media_url_in_playback_info(on, lesson, student):
    data = info(lesson, student)
    split, query = parts(data['media_url'])
    assert split.netloc == 'media.example' and split.path == f'/{KEY}'
    assert edge.verify(KEY, int(query['exp']), query['v'], query['sig'])
    assert int(query['exp']) - time.time() == pytest.approx(600, abs=5)
    # Keys and playlists are still Django's.
    assert data['key_url'].startswith('http://testserver/sealed/') and '/media.m3u8' in data['url']


def test_edge_media_url_in_the_playlist(on, client, lesson, student):
    t = tokens.sign(lesson.pk, student.pk, tokens.APP)
    body = client.get(f'/sealed/{lesson.pk}/media.m3u8', {'t': t}).content.decode()
    media = [line for line in body.splitlines() if line.startswith('https://media.example/')]
    assert len(media) == 2
    _, query = parts(media[0])
    # Native players read the playlist once: the URL outlives the video.
    assert int(query['exp']) - time.time() == pytest.approx(15 + 600, abs=5)
    assert '/sealed/' in body and 'key?t=' in body, 'keys from Django'


def test_an_unsealed_video_is_never_sent_to_the_edge(on, student):
    lesson = Lesson.objects.create(title='x', video_key='lessons/1/upload.mp4')
    assert info(lesson, student)['url'] == 'https://bucket.example/lessons/1/upload.mp4?sig=1'


def test_the_playlist_still_checks_the_viewer(on, client, lesson, student):
    lesson.students.remove(student)
    t = tokens.sign(lesson.pk, student.pk, tokens.APP)
    assert client.get(f'/sealed/{lesson.pk}/media.m3u8', {'t': t}).status_code == 403


# -- checks ---------------------------------------------------------------------------


def test_checks(settings):
    from vidlock.checks import check_settings

    def ids():
        return {p.id for p in check_settings(None)}

    assert not {'vidlock.E008', 'vidlock.E009', 'vidlock.W011', 'vidlock.W012'} & ids()
    settings.VIDLOCK = {**settings.VIDLOCK, 'DELIVERY': 'cdn'}
    assert 'vidlock.E008' in ids()
    settings.VIDLOCK = {**settings.VIDLOCK, 'DELIVERY': 'edge'}
    assert 'vidlock.E009' in ids()
    settings.VIDLOCK = {**settings.VIDLOCK, 'EDGE_URL': 'http://media.example', 'EDGE_SECRETS': ['short']}
    assert {'vidlock.W011', 'vidlock.W012'} <= ids() and 'vidlock.E009' not in ids()


# -- Python signs, the Worker verifies ----------------------------------------------------

needs_node = pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')


def through_worker(url, requests, secrets=f'{SECRET},{OLD}', origins='https://courses.example'):
    spec = {'url': url, 'secrets': secrets, 'key': KEY, 'origins': origins, 'requests': requests}
    run = subprocess.run(
        ['node', WORKER, json.dumps(spec)], capture_output=True, text=True, timeout=60, check=False
    )
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@needs_node
def test_the_worker_accepts_what_python_signs(on):
    url = edge.url_for(KEY, 600, edge.viewer_tag(7, 'lease'))
    out = through_worker(
        url,
        [
            {'range': 'bytes=0-99', 'origin': 'https://courses.example'},
            {'range': 'bytes=0-99'},
            {'range': 'bytes=100-199'},
        ],
    )
    first, second, third = out['responses']
    assert first['status'] == 206 and first['headers']['content-range'] == 'bytes 0-99/1000'
    assert first['headers']['x-vidlock-cache'] == 'MISS' and first['bytes'] == 100
    assert first['headers']['access-control-allow-origin'] == 'https://courses.example'
    assert second['headers']['x-vidlock-cache'] == 'HIT'
    assert third['headers']['x-vidlock-cache'] == 'MISS'
    assert out['r2_gets'] == 2


@needs_node
def test_the_worker_refuses_what_python_did_not_sign(on):
    url = edge.url_for(KEY, 600, 'abc123def456')
    split, query = parts(url)
    base = f'{split.scheme}://{split.netloc}{split.path}'
    forged = f'{base}?exp={query["exp"]}&v=000000000000&sig={query["sig"]}'
    later = f'{base}?exp={int(query["exp"]) + 60}&v={query["v"]}&sig={query["sig"]}'
    out = through_worker(url, [{'url': forged, 'range': 'bytes=0-9'}, {'url': later, 'range': 'bytes=0-9'}])
    assert [r['status'] for r in out['responses']] == [403, 403] and out['r2_gets'] == 0
    old_only = through_worker(url, [{'range': 'bytes=0-9'}], secrets=OLD)
    assert old_only['responses'][0]['status'] == 403, 'signed with the first secret, which this Worker lacks'


def test_doctor(on, capsys):
    from django.core.management import call_command

    call_command('vidlock_doctor', '--offline')
    assert 'edge — URLs sign (not fetched: --offline)' in capsys.readouterr().out
    on.VIDLOCK = {**on.VIDLOCK, 'DELIVERY': 'direct'}
    call_command('vidlock_doctor', '--offline')
    assert "edge — off (VIDLOCK['DELIVERY'] = 'direct')" in capsys.readouterr().out
