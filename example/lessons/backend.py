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
