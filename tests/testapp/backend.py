from vidlock.backend import SealedBackend

from tests.testapp.models import Lesson

REPORTS = []


class LessonBackend(SealedBackend):
    def get_video(self, video_id):
        return Lesson.objects.filter(pk=video_id).first()

    def can_watch(self, user, video):
        return video.students.filter(pk=user.pk).exists()


def report(request, user, video):
    REPORTS.append((user.pk, video.pk))
