/*
 * vidlock/player.js — plays what your playback endpoint returns.
 *
 *   <video id="player" controls playsinline></video>
 *   <script src="{% static 'vidlock/player.js' %}"></script>
 *   <script>
 *     VidLock.attach(document.getElementById('player'), {
 *       endpoint: '/lessons/42/playback/',   // returns vidlock.views.playback_info(...)
 *       hlsSrc: '{{ VIDLOCK_JS_URL }}',   // loaded only when the video is sealed
 *       onStatus: (message) => { ... },      // optional; null clears it
 *     });
 *   </script>
 *
 * Signed URLs live a few minutes. The player renews them before they expire:
 * an MP4 swaps its src and restores the position; a sealed stream keeps
 * playing and hls.js's xhrSetup opens every later request (media ranges and
 * key) on the newest URLs, so a renewal never reloads the stream.
 */
(function (global) {
  'use strict';

  function attach(video, options) {
    const endpoint = options.endpoint;
    const hlsSrc = options.hlsSrc || 'https://cdn.jsdelivr.net/npm/hls.js@1.5.20/dist/hls.light.min.js';
    const onStatus = options.onStatus || function () {};
    const headers = Object.assign({ 'X-Requested-With': 'XMLHttpRequest' }, options.headers || {});
    let renewTimer = null;
    let hls = null;
    let fresh = {};
    let fatalErrors = 0;
    let recovering = false;

    function loadHlsJs() {
      if (global.Hls) return Promise.resolve();
      return new Promise(function (resolve, reject) {
        const tag = document.createElement('script');
        tag.src = hlsSrc;
        tag.onload = resolve;
        tag.onerror = reject;
        document.head.appendChild(tag);
      });
    }

    function resumeAt(position, wasPlaying) {
      if (position <= 0) return;
      video.addEventListener('loadedmetadata', function once() {
        video.removeEventListener('loadedmetadata', once);
        video.currentTime = position;
        if (wasPlaying) video.play().catch(function () {});
      });
    }

    function dropHls() {
      if (hls) {
        hls.destroy();
        hls = null;
      }
    }

    async function attachSource(data, position, wasPlaying) {
      if (data.format !== 'hls') {
        dropHls();
        video.src = data.url;
        resumeAt(position, wasPlaying);
        return true;
      }
      fresh = { media: data.media_url, key: data.key_url };
      if (hls) return true;
      await loadHlsJs().catch(function () {});
      if (global.Hls && global.Hls.isSupported()) {
        const keyPath = new URL(data.key_url, location.href).pathname;
        hls = new global.Hls({
          startPosition: position > 0 ? position : -1,
          xhrSetup: function (xhr, url) {
            const target = new URL(url, location.href);
            if (target.pathname === keyPath && fresh.key) {
              xhr.open('GET', fresh.key, true);
            } else if (target.origin !== location.origin && fresh.media) {
              xhr.open('GET', fresh.media, true);
            }
          },
        });
        hls.on(global.Hls.Events.ERROR, function (event, detail) {
          if (!detail.fatal) return;
          // A stream that keeps failing (storage down, CORS missing) must not
          // turn into a request loop.
          if (++fatalErrors > 3) {
            dropHls();
            onStatus('The video could not be played. Reload the page in a moment.');
            return;
          }
          setTimeout(function () { fatalErrors = Math.max(0, fatalErrors - 1); }, 120 * 1000);
          const at = video.currentTime;
          const playing = !video.paused;
          dropHls();
          onStatus('Retrying…');
          load({ resume: true, at: at, playing: playing });
        });
        hls.loadSource(data.url);
        hls.attachMedia(video);
        if (wasPlaying) {
          hls.once(global.Hls.Events.MANIFEST_PARSED, function () { video.play().catch(function () {}); });
        }
        return true;
      }
      if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = data.url;
        resumeAt(position, wasPlaying);
        return true;
      }
      onStatus('This browser cannot play protected video. Update Chrome, Firefox or Safari.');
      return false;
    }

    async function load(opts) {
      opts = opts || {};
      const resume = !!opts.resume;
      try {
        const response = await fetch(endpoint, { credentials: 'same-origin', headers: headers });
        const data = await response.json().catch(function () { return {}; });
        if (!response.ok || !data.url) {
          onStatus(data.error || 'The video could not be loaded.');
          return;
        }
        const position = opts.at != null ? opts.at : (resume ? video.currentTime : 0);
        const wasPlaying = opts.playing != null ? opts.playing : (resume && !video.paused);
        if (!(await attachSource(data, position, wasPlaying))) return;
        onStatus(null);
        if (data.expires_in) {
          clearTimeout(renewTimer);
          const lead = Math.max(60, Math.min(300, data.expires_in * 0.1));
          renewTimer = setTimeout(function () { load({ resume: true }); }, (data.expires_in - lead) * 1000);
        }
      } catch (error) {
        onStatus('Could not reach the server.');
      }
    }

    // An expired MP4 URL surfaces here (a laptop waking from sleep, say).
    // hls.js reports its own failures above.
    video.addEventListener('error', function () {
      if (recovering || hls) return;
      recovering = true;
      load({ resume: true }).finally(function () {
        setTimeout(function () { recovering = false; }, 5000);
      });
    });

    load();
    return { reload: function () { return load({ resume: true }); }, destroy: function () { clearTimeout(renewTimer); dropHls(); } };
  }

  global.VidLock = { attach: attach };
})(window);
