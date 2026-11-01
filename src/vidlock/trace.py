"""Short codes in the watermark that lead back to a viewer.

A recording posted to a Telegram channel often crops the name or blurs it.
The code beside it is six letters, different for every viewer and day, and
means nothing to anyone without your SECRET_KEY:

    manage.py vidlock_trace K7QMZ4              # searches the last 60 days
    manage.py vidlock_trace K7QMZ4 --day 2026-09-25

**Codes from before the SHA-256 pin.** The first 0.3.0 build computed
codes with ``salted_hmac``'s default algorithm, SHA-1 up to Django 6.x;
every later version pins SHA-256. Recordings made under that build show
SHA-1 codes, so ``find`` tries those too (``legacy=False`` turns it off)
and says which kind matched.
"""

from __future__ import annotations

import base64
import datetime

from django.conf import settings
from django.utils import timezone
from django.utils.crypto import salted_hmac

#: The algorithm codes are drawn with, and the one the first 0.3.0 build used.
CURRENT, LEGACY = 'sha256', 'sha1'


def _code(user_id, day: datetime.date, secret: str | None = None, algorithm: str = CURRENT) -> str:
    # sha256 pinned: Django 7 changes the default, which would change every code.
    digest = salted_hmac(
        'vidlock.trace', f'{user_id}:{day.isoformat()}', secret=secret, algorithm=algorithm
    ).digest()
    return base64.b32encode(digest).decode()[:6]


def code_for(user, day: datetime.date | None = None) -> str:
    """The watermark code for ``user`` on ``day`` (default: today)."""
    return _code(getattr(user, 'pk', user), day or timezone.localdate())


def matches(code: str, days: list[datetime.date], legacy: bool = True) -> list[dict]:
    """Every viewer and day whose code is ``code``, as ``{'user_id', 'day',
    'legacy'}``; ``legacy`` is True for a SHA-1 code from the first 0.3.0
    build. Tries SECRET_KEY and SECRET_KEY_FALLBACKS, so a rotated secret
    still traces old recordings."""
    from django.contrib.auth import get_user_model

    code = code.strip().upper()
    secrets_ = [settings.SECRET_KEY, *getattr(settings, 'SECRET_KEY_FALLBACKS', [])]
    algorithms = (CURRENT, LEGACY) if legacy else (CURRENT,)
    found = []
    for user_id in get_user_model()._default_manager.values_list('pk', flat=True).iterator():
        for day in days:
            for algorithm in algorithms:
                if any(_code(user_id, day, secret, algorithm) == code for secret in secrets_):
                    found.append({'user_id': user_id, 'day': day, 'legacy': algorithm == LEGACY})
                    break
    return found


def find(code: str, days: list[datetime.date], legacy: bool = True) -> list[tuple[object, datetime.date]]:
    """Every (user id, day) whose code is ``code``, current or legacy
    (see ``matches``)."""
    return [(m['user_id'], m['day']) for m in matches(code, days, legacy)]
