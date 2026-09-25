"""Admin helpers for your own ModelAdmin. vidlock registers no model itself.

    from django.contrib import admin
    from vidlock.admin import SealedVideoAdminMixin

    @admin.register(Lesson)
    class LessonAdmin(SealedVideoAdminMixin, admin.ModelAdmin):
        list_display = ('title', 'seal_status')
        list_filter = ('sealed_state',)

The "Seal again" action queues through ``VIDLOCK['ENQUEUE_SEAL']`` when it is
set, and otherwise seals inline (fine for a few short videos, slow for many).
"""

from django.contrib import admin, messages
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from vidlock import conf, pipeline

_READONLY = ('video_size', 'sealed_state', 'sealed_error', 'sealed_duration_display')


class SealedVideoAdminMixin:
    actions = ('seal_again',)

    def get_readonly_fields(self, request, obj=None):
        return (*super().get_readonly_fields(request, obj), *_READONLY)

    def get_exclude(self, request, obj=None):
        # The playlist is generated; editing it by hand can only break playback.
        return (*(super().get_exclude(request, obj) or ()), 'sealed_playlist')

    @admin.display(description=_('Protection'), ordering='sealed_state')
    def seal_status(self, obj):
        if obj.is_sealed:
            return _('Sealed')
        return {
            obj.STATE_PENDING: _('Sealing…'),
            obj.STATE_FAILED: _('Failed'),
            obj.STATE_SKIPPED: _('Not sealable'),
        }.get(obj.sealed_state, _('Not sealed'))

    @admin.display(description=_('Duration'))
    def sealed_duration_display(self, obj):
        seconds = int(obj.sealed_duration)
        if not seconds:
            return '—'
        hours, rest = divmod(seconds, 3600)
        return f'{hours}:{rest // 60:02d}:{rest % 60:02d}' if hours else f'{rest // 60}:{rest % 60:02d}'

    @admin.action(description=_('Seal again (unsealed or failed videos)'))
    def seal_again(self, request, queryset):
        enqueue = conf.load('ENQUEUE_SEAL')
        model = queryset.model
        started = 0
        for obj in queryset:
            if obj.is_sealed or not pipeline.wants(obj.video_key):
                continue
            model._default_manager.filter(pk=obj.pk, video_key=obj.video_key).update(
                sealed_state=model.STATE_PENDING, sealed_error=''
            )
            if enqueue:
                enqueue(model, obj.pk, obj.video_key)
            else:
                pipeline.seal(model, obj.pk, obj.video_key)
            started += 1
        if started:
            self.message_user(
                request,
                ngettext('%d video sent for sealing.', '%d videos sent for sealing.', started) % started,
                messages.SUCCESS,
            )
        else:
            self.message_user(request, _('Nothing to seal among the selected videos.'), messages.WARNING)
