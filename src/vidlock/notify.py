"""Sending insights and reminders: pick one with ``VIDLOCK['NOTIFIER']``.

A notifier is ``fn(users, subject, message)``. Three come with vidlock:

``vidlock.notify.email``
    Django's ``send_mail`` to each user's email.
``vidlock.notify.webhook``
    POSTs ``{"subject", "message", "recipients"}`` as JSON to
    ``VIDLOCK['NOTIFY_WEBHOOK_URL']`` — Slack, Discord, a Telegram bot, n8n,
    Zapier, your own endpoint.
``vidlock.notify.whatsapp``
    The WhatsApp Cloud API (Meta). Set ``WHATSAPP_TOKEN`` and
    ``WHATSAPP_PHONE_NUMBER_ID``; phone numbers come from
    ``SealedBackend.phone(user)``. WhatsApp only delivers free text inside a
    24-hour conversation window: for daily messages, create a template with
    one body variable in WhatsApp Manager and set ``WHATSAPP_TEMPLATE`` (and
    ``WHATSAPP_TEMPLATE_LANGUAGE``, e.g. ``'ar'``).
"""

from __future__ import annotations

import json
import logging
import urllib.request

from django.conf import settings
from django.core.mail import send_mail

from vidlock import conf

logger = logging.getLogger(__name__)


def send(users, subject: str, message: str) -> int:
    """Through the configured notifier; returns how many were sent."""
    notifier = conf.load('NOTIFIER')
    if notifier is None:
        return 0
    users = [u for u in users if u is not None]
    if not users:
        return 0
    return notifier(users, subject, message) or 0


def email(users, subject, message):
    addresses = [u.email for u in users if getattr(u, 'email', '')]
    if not addresses:
        return 0
    sent = 0
    for address in addresses:
        sent += send_mail(subject, message, getattr(settings, 'DEFAULT_FROM_EMAIL', None), [address])
    return sent


def _post(url, payload, headers=None):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json', **(headers or {})},
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=20) as answer:
        return answer.status


def webhook(users, subject, message):
    url = conf.get('NOTIFY_WEBHOOK_URL')
    if not url:
        logger.warning("vidlock.notify.webhook: VIDLOCK['NOTIFY_WEBHOOK_URL'] is not set")
        return 0
    status = _post(
        url,
        {
            'subject': subject,
            'message': message,
            'text': f'{subject}\n{message}',
            'recipients': [u.get_username() for u in users],
        },
    )
    return len(users) if 200 <= status < 300 else 0


def whatsapp(users, subject, message):
    token, phone_id = conf.get('WHATSAPP_TOKEN'), conf.get('WHATSAPP_PHONE_NUMBER_ID')
    if not token or not phone_id:
        logger.warning('vidlock.notify.whatsapp: WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID are required')
        return 0
    backend_cls = conf.load('BACKEND')
    backend = backend_cls() if backend_cls else None
    url = f'https://graph.facebook.com/{conf.get("WHATSAPP_API_VERSION")}/{phone_id}/messages'
    template = conf.get('WHATSAPP_TEMPLATE')
    text = f'{subject}\n{message}'
    sent = 0
    for user in users:
        phone = backend.phone(user) if backend is not None else None
        if not phone:
            continue
        payload = {'messaging_product': 'whatsapp', 'to': str(phone).lstrip('+')}
        if template:
            payload.update(
                type='template',
                template={
                    'name': template,
                    'language': {'code': conf.get('WHATSAPP_TEMPLATE_LANGUAGE')},
                    'components': [{'type': 'body', 'parameters': [{'type': 'text', 'text': text[:1000]}]}],
                },
            )
        else:
            payload.update(type='text', text={'body': text[:4000]})
        try:
            if 200 <= _post(url, payload, {'Authorization': f'Bearer {token}'}) < 300:
                sent += 1
        except Exception:
            logger.exception('vidlock.notify.whatsapp: sending to user %s failed', user.pk)
    return sent
