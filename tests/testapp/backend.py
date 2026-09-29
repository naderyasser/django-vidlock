from tests.testapp.models import Lesson
from vidlock.backend import SealedBackend

REPORTS = []
#: The browser test turns the watermark on.
WATERMARK = []


class LessonBackend(SealedBackend):
    def get_video(self, video_id):
        return Lesson.objects.filter(pk=video_id).first()

    def can_watch(self, user, video):
        return video.students.filter(pk=user.pk).exists()

    def audience(self, video):
        return list(video.students.all())

    def teachers(self, video):
        from django.contrib.auth import get_user_model

        return list(get_user_model().objects.filter(is_staff=True))

    def phone(self, user):
        return getattr(user, 'first_name', '') or None

    def watermark(self, user, video):
        return user.get_username() if WATERMARK else None


def report(request, user, video):
    REPORTS.append((user.pk, video.pk))
