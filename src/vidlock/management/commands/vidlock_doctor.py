"""Check a vidlock setup end to end, and say what to fix.

    manage.py vidlock_doctor              # everything, including a storage round trip
    manage.py vidlock_doctor --offline    # no request to the bucket

Unlike ``manage.py check`` it does things: seals a two-second clip, puts a
file in your storage and reads it back through a signed URL with a Range
request, round-trips a key through your key-encryption secrets, and checks
that the cache is shared and the migrations are applied.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from django.conf import settings
from django.core.cache import cache, caches
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from vidlock import checks, conf, edge, keys, packager, transcode
from vidlock.storage import default_storage


class Command(BaseCommand):
    help = 'Check the vidlock setup end to end: ffmpeg, storage, cache, keys, database, backend.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--offline', action='store_true', help='Skip reading back from storage over HTTP.'
        )

    def handle(self, *args, offline=False, **opts):
        self.failures = 0
        for title, step in (
            ('settings', self.settings_ok),
            ('backend', self.backend_ok),
            ('ffmpeg', self.ffmpeg_ok),
            ('transcode', self.transcode_ok),
            ('cache', self.cache_ok),
            ('key encryption', self.keys_ok),
            ('database', self.database_ok),
            ('storage', lambda: self.storage_ok(offline)),
            ('edge', lambda: self.edge_ok(offline)),
        ):
            try:
                step()
            except Exception as exc:
                self.bad(title, f'{type(exc).__name__}: {exc}')
        if self.failures:
            raise CommandError(f'{self.failures} problem(s) found.')
        self.stdout.write(self.style.SUCCESS('vidlock is ready.'))

    # -- output ----------------------------------------------------------

    def good(self, title, detail=''):
        self.stdout.write(f'  ok    {title}' + (f' — {detail}' if detail else ''))

    def warn(self, title, detail):
        self.stdout.write(self.style.WARNING(f'  warn  {title} — {detail}'))

    def bad(self, title, detail):
        self.failures += 1
        self.stdout.write(self.style.ERROR(f'  FAIL  {title} — {detail}'))

    # -- steps -----------------------------------------------------------

    def settings_ok(self):
        silenced = set(getattr(settings, 'SILENCED_SYSTEM_CHECKS', []))
        found = [p for p in checks.check_settings(None) if p.id not in silenced]
        for problem in found:
            (self.bad if problem.level >= 40 else self.warn)(f'settings {problem.id}', problem.msg)
        if not found:
            self.good('settings')

    def backend_ok(self):
        cls = conf.load('BACKEND')
        if cls is None:
            return self.bad('backend', "VIDLOCK['BACKEND'] is not set")
        from vidlock.backend import SealedBackend

        missing = [
            name for name in ('get_video', 'can_watch') if getattr(cls, name) is getattr(SealedBackend, name)
        ]
        if missing:
            return self.bad('backend', f'{cls.__name__} does not implement {", ".join(missing)}')
        self.good('backend', cls.__name__)

    def ffmpeg_ok(self):
        binary = packager.ffmpeg_binary()
        if not binary:
            return self.warn(
                'ffmpeg', 'not on this machine: fine for web servers, needed on the sealing worker'
            )
        probe = packager.ffprobe_binary()
        with tempfile.TemporaryDirectory(prefix='vidlock-doctor-') as work:
            clip = os.path.join(work, 'clip.mp4')
            made = subprocess.run(
                [
                    binary, '-hide_banner', '-loglevel', 'error', '-nostdin',
                    '-f', 'lavfi', '-i', 'testsrc=duration=2:size=160x120:rate=25',
                    '-f', 'lavfi', '-i', 'sine=duration=2',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', clip,
                ],
                capture_output=True, text=True, timeout=120, check=False,
            )  # fmt: skip
            if made.returncode:
                return self.warn('ffmpeg', f'{binary} found, but it cannot make a test clip (no libx264?)')
            found = packager.probe(clip)
            if found.problem:
                return self.bad('ffmpeg', f'probing a plain H.264 clip says: {found.problem}')
            ts_path, playlist, content_keys = packager.package(clip, work)
            back = packager.unpackage(ts_path, playlist, content_keys, os.path.join(work, 'back.mp4'))
            if tuple(packager.probe(back)) != ('h264', 'aac'):
                return self.bad('ffmpeg', 'a sealed clip did not decrypt back')
        self.good(
            'ffmpeg',
            f'{binary}, ffprobe {"found" if probe else "missing (slower fallback)"}; seal + unseal work',
        )

    def transcode_ok(self):
        if not conf.get('TRANSCODE'):
            return self.good('transcode', "off (VIDLOCK['TRANSCODE']): HEVC/10-bit uploads stay unsealed")
        binary = packager.ffmpeg_binary()
        if not binary:
            return self.warn('transcode', 'no ffmpeg here: needed on the worker that runs the re-encodes')
        compiled = transcode.compiled_encoders(binary)
        missing = {'libx264', 'aac'} - compiled
        if missing:
            return self.bad('transcode', f'ffmpeg has no {" or ".join(sorted(missing))} encoder')
        hardware = [
            f'{name} {"works" if transcode.works(name, binary) else "not usable"}'
            for name in transcode.HARDWARE
            if name in compiled
        ]
        chosen = transcode.encoder()
        with tempfile.TemporaryDirectory(prefix='vidlock-doctor-') as work:
            clip = os.path.join(work, 'clip.mp4')
            # 4:4:4 H.264: plays in VLC, nowhere else — exactly what needs a re-encode.
            made = subprocess.run(
                [
                    binary, '-hide_banner', '-loglevel', 'error', '-nostdin',
                    '-f', 'lavfi', '-i', 'testsrc2=duration=2:size=640x360:rate=25',
                    '-f', 'lavfi', '-i', 'sine=duration=2',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv444p', '-c:a', 'aac', '-shortest', clip,
                ],
                capture_output=True, text=True, timeout=120, check=False,
            )  # fmt: skip
            if made.returncode:
                return self.bad('transcode', 'could not make a test clip')
            found = packager.probe(clip)
            started = time.monotonic()
            used = transcode.transcode_file(clip, os.path.join(work, 'out.mp4'), found)
            took = time.monotonic() - started
            after = packager.probe(os.path.join(work, 'out.mp4'))
            if after.problem:
                return self.bad('transcode', f'the re-encoded clip still cannot be sealed: {after.problem}')
            packager.package(os.path.join(work, 'out.mp4'), work)
        detail = f'{used} ({chosen} chosen; 2 s clip in {took:.1f} s)'
        if hardware:
            detail += '; ' + ', '.join(hardware)
        if not transcode.has_filter('zscale', binary):
            detail += '; no zscale: HDR uploads are converted without tone-mapping'
        if not conf.get('ENQUEUE_TRANSCODE'):
            return self.warn('transcode', detail + '; ENQUEUE_TRANSCODE unset: encodes run inside seal jobs')
        self.good('transcode', detail)

    def cache_ok(self):
        key = f'vidlock:doctor:{secrets.token_hex(4)}'
        cache.set(key, 1, 30)
        if cache.get(key) != 1:
            return self.bad('cache', 'a value written to the cache could not be read back')
        cache.delete(key)
        backend = type(caches['default']).__name__
        if backend in ('LocMemCache', 'DummyCache'):
            return self.warn(
                'cache', f'{backend} is per process: limits, streams and risk scores need Redis/Memcached/DB'
            )
        self.good('cache', backend)

    def keys_ok(self):
        sample = secrets.token_bytes(48)
        if keys.unwrap(keys.wrap(sample)) != sample:
            return self.bad('key encryption', 'a key did not survive wrap/unwrap')
        if not conf.get('KEY_ENCRYPTION_KEYS'):
            return self.warn('key encryption', "derived from SECRET_KEY; set VIDLOCK['KEY_ENCRYPTION_KEYS']")
        self.good('key encryption', f'{len(conf.get("KEY_ENCRYPTION_KEYS"))} secret(s)')

    def database_ok(self):
        from vidlock.models import WatchProgress

        if WatchProgress._meta.db_table not in connection.introspection.table_names():
            return self.bad('database', 'the WatchProgress table is missing: run manage.py migrate')
        self.good('database', 'migrations applied')

    def storage_ok(self, offline):
        storage = default_storage()
        key = f'vidlock-doctor/{secrets.token_hex(8)}.txt'
        body = b'vidlock doctor ' + secrets.token_hex(8).encode()
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(body)
        try:
            stored = storage.upload(fh.name, key, 'text/plain') or key
            url = storage.signed_url(stored, 60)
            if offline or not url.startswith(('http://', 'https://')):
                self.good('storage', f'{type(storage).__name__}: upload and signing work (not read back)')
                return
            request = urllib.request.Request(url, headers={'Range': 'bytes=0-6'})
            with urllib.request.urlopen(request, timeout=20) as answer:
                part = answer.read()
                status = answer.status
            if part != body[:7]:
                return self.bad('storage', f'reading back through the signed URL gave {status} {part[:20]!r}')
            if status != 206:
                self.warn('storage', f'the bucket ignored the Range header (HTTP {status}); players need it')
            else:
                self.good('storage', f'{type(storage).__name__}: upload, signed URL and Range reads work')
        finally:
            os.unlink(fh.name)
            storage.delete(key)

    def edge_ok(self, offline):
        if not edge.enabled():
            return self.good(
                'edge', "off (VIDLOCK['DELIVERY'] = 'direct'): players read presigned bucket URLs"
            )
        if offline:
            edge.url_for('vidlock-doctor/x.ts', 60)
            return self.good('edge', 'URLs sign (not fetched: --offline)')
        storage = default_storage()
        key = f'vidlock-doctor/{secrets.token_hex(8)}.ts'
        body = secrets.token_bytes(4096)
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(body)
        try:
            stored = storage.upload(fh.name, key, 'video/mp2t') or key
            url = edge.url_for(stored, 60, edge.viewer_tag('vidlock-doctor'))
            answers = []
            for _ in range(2):
                request = urllib.request.Request(url, headers={'Range': 'bytes=0-1023'})
                started = time.monotonic()
                with urllib.request.urlopen(request, timeout=20) as answer:
                    part = answer.read()
                    answers.append((answer.status, answer.headers, part, (time.monotonic() - started) * 1000))
            status, headers, part, ms = answers[0]
            if status != 206 or part != body[:1024]:
                return self.bad('edge', f'{conf.get("EDGE_URL")} answered {status} with {len(part)} bytes')
            if not headers.get('x-vidlock-edge'):
                return self.bad('edge', 'no x-vidlock-edge header: is EDGE_URL routed to the Worker?')
            tampered = url[:-1] + ('0' if url[-1] != '0' else '1')
            try:
                urllib.request.urlopen(
                    urllib.request.Request(tampered, headers={'Range': 'bytes=0-9'}), timeout=20
                )
                return self.bad('edge', 'the Worker served a URL with a tampered signature')
            except urllib.error.HTTPError as exc:
                if exc.code != 403:
                    return self.bad('edge', f'a tampered signature got HTTP {exc.code}, not 403')
            second = answers[1][1].get('x-vidlock-cache', '?')
            detail = (
                f'206, {headers.get("x-vidlock-edge")}, {ms:.0f} ms; second read {second} '
                f'in {answers[1][3]:.0f} ms; tampered signature 403'
            )
            if second != 'HIT':
                return self.warn(
                    'edge', detail + ' (a HIT needs the Worker on a custom domain, not workers.dev)'
                )
            self.good('edge', detail)
        finally:
            os.unlink(fh.name)
            storage.delete(key)
