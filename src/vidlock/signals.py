"""Signals your project can listen to.

``seal_finished`` — sent by ``vidlock.pipeline.seal`` whenever a job ends,
whatever the outcome. ``sender`` is your model class; keyword arguments:
``pk``, ``state`` ('sealed', 'skipped', 'failed' or 'stale'), ``error``
(a short message, '' on success) and ``video_key`` (the key now in the row:
the .ts once sealed, else the source).

``key_abuse`` — sent the first time in a day that a viewer crosses a key
limit. ``sender`` is the backend class; keyword arguments: ``request``,
``user``, ``video`` and ``reason`` (``vidlock.guard.DEPTH``, ``BREADTH`` or
``PACE``).

``viewer_flagged`` — sent the first time in a day that a viewer's suspicion
score (``vidlock.risk``) reaches ``RISK_THRESHOLD``. Keyword arguments:
``user``, ``score``, ``events`` (event name -> count), ``suspended_for``
(seconds; 0 when only flagged) and ``request``.

    from django.dispatch import receiver
    from vidlock.signals import viewer_flagged

    @receiver(viewer_flagged)
    def alert(sender, user, score, events, **kwargs):
        notify_admins(f'{user} scored {score}: {events}')
"""

from django.dispatch import Signal

seal_finished = Signal()
key_abuse = Signal()
viewer_flagged = Signal()
