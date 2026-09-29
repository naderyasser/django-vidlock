import os
import uuid

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from lessons.backend import LessonBackend
from lessons.models import Lesson
from vidlock import insights, progress, transcribe
from vidlock.models import Transcript, WatchProgress
from vidlock.pipeline import seal_later
from vidlock.storage import default_storage
from vidlock.views import playback_info


class UploadForm(forms.Form):
    title = forms.CharField(max_length=200)
    video = forms.FileField(help_text='An MP4 or MOV in H.264 — most phones and screen recorders.')


@login_required
def lesson_list(request):
    form = UploadForm()
    if request.method == 'POST' and request.user.is_staff:
        form = UploadForm(request.POST, request.FILES)
        if form.is_valid():
            upload = form.cleaned_data['video']
            extension = os.path.splitext(upload.name)[1].lower() or '.mp4'
            key = default_storage().save_upload(upload, f'uploads/{uuid.uuid4().hex}{extension}')
            lesson = Lesson.objects.create(title=form.cleaned_data['title'])
            # Seals inline after the commit here; a real site sets
            # VIDLOCK['ENQUEUE_SEAL'] to hand it to Celery or Django Tasks.
            if seal_later(lesson, key):
                messages.success(request, f'"{lesson.title}" uploaded and sealed.')
            else:
                messages.warning(
                    request, f'"{lesson.title}" uploaded, but it is not a video vidlock can seal.'
                )
            return redirect('lessons')
    return render(request, 'lessons/list.html', {'lessons': Lesson.objects.all(), 'form': form})


@login_required
def watch(request, pk):
    lesson = get_object_or_404(Lesson, pk=pk)
    query = request.GET.get('q', '').strip()
    hits = transcribe.search(query, videos=[lesson]) if query else []
    has_transcript = Transcript.objects.filter(video_type='lessons.Lesson', video_id=str(lesson.pk)).exists()
    return render(
        request,
        'lessons/watch.html',
        {'lesson': lesson, 'query': query, 'hits': hits, 'has_transcript': has_transcript},
    )


@login_required
def search(request):
    query = request.GET.get('q', '').strip()
    hits = transcribe.search(query) if query else []
    titles = {str(lesson.pk): lesson.title for lesson in Lesson.objects.all()}
    for hit in hits:
        hit['title'] = titles.get(hit['video_id'], '?')
    return render(request, 'lessons/search.html', {'query': query, 'hits': hits})


@login_required
def playback(request, pk):
    lesson = get_object_or_404(Lesson, pk=pk)
    if not LessonBackend().can_watch(request.user, lesson):
        return HttpResponseForbidden()
    return JsonResponse(playback_info(request, lesson))


@login_required
def report(request, pk):
    lesson = get_object_or_404(Lesson, pk=pk)
    data = progress.report(lesson)
    peak = max(data['heatmap'] or [0]) or 1
    bars = [
        {
            'height': round(n / peak * 100),
            'count': n,
            'at': f'{i * data["bucket_seconds"] // 60}:{i * data["bucket_seconds"] % 60:02d}',
        }
        for i, n in enumerate(data['heatmap'])
    ]
    rows = WatchProgress.objects.select_related('user').filter(
        video_type='lessons.Lesson', video_id=str(lesson.pk)
    )
    found = insights.for_video(lesson)
    return render(
        request,
        'lessons/report.html',
        {'lesson': lesson, 'data': data, 'bars': bars, 'rows': rows, 'insights': found},
    )
