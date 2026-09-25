"""The two questions only your project can answer.

Subclass, then point ``VIDLOCK['BACKEND']`` at it:

    class LessonBackend(SealedBackend):
        def get_video(self, video_id):
            return Lesson.objects.filter(pk=video_id).first()

        def can_watch(self, user, video):
            return video.course.students.filter(pk=user.pk).exists()

``can_watch`` runs on every playlist and key request, not only when the page
loads: a refund or a revoked enrolment inside a token's few minutes should
close the door at once, not when the token runs out.

``watermark`` is optional: return a short text (a name, a phone number, an
order id) and the bundled player draws it over the video, moving, so a screen
recording names who made it.
"""

from __future__ import annotations


class SealedBackend:
    def get_video(self, video_id):
        """The model instance for this id, or None."""
        raise NotImplementedError

    def can_watch(self, user, video):
        """Whether ``user`` may watch ``video`` right now."""
        raise NotImplementedError

    def namespace(self, request) -> str:
        """Keeps fetch counters apart when one cache serves several sites or
        tenants whose ids may collide. Return e.g. the tenant's schema name."""
        return ''

    def watermark(self, user, video) -> str | None:
        """Text drawn over the video for this viewer, or None for none."""
        return None

    def client_ip(self, request) -> str:
        """The viewer's address, for the risk score's network count. Behind a
        proxy or CDN, return the header it sets (e.g. CF-Connecting-IP) —
        only if that proxy is the only way in."""
        return request.META.get('REMOTE_ADDR', '')
