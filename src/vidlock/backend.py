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
"""


class SealedBackend:
    def get_video(self, video_id):
        """The model instance for this id, or None."""
        raise NotImplementedError

    def can_watch(self, user, video):
        """Whether ``user`` may watch ``video`` right now."""
        raise NotImplementedError

    def namespace(self, request):
        """Keeps fetch counters apart when one cache serves several sites or
        tenants whose ids may collide. Return e.g. the tenant's schema name."""
        return ''
