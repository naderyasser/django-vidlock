"""Transcripts: captions and search."""

import io
import os
import shutil
import subprocess
from typing import ClassVar
from urllib.parse import urlsplit

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.dispatch import receiver
from django.test import RequestFactory

from tests.testapp import storage
from tests.testapp.models import Lesson
from vidlock import signals, transcribe
from vidlock.models import Transcript
from vidlock.pipeline import seal
from vidlock.views import playback_info

pytestmark = pytest.mark.django_db

LINES = [
    (0.0, 2.5, 'مرحبا بكم في درس الإحتمالات'),
    (2.5, 6.0, 'Today: the lens formula.'),
    (6.0, 9.0, 'Focal length --> f'),
]


class FakeTranscriber:
    calls: ClassVar[list] = []

    def transcribe(self, audio_path, language):
        assert os.path.getsize(audio_path) > 1000, 'real audio was extracted'
        FakeTranscriber.calls.append(language)
        return 'ar', LINES


class BrokenTranscriber:
    def transcribe(self, audio_path, language):
        raise RuntimeError('model exploded')


@pytest.fixture
def lesson(sample):
    storage.OBJECTS['l/a.mp4'] = sample
    return Lesson.objects.create(title='x', video_key='l/a.mp4')


@pytest.fixture
def fake(settings):
    FakeTranscriber.calls.clear()
    settings.VIDLOCK = {
        **settings.VIDLOCK,
        'TRANSCRIBER': 'tests.test_transcribe.FakeTranscriber',
        'TRANSCRIBE_LANGUAGE': 'ar',
    }


def test_sealing_transcribes_after_the_video_is_live(lesson, fake):
    seen = []

    @receiver(signals.transcript_ready, sender=Lesson, weak=False)
    def listen(sender, video, transcript, **kwargs):
        seen.append((video.pk, video.is_sealed, len(transcript.segments)))

    try:
        assert seal(Lesson, lesson.pk, 'l/a.mp4') == 'sealed'
    finally:
        signals.transcript_ready.disconnect(listen, sender=Lesson)
    found = Transcript.objects.get()
    assert found.language == 'ar' and found.segments[1] == {'start': 2.5, 'end': 6.0, 'text': LINES[1][2]}
    assert seen == [(lesson.pk, True, 3)], 'the video was live when the transcript arrived'
    assert FakeTranscriber.calls == ['ar']
    assert not [f for f in os.listdir('/tmp') if f.startswith('vidlock-audio-')], 'no audio left behind'


def test_a_transcriber_failure_never_fails_the_seal(lesson, settings):
    settings.VIDLOCK = {**settings.VIDLOCK, 'TRANSCRIBER': 'tests.test_transcribe.BrokenTranscriber'}
    assert seal(Lesson, lesson.pk, 'l/a.mp4') == 'sealed'
    assert not Transcript.objects.exists()


def test_captions_for_the_player(client, lesson, fake):
    seal(Lesson, lesson.pk, 'l/a.mp4')
    lesson.refresh_from_db()
    student = get_user_model().objects.create_user('amira', password='pw')
    lesson.students.add(student)
    request = RequestFactory().get('/')
    request.user, request.auth = student, object()
    info = playback_info(request, lesson)
    assert info['captions_language'] == 'ar'
    url = urlsplit(info['captions_url'])
    vtt = client.get(f'{url.path}?{url.query}')
    assert vtt['Content-Type'].startswith('text/vtt') and 'no-store' in vtt['Cache-Control']
    body = vtt.content.decode()
    assert body.startswith('WEBVTT') and '00:00:02.500 --> 00:00:06.000' in body
    assert 'Focal length → f' in body, 'a cue cannot break the file'
    assert client.get(f'{url.path}?t=forged').status_code == 403


def test_search_finds_the_moment_and_folds_arabic(lesson, fake):
    seal(Lesson, lesson.pk, 'l/a.mp4')
    hits = transcribe.search('الاحتمالات')  # typed without the hamza
    assert hits and hits[0]['start'] == 0.0 and hits[0]['video_id'] == str(lesson.pk)
    assert transcribe.search('LENS formula')[0]['start'] == 2.5
    assert transcribe.search('nothing like this') == []
    assert transcribe.search('lens', videos=[]) == []
    assert transcribe.search('lens', videos=[lesson])[0]['video_type'] == 'testapp.Lesson'


def test_the_command_transcribes_videos_sealed_before(lesson, fake, settings, tmp_path):
    settings.VIDLOCK = {**settings.VIDLOCK, 'TRANSCRIBER': None}
    seal(Lesson, lesson.pk, 'l/a.mp4')
    lesson.refresh_from_db()
    assert not Transcript.objects.exists()
    body, _ = storage.OBJECTS[lesson.video_key]  # MemoryStorage keeps uploads as bytes
    sealed = tmp_path / 'sealed.ts'
    sealed.write_bytes(body)
    storage.OBJECTS[lesson.video_key] = str(sealed)
    settings.VIDLOCK = {**settings.VIDLOCK, 'TRANSCRIBER': 'tests.test_transcribe.FakeTranscriber'}
    out = io.StringIO()
    call_command('vidlock_transcribe', stdout=out)
    assert '1 transcribed' in out.getvalue() and Transcript.objects.count() == 1
    assert '0 transcribed' in call_command_output('vidlock_transcribe')


def call_command_output(*args):
    out = io.StringIO()
    call_command(*args, stdout=out)
    return out.getvalue()


def test_normalize():
    assert transcribe.normalize('الإحْتِمَالاتُ') == transcribe.normalize('الاحتمالات')
    assert transcribe.normalize('مدرسة') == transcribe.normalize('مدرسه')
    assert transcribe.normalize('  Café  LENS ') == 'cafe lens'


@pytest.mark.skipif(
    not shutil.which('espeak-ng') or __import__('importlib').util.find_spec('faster_whisper') is None,
    reason='needs espeak-ng and faster-whisper',
)
def test_real_speech_with_whisper(settings, tmp_path):
    """A lesson with real (synthetic) speech, transcribed by Whisper tiny."""
    speech = tmp_path / 'speech.wav'
    subprocess.run(
        [
            'espeak-ng',
            '-v',
            'en',
            '-s',
            '140',
            '-w',
            str(speech),
            'Today we will learn about the lens formula.',
        ],
        check=True,
    )
    video = tmp_path / 'talk.mp4'
    subprocess.run(
        [
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'testsrc=duration=6:size=160x120:rate=25', '-i', str(speech),
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(video),
        ],
        check=True,
    )  # fmt: skip
    storage.OBJECTS['l/talk.mp4'] = str(video)
    lesson = Lesson.objects.create(title='talk', video_key='l/talk.mp4')
    settings.VIDLOCK = {
        **settings.VIDLOCK,
        'TRANSCRIBER': 'vidlock.transcribe.FasterWhisper',
        'TRANSCRIBE_MODEL': 'tiny',
        'TRANSCRIBE_LANGUAGE': 'en',
    }
    assert seal(Lesson, lesson.pk, 'l/talk.mp4') == 'sealed'
    hits = transcribe.search('lens')
    assert hits and hits[0]['start'] < 3, Transcript.objects.get().segments
