from lessons.models import Lesson
from vidlock.backend import SealedBackend


class LessonBackend(SealedBackend):
    def get_video(self, video_id):
        return Lesson.objects.filter(pk=video_id).first()

    def can_watch(self, user, video):
        # A real site checks the enrolment here; the demo lets every account in.
        return user.is_authenticated and user.is_active

    def watermark(self, user, video):
        return user.get_username()

    def audience(self, video):
        # A real site returns the course's enrolments; the demo: every student.
        from django.contrib.auth import get_user_model

        return list(get_user_model().objects.filter(is_staff=False, is_active=True))

    def teachers(self, video):
        from django.contrib.auth import get_user_model

        return list(get_user_model().objects.filter(is_staff=True))
