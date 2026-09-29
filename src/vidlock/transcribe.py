"""Transcripts: what is said in each lesson, when — for captions and search.

Set ``VIDLOCK['TRANSCRIBER']`` and every video sealed from then on is also
transcribed, on the same worker, right after it goes live (a failure never
fails the seal). ``manage.py vidlock_transcribe`` does videos sealed before.

    VIDLOCK = {
        ...,
        'TRANSCRIBER': 'vidlock.transcribe.FasterWhisper',   # pip install django-vidlock[transcribe]
        'TRANSCRIBE_MODEL': 'small',        # tiny, base, small, medium, large-v3: bigger is better and slower
        'TRANSCRIBE_LANGUAGE': 'ar',        # or None to detect it
    }

Any class with ``transcribe(audio_path, language) -> (language, segments)``,
where segments are ``(start, end, text)`` tuples, can be plugged in instead —
a cloud speech API, say.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import unicodedata
from typing import ClassVar

from django.db import transaction

from vidlock import conf, packager, signals

logger = logging.getLogger(__name__)


class FasterWhisper:
    """OpenAI's Whisper through faster-whisper, on this machine's CPU (or GPU).
    Arabic, English and ~100 other languages. The model downloads once."""

    #: Loaded models, shared by every instance in this process.
    _models: ClassVar[dict] = {}

    def __init__(self):
        from faster_whisper import WhisperModel  # noqa: F401 — fail early if missing

        self.model_name = conf.get('TRANSCRIBE_MODEL')

    def _model(self):
        from faster_whisper import WhisperModel

        if self.model_name not in self._models:
            self._models[self.model_name] = WhisperModel(self.model_name, device='auto', compute_type='int8')
        return self._models[self.model_name]

    def transcribe(self, audio_path: str, language: str | None):
        segments, info = self._model().transcribe(audio_path, language=language, vad_filter=True)
        return info.language, [(s.start, s.end, s.text.strip()) for s in segments if s.text.strip()]


def extract_audio(source: str, out_path: str) -> str:
    """16 kHz mono WAV of ``source``'s audio: what speech models want."""
    run = subprocess.run(
        [
            packager.ffmpeg_binary() or 'ffmpeg',
            '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            *packager.SAFE_INPUT, '-i', source,
            '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', out_path,
        ],
        check=False, capture_output=True, text=True, timeout=packager.TIMEOUT_SECONDS,
    )  # fmt: skip
    if run.returncode:
        raise packager.PackagingError(f'ffmpeg could not extract audio: {run.stderr.strip()[-200:]}')
    return out_path


def transcriber():
    cls = conf.load('TRANSCRIBER')
    return cls() if cls else None


def transcribe_audio(video, audio_path: str, engine=None):
    """Transcribe ``audio_path`` for ``video`` and store it; returns the
    ``Transcript`` (or None when no transcriber is configured)."""
    from vidlock.models import Transcript

    engine = engine or transcriber()
    if engine is None:
        return None
    language, segments = engine.transcribe(audio_path, conf.get('TRANSCRIBE_LANGUAGE'))
    rows = [{'start': round(float(a), 2), 'end': round(float(b), 2), 'text': str(t)} for a, b, t in segments]
    with transaction.atomic():
        found, _ = Transcript.objects.update_or_create(
            video_type=video._meta.label,
            video_id=str(video.pk),
            defaults={
                'language': language or '',
                'segments': rows,
                'text': ' '.join(r['text'] for r in rows),
                'search_text': normalize(' '.join(r['text'] for r in rows)),
            },
        )
    signals.transcript_ready.send(sender=type(video), video=video, transcript=found)
    return found


def after_seal(model, pk, source_audio: str) -> None:
    """Called by the pipeline once a video is live. Never raises."""
    try:
        video = model._default_manager.get(pk=pk)
        transcribe_audio(video, source_audio)
    except Exception:
        logger.exception('transcribing %s %s failed', model.__name__, pk)
    finally:
        if os.path.exists(source_audio):
            os.remove(source_audio)


# -- captions ----------------------------------------------------------------


def _stamp(seconds: float) -> str:
    ms = round(seconds * 1000)
    hours, ms = divmod(ms, 3600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    return f'{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}'


def webvtt(transcript) -> str:
    """The transcript as WebVTT, for a ``<track>``."""
    lines = ['WEBVTT', '']
    for index, row in enumerate(transcript.segments, 1):
        text = row['text'].replace('-->', '→')
        lines += [str(index), f'{_stamp(row["start"])} --> {_stamp(row["end"])}', text, '']
    return '\n'.join(lines)


# -- search --------------------------------------------------------------------

_ARABIC_MARKS = re.compile('[ؐ-ًؚ-ٰٟۖ-ۭـ]')
_FOLD = str.maketrans({'أ': 'ا', 'إ': 'ا', 'آ': 'ا', 'ٱ': 'ا', 'ى': 'ي', 'ة': 'ه', 'ؤ': 'و', 'ئ': 'ي'})


def normalize(text: str) -> str:
    """Fold text for search: case, accents, Arabic diacritics and tatweel,
    alef/ya/ta-marbuta variants — so «الإحتمالات» finds «الاحتمالات»."""
    text = unicodedata.normalize('NFKC', text).casefold()
    text = _ARABIC_MARKS.sub('', text).translate(_FOLD)
    text = ''.join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')
    return re.sub(r'\s+', ' ', text).strip()


def search(query: str, videos=None, limit: int = 20) -> list[dict]:
    """Moments where ``query`` is said: ``[{'video_type', 'video_id',
    'start', 'text'}]``, best first. ``videos`` (a queryset or list) limits
    it to those, e.g. the courses a student is enrolled in."""
    from vidlock.models import Transcript

    needle = normalize(query)
    if not needle:
        return []
    rows = Transcript.objects.filter(search_text__contains=needle)
    if videos is not None:
        ids = {(v._meta.label, str(v.pk)) for v in videos}
        rows = [r for r in rows if (r.video_type, r.video_id) in ids]
    found = []
    for row in rows:
        for segment in row.segments:
            if needle in normalize(segment['text']):
                found.append(
                    {
                        'video_type': row.video_type,
                        'video_id': row.video_id,
                        'start': segment['start'],
                        'text': segment['text'],
                    }
                )
                if len(found) >= limit:
                    return found
    return found
