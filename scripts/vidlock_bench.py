#!/usr/bin/env python3
"""Measure how a sealed video's bytes reach a viewer: per-request TTFB, status,
cache status and throughput, from wherever you run it.

Standard library only (Python 3.8+): copy this one file to a laptop in Cairo
or Riyadh and run it there. It plays through a sealed video's byte ranges the
way a player does (the exact BYTERANGEs of the playlist, in order, on one
kept-alive connection, plus a few seeks), and never asks for a key: it
measures delivery, and leaves nobody's risk score touched.

Get a playlist URL from your server (it lives TOKEN_TTL, 10 minutes):

    manage.py vidlock_bench_url courses.Lesson 42 --user bench-account

Then, on the client machine:

    python3 vidlock_bench.py 'https://site/video/42/media.m3u8?t=...' \\
        --label 'Cairo, WE fibre, direct R2' --json cairo-direct.json

    # later, after switching DELIVERY to the edge Worker:
    python3 vidlock_bench.py '<new playlist URL>' --label 'Cairo, edge' --json cairo-edge.json --passes 2
    python3 vidlock_bench.py --compare cairo-direct.json cairo-edge.json

Modes (``--mode``):

* ``play`` (default): the first ``--segments`` ranges in order, like a
  viewer pressing play, then ``--seeks`` random ranges, like seeking.
* ``all``: every range in order (a whole lesson: one GET per segment).
* ``seek``: ``--seeks`` random ranges only.

``--passes 2`` repeats the same ranges: through the edge Worker, the first
pass shows ``cf-cache-status: MISS`` and the second ``HIT``. Direct signed
R2 URLs are not cached, so both passes go to R2.
"""

from __future__ import annotations

import argparse
import http.client
import json
import random
import re
import socket
import ssl
import statistics
import sys
import time
import urllib.parse
import urllib.request

USER_AGENT = 'vidlock-bench/1'
_BYTERANGE = re.compile(r'^#EXT-X-BYTERANGE:(\d+)(?:@(\d+))?$')


# -- the playlist ----------------------------------------------------------------


def parse_playlist(text: str, base_url: str = '') -> list[dict]:
    """``[{'url', 'start', 'length', 'duration'}]``, one per segment, in order."""
    out = []
    duration = 0.0
    length = start = None
    offset = 0
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith('#EXTINF:'):
            duration = float(line[len('#EXTINF:') :].split(',')[0])
        elif line.startswith('#EXT-X-BYTERANGE:'):
            found = _BYTERANGE.match(line)
            if found:
                length = int(found.group(1))
                start = int(found.group(2)) if found.group(2) is not None else offset
        elif line and not line.startswith('#'):
            url = urllib.parse.urljoin(base_url, line)
            if length is not None:
                out.append({'url': url, 'start': start, 'length': length, 'duration': duration})
                offset = start + length
            else:
                out.append({'url': url, 'start': None, 'length': None, 'duration': duration})
            length = start = None
    return out


def fetch_playlist(url: str, timeout: float = 30) -> tuple[list[dict], dict]:
    started = time.perf_counter()
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as answer:
        body = answer.read().decode('utf-8', 'replace')
        meta = {
            'status': answer.status,
            'ms': round((time.perf_counter() - started) * 1000, 1),
            'cache_control': answer.headers.get('Cache-Control', ''),
        }
    if not body.lstrip().startswith('#EXTM3U'):
        raise SystemExit(f'not an HLS playlist (HTTP {meta["status"]}): {body[:120]!r}')
    if '#EXT-X-STREAM-INF' in body:
        raise SystemExit('this is a master playlist: pass one rendition\'s media playlist URL')
    return parse_playlist(body, url), meta


# -- measuring ---------------------------------------------------------------------


def delivery_of(url: str) -> str:
    host = urllib.parse.urlsplit(url).hostname or ''
    if host.endswith('.r2.cloudflarestorage.com'):
        return 'direct-r2'
    if host.endswith('.amazonaws.com'):
        return 'direct-s3'
    return 'edge-or-other'


def colo_of(headers) -> str:
    """The Cloudflare data centre that answered, from cf-ray (``…-CAI``)."""
    ray = headers.get('cf-ray', '') or ''
    return ray.rsplit('-', 1)[-1] if '-' in ray else ''


