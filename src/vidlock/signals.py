"""Signals your project can listen to.

``seal_finished`` — sent by ``vidlock.pipeline.seal`` whenever a job ends,
whatever the outcome. ``sender`` is your model class; keyword arguments:
``pk``, ``state`` ('sealed', 'skipped', 'failed' or 'stale'), ``error``
(a short message, '' on success) and ``video_key`` (the key now in the row:
the .ts once sealed, else the source).

``key_abuse`` — sent the first time in a day that a viewer crosses a key
limit. ``sender`` is the backend class; keyword arguments: ``request``,
``user``, ``video`` and ``reason`` (``vidlock.guard.DEPTH`` or ``BREADTH``).

    from django.dispatch import receiver
    from vidlock.signals import key_abuse

    @receiver(key_abuse)
    def alert(sender, request, user, video, reason, **kwargs):
        notify_admins(f'{user} tripped the {reason} limit on {video}')
"""

from django.dispatch import Signal

seal_finished = Signal()
key_abuse = Signal()
