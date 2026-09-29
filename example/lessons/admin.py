from django.contrib import admin

from lessons.models import Lesson
from vidlock.admin import SealedVideoAdminMixin


@admin.register(Lesson)
class LessonAdmin(SealedVideoAdminMixin, admin.ModelAdmin):
    list_display = ('title', 'seal_status', 'created')
    list_filter = ('sealed_state',)
