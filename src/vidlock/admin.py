"""Admin helpers for your own ModelAdmin, and a read-only page for
``WatchProgress`` (who watched what).

    from django.contrib import admin
    from vidlock.admin import SealedVideoAdminMixin

    @admin.register(Lesson)
    class LessonAdmin(SealedVideoAdminMixin, admin.ModelAdmin):
        list_display = ('title', 'seal_status')
        list_filter = ('sealed_state',)

The "Seal again" action queues through ``VIDLOCK['ENQUEUE_SEAL']`` when it is
set, and otherwise seals inline (fine for a few short videos, slow for many).
"Re-encode and seal" (with ``TRANSCODE`` on) does the same through
``ENQUEUE_TRANSCODE``; the Protection column shows a re-encode's progress.
"""

from django.contrib import admin, messages
from django.db.models import CharField, OuterRef, Subquery
from django.db.models.functions import Cast
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from vidlock import conf, pipeline, transcode
from vidlock.models import Transcode, WatchProgress

_READONLY = ('video_size', 'sealed_state', 'sealed_error', 'sealed_duration_display', 'transcode_display')
#: Annotated onto the changelist, so the Protection column costs no query per row.
_TRANSCODE_FIELDS = ('state', 'progress', 'encoder')


def _transcode_of(obj):
    """(state, progress, encoder) of the video's re-encode, or None."""
    if hasattr(obj, '_vidlock_transcode_state'):
        state = obj._vidlock_transcode_state
        if state is None:
            return None
        return state, obj._vidlock_transcode_progress or 0, obj._vidlock_transcode_encoder or ''
    found = Transcode.for_video(obj)
    return (found.state, found.progress, found.encoder) if found else None


class SealedVideoAdminMixin:
    actions = ('seal_again', 'transcode_again')

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        rows = Transcode.objects.filter(
            video_type=queryset.model._meta.label, video_id=Cast(OuterRef('pk'), CharField())
        )
        return queryset.annotate(
            **{f'_vidlock_transcode_{name}': Subquery(rows.values(name)[:1]) for name in _TRANSCODE_FIELDS}
        )

    def get_readonly_fields(self, request, obj=None):
        return (*super().get_readonly_fields(request, obj), *_READONLY)

    def get_exclude(self, request, obj=None):
        # The playlist is generated; editing it by hand can only break playback.
        return (*(super().get_exclude(request, obj) or ()), 'sealed_playlist')

    @admin.display(description=_('Protection'), ordering='sealed_state')
    def seal_status(self, obj):
        if obj.is_sealed:
            return _('Sealed')
        found = _transcode_of(obj) if obj.sealed_state == obj.STATE_PENDING else None
        if found and found[0] == Transcode.STATE_QUEUED:
            return _('Waiting to re-encode')
        if found and found[0] == Transcode.STATE_RUNNING:
            return _('Re-encoding %(percent)d%%') % {'percent': int(found[1] * 100)} + (
                f' · {found[2]}' if found[2] else ''
            )
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

    @admin.display(description=_('Re-encode'))
    def transcode_display(self, obj):
        found = Transcode.for_video(obj) if obj and obj.pk else None
        if found is None:
            return '—'
        parts = [str(_STATES.get(found.state, found.state or '—'))]
        if found.state == Transcode.STATE_RUNNING:
            parts.append(f'{found.progress:.0%}')
        if found.encoder:
            parts.append(found.encoder)
        if found.source_info:
            parts.append(_('from %(source)s') % {'source': found.source_info})
        if found.started and found.finished:
            minutes = round((found.finished - found.started).total_seconds() / 60, 1)
            parts.append(_('%(minutes)s min') % {'minutes': minutes})
        if found.error:
            parts.append(found.error)
        return ' · '.join(str(p) for p in parts)

    @admin.action(description=_('Re-encode and seal (skipped or failed videos)'))
    def transcode_again(self, request, queryset):
        if not transcode.enabled():
            self.message_user(
                request, _("Turn on VIDLOCK['TRANSCODE'] to re-encode videos."), messages.WARNING
            )
            return
        model = queryset.model
        started = 0
        for obj in queryset:
            if obj.is_sealed or not pipeline.wants(obj.video_key):
                continue
            if obj.sealed_state not in (model.STATE_SKIPPED, model.STATE_FAILED, model.STATE_NONE):
                continue  # pending: already on its way
            if not transcode.queue(model, obj.pk, obj.video_key):
                model._default_manager.filter(pk=obj.pk, video_key=obj.video_key).update(
                    sealed_state=model.STATE_PENDING, sealed_error=''
                )
                transcode.run(model, obj.pk, obj.video_key)
            started += 1
        if started:
            self.message_user(
                request,
                ngettext('%d video sent for re-encoding.', '%d videos sent for re-encoding.', started)
                % started,
                messages.SUCCESS,
            )
        else:
            self.message_user(request, _('Nothing to re-encode among the selected videos.'), messages.WARNING)

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


_STATES = {
    Transcode.STATE_QUEUED: _('Waiting'),
    Transcode.STATE_RUNNING: _('Running'),
    Transcode.STATE_DONE: _('Done'),
    Transcode.STATE_FAILED: _('Failed'),
}


@admin.register(WatchProgress)
class WatchProgressAdmin(admin.ModelAdmin):
    list_display = ('user', 'video_type', 'video_id', 'completion_display', 'minutes', 'last_seen')
    list_filter = ('video_type',)
    search_fields = ('video_id',)
    date_hierarchy = 'last_seen'
    readonly_fields = tuple(f.name for f in WatchProgress._meta.fields)
    list_select_related = ('user',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description=_('Watched'))
    def completion_display(self, obj):
        return f'{obj.completion:.0%}'

    @admin.display(description=_('Minutes'), ordering='seconds_watched')
    def minutes(self, obj):
        return round(obj.seconds_watched / 60)
