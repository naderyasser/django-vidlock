from django.contrib import admin

from tests.testapp.models import Lesson
from vidlock.admin import SealedVideoAdminMixin


@admin.register(Lesson)
class LessonAdmin(SealedVideoAdminMixin, admin.ModelAdmin):
    list_display = ('title', 'seal_status')
    list_filter = ('sealed_state',)
