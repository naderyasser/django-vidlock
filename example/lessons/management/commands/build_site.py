"""Freeze the demo into static pages for a static host (Vercel, Netlify,
GitHub Pages): the real templates, styles and motion, with a plain copy of
the sample lesson in place of the sealed stream.

    python manage.py demo && python manage.py build_site ../site
"""

import os
import re
import shutil
import subprocess
import tempfile

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.test import Client, override_settings

from lessons.management.commands.demo import NARRATION
from lessons.models import Lesson
from vidlock import packager
from vidlock.models import Transcript
from vidlock.transcribe import webvtt

STATIC = os.path.join(os.path.dirname(__file__), '..', '..', 'static', 'lessons')

BANNER = (
    '<div class="preview-bar"><b>Live preview</b><span>The django-vidlock demo as static pages: '
    'the design and motion are real; sign-in, uploads and the sealed stream need the Django server.'
    '</span><a href="https://github.com/naderyasser/django-vidlock">GitHub →</a></div>'
)

PREVIEW_CSS = """
.preview-bar{position:relative;z-index:30;display:flex;flex-wrap:wrap;gap:6px 14px;justify-content:center;align-items:center;padding:8px 16px;font-size:12px;letter-spacing:.04em;color:var(--muted);background:rgba(230,200,121,.07);border-bottom:1px solid rgba(230,200,121,.2);text-align:center}
.preview-bar b{color:var(--gold);font-weight:500;letter-spacing:.14em;text-transform:uppercase;font-size:11px}
.video-wrap{position:relative;overflow:hidden;border-radius:10px}
.demo-mark{position:absolute;left:50%;top:45%;pointer-events:none;font:600 clamp(11px,2vw,20px)/1 system-ui,sans-serif;color:rgba(255,255,255,.55);text-shadow:0 1px 3px rgba(0,0,0,.6);white-space:nowrap;animation:mark-drift 18s ease-in-out infinite alternate}
@keyframes mark-drift{0%{transform:translate(-90%,-180%)}50%{transform:translate(-30%,60%)}100%{transform:translate(-10%,-140%)}}
.preview-note{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:40;padding:10px 16px;border-radius:12px;background:rgba(14,18,34,.95);border:1px solid var(--border);color:var(--text);font-size:13px;box-shadow:var(--shadow)}
@media (prefers-reduced-motion: reduce){.demo-mark{animation:none}}
"""

PREVIEW_JS = """
document.querySelectorAll('form').forEach(function (form) {
  if (form.getAttribute('action')) { form.method = 'get'; return; }
  form.addEventListener('submit', function (event) {
    event.preventDefault();
    var note = document.createElement('div');
    note.className = 'preview-note';
    note.textContent = 'This is a static preview: this form needs the Django server.';
    document.body.appendChild(note);
    setTimeout(function () { note.remove(); }, 2600);
  });
});
"""

PLAYER = """<div class="video-wrap"><video id="player" controls playsinline muted autoplay style="width:100%" src="lesson.mp4"><track kind="captions" srclang="en" label="English" default src="captions.vtt"></video><div class="demo-mark">student · B2NXV4 · <span id="clock"></span></div></div>"""

PLAYER_JS = """<script>
var video = document.getElementById('player');
window.player = {
  seek: function (t) {
    function go() { video.currentTime = t; video.play(); }
    if (video.readyState >= 1) go(); else video.addEventListener('loadedmetadata', go, { once: true });
  },
};
var jump = location.hash.match(/t=([\\d.]+)/);
if (jump) player.seek(parseFloat(jump[1]));
(function tick() {
  var now = new Date();
  document.getElementById('clock').textContent = now.toISOString().slice(0, 10) + ' ' + now.toTimeString().slice(0, 5);
  setTimeout(tick, 15000);
})();
</script>"""

ROUTES = [
    (r'href="/lessons/\d+/report/"', 'href="report.html"'),
    (r'href="/lessons/\d+/#t=([\d.]+)"', r'href="watch.html#t=\1"'),
    (r'href="/lessons/\d+/"', 'href="watch.html"'),
    (r'href="/"', 'href="index.html"'),
    (r'action="/search/"', 'action="search.html"'),
    (r'action="/logout/"', 'action="login.html"'),
]


