"""Short codes in the watermark that lead back to a viewer.

A recording posted to a Telegram channel often crops the name or blurs it.
The code beside it is six letters, different for every viewer and day, and
means nothing to anyone without your SECRET_KEY:

    manage.py vidlock_trace K7QMZ4              # searches the last 60 days
    manage.py vidlock_trace K7QMZ4 --day 2026-09-25
"""

from __future__ import annotations

import base64
import datetime

from django.conf import settings
from django.utils import timezone
from django.utils.crypto import salted_hmac


def _code(user_id, day: datetime.date, secret: str | None = None) -> str:
    # sha256 pinned: Django 7 changes the default, which would change every code.
    digest = salted_hmac(
        'vidlock.trace', f'{user_id}:{day.isoformat()}', secret=secret, algorithm='sha256'
    ).digest()
    return base64.b32encode(digest).decode()[:6]


def code_for(user, day: datetime.date | None = None) -> str:
    """The watermark code for ``user`` on ``day`` (default: today)."""
    return _code(getattr(user, 'pk', user), day or timezone.localdate())


def find(code: str, days: list[datetime.date]) -> list[tuple[object, datetime.date]]:
    """Every (user id, day) whose code is ``code``. Tries SECRET_KEY and
    SECRET_KEY_FALLBACKS, so a rotated secret still traces old recordings."""
    from django.contrib.auth import get_user_model

    code = code.strip().upper()
    secrets_ = [settings.SECRET_KEY, *getattr(settings, 'SECRET_KEY_FALLBACKS', [])]
    found = []
    for user_id in get_user_model()._default_manager.values_list('pk', flat=True).iterator():
        for day in days:
            if any(_code(user_id, day, secret) == code for secret in secrets_):
                found.append((user_id, day))
    return found
