"""Just enough of a site for the browser test: a page, a playback endpoint,
and a fake bucket that answers Range requests with CORS."""

import re

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render

from tests.testapp import storage
from tests.testapp.models import Lesson
from vidlock.views import playback_info


@login_required
def page(request, pk):
    return render(request, 'player.html', {'pk': pk})


@login_required
def playback(request, pk):
    return JsonResponse(playback_info(request, get_object_or_404(Lesson, pk=pk)))


def bucket(request, key):
    if request.method == 'OPTIONS':
        response = HttpResponse()
    else:
        found = storage.OBJECTS.get(key)
        if found is None:
            raise Http404
        body = found[0]
        match = re.match(r'bytes=(\d+)-(\d*)', request.headers.get('Range', ''))
        if match:
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else len(body) - 1
            response = HttpResponse(body[start : end + 1], status=206, content_type='video/mp2t')
            response['Content-Range'] = f'bytes {start}-{end}/{len(body)}'
        else:
            response = HttpResponse(body, content_type='video/mp2t')
    response['Access-Control-Allow-Origin'] = '*'
    response['Access-Control-Allow-Headers'] = 'range'
    response['Access-Control-Expose-Headers'] = 'content-length, content-range'
    return response
