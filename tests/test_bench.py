"""scripts/vidlock_bench.py and manage.py vidlock_bench_url."""

import importlib.util
import io
import json
import os
from contextlib import redirect_stdout

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command

from tests.testapp import storage
from tests.testapp.models import Lesson
from vidlock.pipeline import seal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    'vidlock_bench', os.path.join(ROOT, 'scripts', 'vidlock_bench.py')
)
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)

PLAYLIST = (
    '#EXTM3U\n#EXT-X-VERSION:4\n#EXT-X-KEY:METHOD=AES-128,URI="https://site/key?t=x"\n'
    '#EXTINF:10.0,\n#EXT-X-BYTERANGE:1000@0\nhttps://acct.r2.cloudflarestorage.com/b/v.ts?X-Amz-Signature=s\n'
    '#EXTINF:10.0,\n#EXT-X-BYTERANGE:500\nhttps://acct.r2.cloudflarestorage.com/b/v.ts?X-Amz-Signature=s\n'
    '#EXTINF:4.5,\n#EXT-X-BYTERANGE:250@1500\nhttps://acct.r2.cloudflarestorage.com/b/v.ts?X-Amz-Signature=s\n'
    '#EXT-X-ENDLIST\n'
)


def test_it_reads_the_byte_ranges_and_never_the_keys():
    segments = bench.parse_playlist(PLAYLIST)
    assert [(s['start'], s['length'], s['duration']) for s in segments] == [
        (0, 1000, 10.0),
        (1000, 500, 10.0),
        (1500, 250, 4.5),
    ]
    assert all('key' not in s['url'] for s in segments)
    assert bench.delivery_of(segments[0]['url']) == 'direct-r2'
    assert bench.delivery_of('https://media.example/v/x.ts?exp=1&sig=a') == 'edge-or-other'


def test_plans():
    segments = [{}] * 50
    assert bench.plan(segments, 'all', 0, 0, 1) == list(range(50))
    played = bench.plan(segments, 'play', 30, 10, 1)
    assert played[:30] == list(range(30)) and len(played) == 40 and len(set(played)) == 40
    assert bench.plan(segments, 'play', 30, 10, 1) == played, 'same seed, same seeks'
    assert len(bench.plan(segments, 'seek', 30, 10, 1)) == 10


def test_colo_and_percentiles():
    assert bench.colo_of({'cf-ray': '8f1d2c3b4a5e6f70-CAI'}) == 'CAI'
    assert bench.colo_of({}) == ''
    assert bench.percentile([5, 1, 3, 2, 4], 0.5) == 3
    assert bench.percentile([], 0.9) == 0.0


@pytest.fixture
def account():
    return get_user_model().objects.create_user('bench', password='pw')


@pytest.fixture
def sealed_lesson(sample, account):
    storage.OBJECTS['l/a.mp4'] = sample
    lesson = Lesson.objects.create(title='x', video_key='l/a.mp4')
    lesson.students.add(account)
    assert seal(Lesson, lesson.pk, 'l/a.mp4') == 'sealed'
    lesson.refresh_from_db()
    return lesson


@pytest.mark.django_db
def test_bench_url(sealed_lesson, account):
    out, err = io.StringIO(), io.StringIO()
    call_command(
        'vidlock_bench_url', 'testapp.Lesson', str(sealed_lesson.pk), '--user', 'bench',
        '--base', 'https://courses.example/', stdout=out, stderr=err,
    )  # fmt: skip
    url = out.getvalue().strip()
    assert url.startswith(f'https://courses.example/sealed/{sealed_lesson.pk}/media.m3u8?t=')
    assert 'valid for 600 s' in err.getvalue()


@pytest.mark.django_db
def test_bench_url_refuses_what_the_account_may_not_watch(sealed_lesson):
    get_user_model().objects.create_user('stranger')
    with pytest.raises(CommandError, match='can_watch'):
        call_command('vidlock_bench_url', 'testapp.Lesson', str(sealed_lesson.pk), '--user', 'stranger')
    with pytest.raises(CommandError, match='not a sealed video'):
        call_command('vidlock_bench_url', 'testapp.Lesson', '999', '--user', 'stranger')


@pytest.mark.django_db(transaction=True)
def test_a_real_run_against_a_bucket_that_answers_ranges(
    sealed_lesson, account, live_server, settings, tmp_path
):
    """End to end: the command's URL, the playlist view, and ranges from a
    (fake) bucket over real HTTP; no key is ever requested."""
    settings.VIDLOCK = {**settings.VIDLOCK, 'STORAGE': 'tests.testapp.storage.LiveStorage'}
    storage.BASE['url'] = live_server.url
    out = io.StringIO()
    call_command(
        'vidlock_bench_url', 'testapp.Lesson', str(sealed_lesson.pk), '--user', 'bench',
        '--base', live_server.url, stdout=out, stderr=io.StringIO(),
    )  # fmt: skip
    report = tmp_path / 'run.json'
    printed = io.StringIO()
    with redirect_stdout(printed):
        code = bench.main(
            [
                out.getvalue().strip(),
                '--label',
                'test',
                '--passes',
                '2',
                '--seeks',
                '1',
                '--json',
                str(report),
            ]
        )
    assert code == 0, printed.getvalue()
    data = json.loads(report.read_text())
    assert data['delivery'] == 'edge-or-other' and data['playlist']['status'] == 200
    rows = data['rows']
    assert len(rows) == 2 * 4, 'three segments in order, one seek, twice'
    assert all(r['status'] == 206 and r['range_ok'] and r['content_range'] for r in rows)
    assert {r['pass'] for r in rows} == {1, 2}
    assert data['summary']['1']['errors'] == 0 and data['summary']['1']['ttfb_p50_ms'] > 0
    assert 'pass 2:' in printed.getvalue()

    compared = io.StringIO()
    with redirect_stdout(compared):
        bench.main(['--compare', str(report), str(report)])
    assert compared.getvalue().count('test') == 4


def test_it_flags_a_server_that_ignores_range(monkeypatch):
    class Ignoring(bench.Connection):
        def get(self, url, headers):
            return {'status': 200, 'headers': {}, 'bytes': 5000, 'connect_ms': 1, 'ttfb_ms': 2, 'total_ms': 3}

    monkeypatch.setattr(bench, 'Connection', Ignoring)
    rows = bench.measure(bench.parse_playlist(PLAYLIST), [0], 1, 5, verbose=False)
    assert rows[0]['range_ok'] is False