class Connection:
    """One kept-alive connection per host, like a player's."""

    def __init__(self, timeout: float):
        self.timeout = timeout
        self.conns: dict[tuple, http.client.HTTPConnection] = {}

    def _open(self, scheme, host, port):
        started = time.perf_counter()
        if scheme == 'https':
            conn = http.client.HTTPSConnection(
                host, port, timeout=self.timeout, context=ssl.create_default_context()
            )
        else:
            conn = http.client.HTTPConnection(host, port, timeout=self.timeout)
        conn.connect()
        return conn, (time.perf_counter() - started) * 1000

    def get(self, url: str, headers: dict) -> dict:
        parts = urllib.parse.urlsplit(url)
        scheme = parts.scheme
        port = parts.port or (443 if scheme == 'https' else 80)
        key = (scheme, parts.hostname, port)
        target = parts.path + (f'?{parts.query}' if parts.query else '')
        connect_ms = 0.0
        for attempt in (0, 1):
            conn = self.conns.get(key)
            if conn is None:
                conn, connect_ms = self._open(scheme, parts.hostname, port)
                self.conns[key] = conn
            try:
                sent = time.perf_counter()
                conn.request('GET', target, headers={'User-Agent': USER_AGENT, **headers})
                response = conn.getresponse()
                first = time.perf_counter()
                body = response.read()
                done = time.perf_counter()
                break
            except (http.client.HTTPException, OSError):
                conn.close()
                self.conns.pop(key, None)
                if attempt:
                    raise
        return {
            'status': response.status,
            'headers': {k.lower(): v for k, v in response.getheaders()},
            'bytes': len(body),
            'connect_ms': round(connect_ms, 1),
            'ttfb_ms': round((first - sent) * 1000, 1),
            'total_ms': round((done - sent) * 1000, 1),
        }

    def close(self):
        for conn in self.conns.values():
            conn.close()


def plan(segments: list[dict], mode: str, count: int, seeks: int, seed: int) -> list[int]:
    indexes = list(range(len(segments)))
    rng = random.Random(seed)
    if mode == 'all':
        return indexes
    chosen = [] if mode == 'seek' else indexes[: max(0, count)]
    rest = [i for i in indexes if i not in set(chosen)] or indexes
    chosen += rng.sample(rest, min(seeks, len(rest)))
    return chosen


EMPTY = {'status': 0, 'headers': {}, 'bytes': 0, 'connect_ms': 0, 'ttfb_ms': 0, 'total_ms': 0}


def measure(segments, order, passes, timeout, verbose=True, out=None):
    conn = Connection(timeout)
    rows = []
    try:
        for number in range(1, passes + 1):
            for position, index in enumerate(order):
                segment = segments[index]
                headers = {}
                if segment['length'] is not None:
                    end = segment['start'] + segment['length'] - 1
                    headers['Range'] = f'bytes={segment["start"]}-{end}'
                try:
                    result = conn.get(segment['url'], headers)
                    error = ''
                except (OSError, http.client.HTTPException, ssl.SSLError, socket.timeout) as exc:
                    result = dict(EMPTY)
                    error = f'{type(exc).__name__}: {exc}'
                h = result['headers']
                expected = segment['length']
                honoured = result['status'] == 206 and bool(h.get('content-range'))
                range_ok = expected is None or (honoured and result['bytes'] == expected)
                row = {
                    'pass': number,
                    'seq': position + 1,
                    'segment': index,
                    'range': headers.get('Range', ''),
                    'status': result['status'],
                    'bytes': result['bytes'],
                    'connect_ms': result['connect_ms'],
                    'ttfb_ms': result['ttfb_ms'],
                    'total_ms': result['total_ms'],
                    'mbps': (
                        round(result['bytes'] * 8 / (result['total_ms'] / 1000) / 1e6, 2)
                        if result['total_ms']
                        else 0.0
                    ),
                    'cache': h.get('cf-cache-status', ''),
                    'colo': colo_of(h),
                    'edge': h.get('x-vidlock-edge', ''),
                    'content_range': h.get('content-range', ''),
                    'cache_control': h.get('cache-control', ''),
                    'age': h.get('age', ''),
                    'range_ok': range_ok,
                    'error': error,
                }
                rows.append(row)
                if verbose:
                    print_row(row, out)
    finally:
        conn.close()
    return rows


# -- reporting ---------------------------------------------------------------------

HEADER = (
    f'{"pass":>4} {"#":>3} {"seg":>4} {"status":>6} {"bytes":>9} {"conn ms":>7} {"ttfb ms":>7} '
    f'{"total ms":>8} {"Mbit/s":>7} {"cache":>7} {"colo":>4}  content-range'
)


def print_row(row, out=None):
    out = out or sys.stdout
    flag = '' if row['range_ok'] else '  <-- range not honoured'
    if row['error']:
        flag = f'  ERROR {row["error"]}'
    print(
        f'{row["pass"]:>4} {row["seq"]:>3} {row["segment"]:>4} {row["status"]:>6} {row["bytes"]:>9} '
        f'{row["connect_ms"]:>7} {row["ttfb_ms"]:>7} {row["total_ms"]:>8} {row["mbps"]:>7} '
        f'{row["cache"] or "-":>7} {row["colo"] or "-":>4}  {row["content_range"]}{flag}',
        file=out,
    )


def percentile(values, share):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(share * (len(ordered) - 1))))
    return ordered[index]


