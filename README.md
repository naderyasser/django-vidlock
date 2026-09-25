# django-vidlock

**Lock your course videos.** Encrypted single-file HLS for Django: a video
downloaded from its link is worth nothing without a key that only an
authorised viewer receives, for a few minutes.

Built for paid-course platforms on a budget. No DRM licence, no video
service, no re-encoding: one `ffmpeg -c copy` per upload, one object in your
bucket, and the player you already know.

[العربي تحت ↓](#بالعربي)

---

## The problem

A signed MP4 URL stops casual link-sharing, but anyone with a download
manager catches the URL while it is valid and walks away with a clean,
playable copy. A watermark drawn over the player does not survive that.

## What vidlock does

1. **Seals every upload.** ffmpeg remuxes the MP4 (no re-encode — about a
   second for a 78 MB lesson, under 50 MB of RAM) into **one** AES-128
   encrypted MPEG-TS plus a playlist of byte ranges. The sealed file replaces
   the MP4 in your bucket; the 16-byte key and the playlist live in your
   database.
2. **Hands the key only to a viewer you approve.** The playlist and key URLs
   carry a token bound to one viewer, one video and ten minutes. Your
   `can_watch()` runs again on every playlist and key request, so a refund
   closes the door at once.
3. **Refuses the key to download tools.** A token issued to a browser session
   opens the key only alongside that viewer's session cookie — a URL pasted
   into yt-dlp or N_m3u8DL-RE arrives without it. And more than
   `KEY_FETCHES_PER_HOUR` key requests from one viewer for one video (a
   player needs about one per page load) are refused, with a hook so you hear
   about it.
4. **Plays everywhere.** The bundled script uses hls.js where MediaSource
   exists and native HLS on Safari/iOS; ExoPlayer and AVPlayer play the same
   URL in a mobile app. Signed URLs are renewed before they expire without
   reloading the stream.

### Why one file

`-hls_flags single_file` writes a single `.ts` and a playlist of
`#EXT-X-BYTERANGE`s into it, every range decryptable on its own. Compared
with the usual hundreds of segment files:

* one upload, one signed URL, one delete — your storage accounting and
  cleanup code see one key per video, exactly as they did for the MP4;
* no per-segment URL signing and no playlist full of object names.

### What it is not

**Not DRM.** Anyone holding a valid token and the matching session can, with
effort, recover the key — it has to reach the browser to play. vidlock
makes the file on disk useless and the easy tools fail; it does not stop a
determined engineer. If you need that, you need Widevine/FairPlay and their
licence fees. Pair vidlock with a visible per-viewer watermark for screen
recording.

## Requirements

* Python 3.10+, Django 4.2+
* `ffmpeg` on the worker that seals (any recent static build)
* An S3-compatible **private** bucket (Cloudflare R2, AWS S3, MinIO, B2) and
  `pip install django-vidlock[s3]`, or your own storage class
* Sources in H.264 (+ AAC/MP3). Anything else (HEVC from an iPhone, WebM) is
  left untouched and marked `skipped` — sealing never re-encodes.

## Quick start

```bash
pip install "django-vidlock[s3]"
```

```python
# settings.py
INSTALLED_APPS = [..., 'vidlock']

VIDLOCK = {
    'BACKEND': 'courses.video.LessonBackend',
    'S3_BUCKET': 'my-private-videos',
    'S3_ENDPOINT_URL': 'https://<account>.r2.cloudflarestorage.com',
    'S3_ACCESS_KEY_ID': env('R2_KEY'),
    'S3_SECRET_ACCESS_KEY': env('R2_SECRET'),
}
```

```python
# models.py — your model gains video_key, sealed_state, sealed_playlist, sealed_key…
from vidlock.models import SealedVideoMixin

class Lesson(SealedVideoMixin):
    course = models.ForeignKey(Course, on_delete=models.CASCADE)
```

```python
# courses/video.py — the two questions only your project can answer
from vidlock.backend import SealedBackend

class LessonBackend(SealedBackend):
    def get_video(self, video_id):
        return Lesson.objects.filter(pk=video_id).first()

    def can_watch(self, user, video):
        return video.course.enrolments.filter(user=user, active=True).exists()
```

```python
# urls.py
path('video/', include('vidlock.urls')),
```

**After an upload lands in the bucket**, record the key and seal it in the
background:

```python
from django.db import transaction
from vidlock.pipeline import seal, wants

lesson.forget_seal()           # a replaced video must not keep the old seal
lesson.video_key = uploaded_key
lesson.sealed_state = Lesson.STATE_PENDING if wants(uploaded_key) else ''
lesson.save()

if wants(uploaded_key):
    transaction.on_commit(lambda: seal_task.delay(lesson.pk, uploaded_key))

# tasks.py (Celery — or RQ, a thread, a management command)
@shared_task(acks_late=True)
def seal_task(pk, key):
    return seal(Lesson, pk, key)
```

Until sealing finishes the MP4 keeps playing. If a new upload replaces the
video while ffmpeg runs, the job notices and throws its work away.

**Your playback endpoint** checks access as it always did, then:

```python
from vidlock.views import playback_info

@login_required
def playback(request, pk):
    lesson = get_object_or_404(Lesson, pk=pk)
    if not LessonBackend().can_watch(request.user, lesson):
        return HttpResponseForbidden()
    return JsonResponse(playback_info(request, lesson))
```

```json
{"format": "hls", "url": "https://…/video/42/media.m3u8?t=…",
 "media_url": "https://bucket…", "key_url": "https://…/video/42/key?t=…", "expires_in": 600}
```

**The page:**

```html
<video id="player" controls playsinline></video>
<script src="{% static 'vidlock/player.js' %}"></script>
<script>
  VidLock.attach(document.getElementById('player'), {
    endpoint: '/lessons/42/playback/',
    onStatus: (message) => { /* show or clear a message */ },
  });
</script>
```

### Bucket CORS

hls.js reads byte ranges from the bucket with XHR, so the bucket must allow
it. The signed URL is still the only way in; CORS only lets the browser read
the answer.

```json
[{"AllowedOrigins": ["*"], "AllowedMethods": ["GET", "HEAD"],
  "AllowedHeaders": ["range"], "ExposeHeaders": ["content-length", "content-range"],
  "MaxAgeSeconds": 3600}]
```

### Mobile apps

Authenticate the playback request with a Bearer token (DRF/SimpleJWT set
`request.auth`) and vidlock issues an **app** token, which needs no cookie.
Hand `url` to ExoPlayer's `HlsMediaSource` or `AVPlayer`: both fetch the key
themselves. Before `expires_in`, ask again and reload at the current position.

## Settings

| Key | Default | |
|---|---|---|
| `BACKEND` | — | Dotted path to your `SealedBackend` subclass. |
| `STORAGE` | S3Storage | Dotted path to a class with `download`, `upload`, `signed_url`, `delete`. |
| `S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_REGION` | | For the default storage. |
| `FFMPEG_BINARY` | `ffmpeg` | |
| `SEGMENT_SECONDS` | `10` | Each range is one GET on the bucket. 30 means a third of the billed requests and coarser seeking. |
| `TOKEN_TTL` | `600` | Seconds a token and a signed media URL live. |
| `KEY_FETCHES_PER_HOUR` | `20` | Per viewer and video; beyond it the key is refused (429). |
| `ON_KEY_ABUSE` | — | Dotted path to `fn(request, user, video)`, called once a day per viewer and video past the limit. |
| `HLS_JS_URL` | jsDelivr hls.js 1.5.20 light | |

With several tenants or sites on one cache, override
`SealedBackend.namespace(request)` so their fetch counters stay apart.

## Costs

On Cloudflare R2 (free egress), a 45-minute lesson at 10-second segments is
about 270 class-B reads per full view: the free 10 million a month cover
roughly 37,000 full views. Sealing costs a second of one core per upload.

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
Security problems: please read [SECURITY.md](SECURITY.md) first and do not
open a public issue.

## License

MIT © Nader Yasser

---

## بالعربي

**اقفل فيديوهات كورساتك.** مكتبة Django بتحوّل كل فيديو بيترفع لملف واحد
مشفّر (HLS بـAES-128). اللي ينزّل الفيديو من الرابط ياخد ملف مالوش أي لازمة
من غير المفتاح، والمفتاح بيروح بس للطالب اللي إنت سامحله يتفرج، ولمدة ١٠ دقايق.

معمولة للمنصات التعليمية اللي ميزانيتها محدودة: من غير رخصة DRM، ومن غير
خدمة فيديو مدفوعة، ومن غير إعادة ترميز. أمر ffmpeg واحد لكل فيديو (ثانية
تقريبًا)، وملف واحد في الـbucket.

**بتعمل إيه:**
- **بتشفّر الفيديو لوحدها بعد الرفع،** والـMP4 الأصلي بيتمسح بعدها.
- **المفتاح بيتفحص مع كل طلب:** بتسأل دالتك `can_watch` كل مرة، فالطالب
  اللي اشتراكه اتلغى بيتقفل عليه فورًا.
- **برامج التحميل مابتاخدش المفتاح:** زي yt-dlp وN_m3u8DL-RE، لأن التوكن
  بتاع المتصفح لازم يكون معاه كوكي نفس الطالب. واللي يطلب المفتاح كتير في
  الساعة بيترفض وإنت بيوصلك تنبيه.
- **بتشتغل على كل حاجة:** كروم وفايرفوكس بـhls.js، وسفاري والآيفون من غير
  حاجة، والأبلكيشن بـExoPlayer أو AVPlayer.

**مش DRM:** حد فاهم ومصمّم يقدر يوصل للمفتاح في الآخر. الهدف إن الملف المتحمّل
مايشتغلش، وإن البرامج السهلة تفشل. استعملها مع علامة مائية باسم الطالب ضد
تسجيل الشاشة.

المشاركة مرحّب بيها: افتح Issue أو Pull Request، والتفاصيل في
[CONTRIBUTING.md](CONTRIBUTING.md).
