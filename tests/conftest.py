import os
import shutil
import subprocess

import pytest

from tests.testapp import backend, storage

HAS_FFMPEG = bool(shutil.which(os.environ.get('VIDLOCK_TEST_FFMPEG', 'ffmpeg')))


@pytest.fixture(autouse=True)
def _clean(settings):
    from django.core.cache import cache

    storage.OBJECTS.clear()
    storage.DELETED.clear()
    backend.REPORTS.clear()
    cache.clear()
    settings.VIDLOCK = {**settings.VIDLOCK, 'FFMPEG_BINARY': os.environ.get('VIDLOCK_TEST_FFMPEG', 'ffmpeg')}


@pytest.fixture
def sample(tmp_path):
    """A 25-second H.264/AAC MP4, made on the spot."""
    if not HAS_FFMPEG:
        pytest.skip('ffmpeg is not installed')
    path = tmp_path / 'sample.mp4'
    subprocess.run(
        [
            os.environ.get('VIDLOCK_TEST_FFMPEG', 'ffmpeg'),
            '-hide_banner',
            '-loglevel',
            'error',
            '-f',
            'lavfi',
            '-i',
            'testsrc=duration=25:size=160x120:rate=25',
            '-f',
            'lavfi',
            '-i',
            'sine=duration=25',
            '-c:v',
            'libx264',
            '-c:a',
            'aac',
            '-shortest',
            str(path),
        ],
        check=True,
    )
    return str(path)