def summarise(rows) -> dict:
    by_pass = {}
    for number in sorted({r['pass'] for r in rows}):
        part = [r for r in rows if r['pass'] == number]
        good = [r for r in part if not r['error'] and r['status'] in (200, 206)]
        ttfb = [r['ttfb_ms'] for r in good]
        total_bytes = sum(r['bytes'] for r in good)
        total_ms = sum(r['total_ms'] for r in good)
        caches = {}
        for r in part:
            caches[r['cache'] or '-'] = caches.get(r['cache'] or '-', 0) + 1
        by_pass[number] = {
            'requests': len(part),
            'errors': len(part) - len(good),
            'range_not_honoured': sum(1 for r in part if not r['range_ok']),
            'ttfb_p50_ms': round(percentile(ttfb, 0.5), 1),
            'ttfb_p90_ms': round(percentile(ttfb, 0.9), 1),
            'ttfb_p99_ms': round(percentile(ttfb, 0.99), 1),
            'ttfb_mean_ms': round(statistics.fmean(ttfb), 1) if ttfb else 0.0,
            'mbps': round(total_bytes * 8 / (total_ms / 1000) / 1e6, 2) if total_ms else 0.0,
            'bytes': total_bytes,
            'cache': caches,
            'colos': sorted({r['colo'] for r in part if r['colo']}),
        }
    return by_pass


def print_summary(label, delivery, summary, out=None):
    out = out or sys.stdout
    print(f'\n{label or "run"} [{delivery}]', file=out)
    for number, s in summary.items():
        print(
            f'  pass {number}: {s["requests"]} requests, {s["errors"]} errors, '
            f'{s["range_not_honoured"]} ranges not honoured | TTFB p50 {s["ttfb_p50_ms"]} ms, '
            f'p90 {s["ttfb_p90_ms"]} ms, p99 {s["ttfb_p99_ms"]} ms | {s["mbps"]} Mbit/s | '
            f'cache {s["cache"]} | colo {", ".join(s["colos"]) or "-"}',
            file=out,
        )


def compare(paths, out=None):
    out = out or sys.stdout
    runs = []
    for path in paths:
        with open(path) as fh:
            runs.append(json.load(fh))
    print(
        f'{"run":40} {"delivery":14} {"pass":>4} {"p50":>7} {"p90":>7} {"p99":>7} '
        f'{"Mbit/s":>7} {"err":>4}  cache',
        file=out,
    )
    for run in runs:
        for number, s in run['summary'].items():
            print(
                f'{(run.get("label") or run["started"])[:40]:40} {run["delivery"]:14} {number:>4} '
                f'{s["ttfb_p50_ms"]:>7} {s["ttfb_p90_ms"]:>7} {s["ttfb_p99_ms"]:>7} {s["mbps"]:>7} '
                f'{s["errors"]:>4}  {s["cache"]}',
                file=out,
            )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('playlist', nargs='?', help="A sealed video's playlist URL, with its token.")
    parser.add_argument('--label', default='', help='Where and how this ran, e.g. "Riyadh, STC 5G, direct".')
    parser.add_argument('--mode', choices=('play', 'all', 'seek'), default='play')
    parser.add_argument('--segments', type=int, default=30, help='play mode: ranges played in order.')
    parser.add_argument('--seeks', type=int, default=10, help='Random ranges after (or instead of) playing.')
    parser.add_argument('--passes', type=int, default=1, help='Repeat the same ranges (MISS, then HIT).')
    parser.add_argument('--seed', type=int, default=1, help='Same seed, same seeks: comparable runs.')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--json', metavar='FILE', help='Write every request and the summary here.')
    parser.add_argument('--quiet', action='store_true', help='Only the summary.')
    parser.add_argument('--compare', nargs='+', metavar='FILE', help='Compare earlier --json runs.')
    args = parser.parse_args(argv)

    if args.compare:
        compare(args.compare)
        return 0
    if not args.playlist:
        parser.error('give a playlist URL (or --compare files)')

    started = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    segments, meta = fetch_playlist(args.playlist, args.timeout)
    if not segments:
        raise SystemExit('the playlist names no media')
    media = segments[0]['url']
    delivery = delivery_of(media)
    print(
        f'playlist: HTTP {meta["status"]} in {meta["ms"]} ms '
        f'({meta["cache_control"] or "no Cache-Control"}); '
        f'{len(segments)} segments, {sum(s["duration"] for s in segments):.0f} s; media on '
        f'{urllib.parse.urlsplit(media).hostname} [{delivery}]'
    )
    order = plan(segments, args.mode, args.segments, args.seeks, args.seed)
    if not args.quiet:
        print(HEADER)
    rows = measure(segments, order, max(1, args.passes), args.timeout, verbose=not args.quiet)
    summary = summarise(rows)
    print_summary(args.label, delivery, summary)
    if args.json:
        with open(args.json, 'w') as fh:
            json.dump(
                {
                    'label': args.label,
                    'started': started,
                    'delivery': delivery,
                    'media_host': urllib.parse.urlsplit(media).hostname,
                    'playlist': meta,
                    'mode': args.mode,
                    'rows': rows,
                    'summary': summary,
                },
                fh,
                indent=1,
            )
        print(f'written to {args.json}')
    return 1 if any(r['error'] or not r['range_ok'] for r in rows) else 0


if __name__ == '__main__':
    sys.exit(main())
