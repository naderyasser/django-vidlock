"""``{% vidlock_player %}``: the player script, wired to your settings.

    {% load vidlock %}
    <video id="player" controls playsinline></video>
    {% vidlock_player %}
    <script>VidLock.attach(document.getElementById('player'), {endpoint: '...'})</script>

It loads ``vidlock/player.js`` and tells it where hls.js is: the copy bundled
with vidlock unless ``VIDLOCK['HLS_JS_URL']`` names another (with
``HLS_JS_INTEGRITY`` for Subresource Integrity when it is a CDN).
"""

from django import template
from django.templatetags.static import static
from django.utils.html import format_html

from vidlock import conf

register = template.Library()

BUNDLED_HLS_JS = 'vidlock/vendor/hls.light.min.js'


def hls_js_url():
    return conf.get('HLS_JS_URL') or static(BUNDLED_HLS_JS)


@register.simple_tag
def vidlock_player():
    integrity = conf.get('HLS_JS_INTEGRITY') if conf.get('HLS_JS_URL') else ''
    return format_html(
        '<script src="{}" data-hls-src="{}" data-hls-integrity="{}"></script>',
        static('vidlock/player.js'),
        hls_js_url(),
        integrity or '',
    )