class Command(BaseCommand):
    help = 'Write the demo as static pages (for Vercel and other static hosts).'

    def add_arguments(self, parser):
        parser.add_argument('out', help='Folder to write, e.g. ../site')
        parser.add_argument('--seconds', type=int, default=45, help='Length of the lesson clip.')

    def handle(self, out, seconds, **opts):
        lesson = Lesson.objects.order_by('pk').first()
        if lesson is None:
            call_command('demo', verbosity=0)
            lesson = Lesson.objects.order_by('pk').first()
        os.makedirs(out, exist_ok=True)
        with override_settings(DEBUG=True, ALLOWED_HOSTS=['*']):
            pages = self._render(lesson)
        for name, html in pages.items():
            with open(os.path.join(out, name), 'w', encoding='utf-8') as fh:
                fh.write(self._rewrite(name, html))
        with open(os.path.join(STATIC, 'theme.css'), encoding='utf-8') as fh:
            css = fh.read()
        with open(os.path.join(out, 'theme.css'), 'w', encoding='utf-8') as fh:
            fh.write(css + PREVIEW_CSS)
        for name in ('constellation.js', 'motion.js'):
            shutil.copy(os.path.join(STATIC, name), os.path.join(out, name))
        with open(os.path.join(out, 'preview.js'), 'w', encoding='utf-8') as fh:
            fh.write(PREVIEW_JS)
        transcript = Transcript.objects.filter(video_type='lessons.Lesson', video_id=str(lesson.pk)).first()
        with open(os.path.join(out, 'captions.vtt'), 'w', encoding='utf-8') as fh:
            fh.write(webvtt(transcript) if transcript else 'WEBVTT\n')
        self._clip(os.path.join(out, 'lesson.mp4'), seconds)
        self.stdout.write(f'static site written to {out}')

    def _render(self, lesson):
        client = Client()
        pages = {'login.html': client.get('/login/').content.decode()}
        student = get_user_model().objects.get(username='student')
        teacher = get_user_model().objects.get(username='teacher')
        client.force_login(student)
        pages['watch.html'] = client.get(f'/lessons/{lesson.pk}/?q=focal+length').content.decode()
        pages['search.html'] = client.get('/search/?q=lens').content.decode()
        client.force_login(teacher)
        pages['index.html'] = client.get('/').content.decode()
        pages['report.html'] = client.get(f'/lessons/{lesson.pk}/report/').content.decode()
        return pages

    def _rewrite(self, name, html):
        for pattern, replacement in ROUTES:
            html = re.sub(pattern, replacement, html)
        static = settings.STATIC_URL
        html = html.replace(f'href="{static}lessons/theme.css"', 'href="theme.css"')
        html = html.replace(f'src="{static}lessons/constellation.js"', 'src="constellation.js"')
        html = html.replace(f'src="{static}lessons/motion.js"', 'src="motion.js"')
        html = html.replace(f'<script src="{static}vidlock/upload.js" defer></script>', '')
        html = html.replace('</body>', '<script src="preview.js"></script>\n</body>')
        html = re.sub(r'<input type="hidden" name="csrfmiddlewaretoken"[^>]*>', '', html)
        html = html.replace('<body>\n', '<body>\n' + BANNER + '\n', 1)
        if name == 'login.html':
            html = html.replace('<form method="post">', '<form method="get" action="index.html">')
        if name == 'watch.html':
            html = re.sub(r'<video id="player"[^>]*></video>', lambda m: PLAYER, html)
            html = re.sub(
                rf'<script src="{static}vidlock/player\.js".*?</script>\s*<script>.*?</script>',
                lambda m: PLAYER_JS,
                html,
                flags=re.S,
            )
            html = html.replace(
                '<p id="status" class="status"></p>',
                '<p id="status" class="status">Preview: a plain copy of the lesson. On the real site this is '
                'the sealed HLS stream, with a key for every minute.</p>',
            )
        if static in html:
            raise CommandError(f'{name} still points at {static}: the templates changed; update build_site.')
        return html

    def _clip(self, path, seconds):
        ffmpeg = packager.ffmpeg_binary()
        if not ffmpeg:
            raise CommandError('ffmpeg is not installed.')
        with tempfile.TemporaryDirectory(prefix='vidlock-site-') as work:
            voice = os.path.join(work, 'voice.wav')
            if shutil.which('espeak-ng'):
                subprocess.run(['espeak-ng', '-v', 'en', '-s', '145', '-w', voice, NARRATION], check=True)
                audio = ['-i', voice, '-af', 'apad']
            else:
                audio = ['-f', 'lavfi', '-i', f'sine=frequency=330:duration={seconds}']
            subprocess.run(
                [
                    ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                    '-f', 'lavfi', '-i', f'testsrc2=duration={seconds}:size=640x360:rate=25', *audio,
                    '-t', str(seconds), '-c:v', 'libx264', '-preset', 'slow', '-crf', '30', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-b:a', '64k', '-movflags', '+faststart', path,
                ],
                check=True,
            )  # fmt: skip
