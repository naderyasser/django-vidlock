/*
 * vidlock/player.js — plays what your playback endpoint returns.
 *
 *   {% load vidlock %}
 *   <video id="player" controls playsinline></video>
 *   {% vidlock_player %}
 *   <script>
 *     VidLock.attach(document.getElementById('player'), {
 *       endpoint: '/lessons/42/playback/',   // returns vidlock.views.playback_info(...)
 *       onStatus: (message) => { ... },      // optional; null clears it
 *       lang: 'ar',                          // optional; default <html lang>, then the browser's
 *       hlsConfig: { maxBufferLength: 30 },  // optional; merged into hls.js's config
 *       onHls: (hls) => { ... },             // optional; the hls.js instance, before it loads
 *     });
 *   </script>
 *
 * Signed URLs live a few minutes. The player renews them before they expire:
 * an MP4 swaps its src and restores the position; a sealed stream keeps
 * playing and hls.js's xhrSetup opens every later request (media ranges and
 * key) on the newest URLs, so a renewal never reloads the stream. Safari's
 * native player reads its playlist once, and vidlock signs that playlist's
 * media URL for the whole video, so it is left alone too.
 *
 * When the playback endpoint returns a `watermark`, it is drawn over the
 * video and moves every few seconds, in fullscreen as well (except iOS's
 * own fullscreen player, which draws nothing over itself).
 */
(function (global) {
  'use strict';

  const script = document.currentScript;
  const scriptData = (script && script.dataset) || {};
  // Without {% vidlock_player %}, guess the bundled copy from this script's
  // own URL — only when that URL is the plain name (a hashed static name like
  // player.3f9a.js says nothing about hls.js's hash).
  const PLAIN_NAME = /player\.js(\?.*)?$/;
  const DEFAULT_HLS = scriptData.hlsSrc ||
    (script && PLAIN_NAME.test(script.src || '') ? script.src.replace(PLAIN_NAME, 'vendor/hls.light.min.js') : '');
  const DEFAULT_INTEGRITY = scriptData.hlsIntegrity || '';

  const MESSAGES = {
    en: {
      failed: 'The video could not be played. Reload the page in a moment.',
      retrying: 'Retrying…',
      unsupported: 'This browser cannot play protected video. Update Chrome, Firefox or Safari.',
      unavailable: 'The video could not be loaded.',
      offline: 'Could not reach the server.',
    },
    ar: {
      failed: 'تعذّر تشغيل الفيديو. أعد تحميل الصفحة بعد قليل.',
      retrying: 'جارٍ إعادة المحاولة…',
      unsupported: 'هذا المتصفح لا يشغّل الفيديو المحمي. حدّث Chrome أو Firefox أو Safari.',
      unavailable: 'تعذّر تحميل الفيديو.',
      offline: 'تعذّر الاتصال بالخادم.',
    },
    fr: {
      failed: 'La vidéo n’a pas pu être lue. Rechargez la page dans un instant.',
      retrying: 'Nouvelle tentative…',
      unsupported: 'Ce navigateur ne lit pas les vidéos protégées. Mettez à jour Chrome, Firefox ou Safari.',
      unavailable: 'La vidéo n’a pas pu être chargée.',
      offline: 'Impossible de joindre le serveur.',
    },
    es: {
      failed: 'No se pudo reproducir el vídeo. Recarga la página en un momento.',
      retrying: 'Reintentando…',
      unsupported: 'Este navegador no reproduce vídeo protegido. Actualiza Chrome, Firefox o Safari.',
      unavailable: 'No se pudo cargar el vídeo.',
      offline: 'No se pudo contactar con el servidor.',
    },
    pt: {
      failed: 'Não foi possível reproduzir o vídeo. Recarregue a página em instantes.',
      retrying: 'Tentando novamente…',
      unsupported: 'Este navegador não reproduz vídeo protegido. Atualize o Chrome, Firefox ou Safari.',
      unavailable: 'Não foi possível carregar o vídeo.',
      offline: 'Não foi possível contactar o servidor.',
    },
    de: {
      failed: 'Das Video konnte nicht abgespielt werden. Laden Sie die Seite gleich neu.',
      retrying: 'Neuer Versuch…',
      unsupported: 'Dieser Browser spielt geschützte Videos nicht ab. Aktualisieren Sie Chrome, Firefox oder Safari.',
      unavailable: 'Das Video konnte nicht geladen werden.',
      offline: 'Der Server ist nicht erreichbar.',
    },
    tr: {
      failed: 'Video oynatılamadı. Birazdan sayfayı yenileyin.',
      retrying: 'Yeniden deneniyor…',
      unsupported: 'Bu tarayıcı korumalı videoyu oynatamıyor. Chrome, Firefox veya Safari’yi güncelleyin.',
      unavailable: 'Video yüklenemedi.',
      offline: 'Sunucuya ulaşılamadı.',
    },
  };

  function pickMessages(options) {
    const wanted = [options.lang, document.documentElement.lang].concat(navigator.languages || [navigator.language]);
    for (let i = 0; i < wanted.length; i++) {
      const code = String(wanted[i] || '').toLowerCase().split('-')[0];
      if (MESSAGES[code]) return Object.assign({}, MESSAGES.en, MESSAGES[code], options.messages || {});
    }
    return Object.assign({}, MESSAGES.en, options.messages || {});
  }

  let hlsPromise = null;

  function loadHlsJs(src, integrity) {
    if (global.Hls) return Promise.resolve();
    if (!hlsPromise) {
      hlsPromise = new Promise(function (resolve, reject) {
        const tag = document.createElement('script');
        tag.src = src;
        if (integrity) {
          tag.integrity = integrity;
          tag.crossOrigin = 'anonymous';
        }
        tag.onload = resolve;
        tag.onerror = function (error) { hlsPromise = null; reject(error); };
        document.head.appendChild(tag);
      });
    }
    return hlsPromise;
  }

  // ---- watermark ---------------------------------------------------------

  let styleAdded = false;

  function addStyle() {
    if (styleAdded) return;
    styleAdded = true;
    const style = document.createElement('style');
    style.textContent =
      '.vidlock-frame{position:relative;max-width:100%}' +
      '.vidlock-frame:fullscreen,.vidlock-frame:-webkit-full-screen{width:100%;height:100%;background:#000}' +
      '.vidlock-frame:fullscreen>video,.vidlock-frame:-webkit-full-screen>video{width:100%;height:100%}' +
      '.vidlock-mark{position:absolute;z-index:2147483647;pointer-events:none;user-select:none;' +
      '-webkit-user-select:none;white-space:nowrap;font:600 clamp(11px,2.2vw,20px)/1.2 system-ui,sans-serif;' +
      'color:#fff;text-shadow:0 0 3px #000,0 0 1px #000;transition:top 1.2s ease,left 1.2s ease}';
    document.head.appendChild(style);
  }

  function Watermark(video, settings) {
    addStyle();
    let frame = video.parentNode;
    let madeFrame = false;
    if (!frame || !frame.classList || !frame.classList.contains('vidlock-frame')) {
      frame = document.createElement('div');
      frame.className = 'vidlock-frame';
      frame.style.display = getComputedStyle(video).display === 'block' ? 'block' : 'inline-block';
      video.parentNode.insertBefore(frame, video);
      frame.appendChild(video);
      madeFrame = true;
    }
    const mark = document.createElement('div');
    mark.className = 'vidlock-mark';
    mark.setAttribute('aria-hidden', 'true');
    mark.style.opacity = String(settings.opacity != null ? settings.opacity : 0.4);
    let text = settings.text;
    mark.textContent = text;
    frame.appendChild(mark);

    function move() {
      const w = Math.max(0, frame.clientWidth - mark.offsetWidth - 8);
      const h = Math.max(0, frame.clientHeight - mark.offsetHeight - 8);
      mark.style.left = Math.round(4 + Math.random() * w) + 'px';
      mark.style.top = Math.round(4 + Math.random() * h) + 'px';
    }
    move();
    const timer = setInterval(move, (settings.interval || 8) * 1000);

    // Put it back if someone deletes or edits it from the console.
    const guard = new MutationObserver(function () {
      if (mark.parentNode !== frame) frame.appendChild(mark);
      if (mark.textContent !== text) mark.textContent = text;
      if (mark.style.display === 'none' || mark.style.visibility === 'hidden') {
        mark.style.display = '';
        mark.style.visibility = '';
      }
    });
    guard.observe(frame, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['style', 'class'] });

    // A fullscreen <video> hides everything drawn over it, so fullscreen the
    // frame instead. The browser's own "exit" and Esc still work.
    let frameWasFull = false;
    function fullscreenElement() { return document.fullscreenElement || document.webkitFullscreenElement; }
    function exitFullscreen() {
      return (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    }
    function onFullscreen() {
      const el = fullscreenElement();
      if (el === video) {
        Promise.resolve(exitFullscreen()).then(function () {
          if (frameWasFull) {
            // The video's own button, pressed while the frame was fullscreen: leave.
            if (fullscreenElement()) exitFullscreen();
            frameWasFull = false;
            return;
          }
          const request = frame.requestFullscreen || frame.webkitRequestFullscreen;
          if (request) request.call(frame);
        }).catch(function () {});
        return;
      }
      frameWasFull = el === frame;
      setTimeout(move, 50);
    }
    document.addEventListener('fullscreenchange', onFullscreen);
    document.addEventListener('webkitfullscreenchange', onFullscreen);
    global.addEventListener('resize', move);

    return {
      setText: function (value) { text = value; mark.textContent = value; },
      remove: function () {
        clearInterval(timer);
        guard.disconnect();
        document.removeEventListener('fullscreenchange', onFullscreen);
        document.removeEventListener('webkitfullscreenchange', onFullscreen);
        global.removeEventListener('resize', move);
        mark.remove();
        if (madeFrame && frame.parentNode) {
          frame.parentNode.insertBefore(video, frame);
          frame.remove();
        }
      },
    };
  }

  // ---- player ------------------------------------------------------------

  function attach(video, options) {
    options = options || {};
    const endpoint = options.endpoint;
    const hlsSrc = options.hlsSrc || DEFAULT_HLS || 'https://cdn.jsdelivr.net/npm/hls.js@1.7.3/dist/hls.light.min.js';
    const hlsIntegrity = options.hlsSrc ? options.hlsIntegrity || '' : DEFAULT_INTEGRITY;
    const onStatus = options.onStatus || function () {};
    const text = pickMessages(options);
    const headers = Object.assign({ 'X-Requested-With': 'XMLHttpRequest' }, options.headers || {});
    let renewTimer = null;
    let renewDue = false;
    let hls = null;
    let native = false;
    let fresh = {};
    let fatalErrors = 0;
    let recovering = false;
    let watermark = null;
    let destroyed = false;

    function resumeAt(position, wasPlaying) {
      if (position <= 0) {
        if (wasPlaying) video.play().catch(function () {});
        return;
      }
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

    function showWatermark(data) {
      if (options.watermark === false) return;
      const custom = typeof options.watermark === 'object' && options.watermark ? options.watermark : {};
      const value = custom.text || data.watermark;
      if (!value) return;
      if (watermark) watermark.setText(value);
      else watermark = Watermark(video, Object.assign({}, custom, { text: value }));
    }

    async function attachSource(data, position, wasPlaying) {
      if (data.format !== 'hls') {
        dropHls();
        native = false;
        video.src = data.url;
        resumeAt(position, wasPlaying);
        return true;
      }
      fresh = { media: data.media_url, key: data.key_url };
      if (hls || native) return true;
      await loadHlsJs(hlsSrc, hlsIntegrity).catch(function () {});
      if (global.Hls && global.Hls.isSupported()) {
        const keyPath = new URL(data.key_url, location.href).pathname;
        hls = new global.Hls(Object.assign({}, options.hlsConfig || {}, {
          startPosition: position > 0 ? position : -1,
          xhrSetup: function (xhr, url) {
            const target = new URL(url, location.href);
            if (target.pathname === keyPath && fresh.key) {
              xhr.open('GET', fresh.key, true);
            } else if (target.origin !== location.origin && fresh.media) {
              xhr.open('GET', fresh.media, true);
            }
          },
        }));
        hls.on(global.Hls.Events.ERROR, function (event, detail) {
          if (!detail.fatal) return;
          // A stream that keeps failing (storage down, CORS missing) must not
          // turn into a request loop.
          if (++fatalErrors > 3) {
            dropHls();
            onStatus(text.failed);
            return;
          }
          setTimeout(function () { fatalErrors = Math.max(0, fatalErrors - 1); }, 120 * 1000);
          const at = video.currentTime;
          const playing = !video.paused;
          dropHls();
          onStatus(text.retrying);
          load({ resume: true, at: at, playing: playing });
        });
        if (options.onHls) options.onHls(hls);
        hls.loadSource(data.url);
        hls.attachMedia(video);
        if (wasPlaying) {
          hls.once(global.Hls.Events.MANIFEST_PARSED, function () { video.play().catch(function () {}); });
        }
        return true;
      }
      if (video.canPlayType('application/vnd.apple.mpegurl')) {
        native = true;
        video.src = data.url;
        resumeAt(position, wasPlaying);
        return true;
      }
      onStatus(text.unsupported);
      return false;
    }

    function scheduleRenewal(seconds) {
      clearTimeout(renewTimer);
      renewDue = false;
      const lead = Math.max(60, Math.min(300, seconds * 0.1));
      renewTimer = setTimeout(function () {
        // Nobody is watching: renew when they come back instead of every few
        // minutes for a tab left open all day.
        if (video.paused && document.hidden) renewDue = true;
        else load({ resume: true });
      }, Math.max(10, seconds - lead) * 1000);
    }

    async function load(opts) {
      if (destroyed) return;
      opts = opts || {};
      const resume = !!opts.resume;
      try {
        const response = await fetch(endpoint, { credentials: 'same-origin', headers: headers });
        const data = await response.json().catch(function () { return {}; });
        if (destroyed) return;
        if (!response.ok || !data.url) {
          onStatus(data.error || text.unavailable);
          return;
        }
        const position = opts.at != null ? opts.at : (resume ? video.currentTime : 0);
        const wasPlaying = opts.playing != null ? opts.playing : (resume && !video.paused);
        if (!(await attachSource(data, position, wasPlaying))) return;
        showWatermark(data);
        onStatus(null);
        if (data.expires_in) scheduleRenewal(data.expires_in);
      } catch (error) {
        onStatus(text.offline);
      }
    }

    function renewIfDue() {
      if (renewDue) {
        renewDue = false;
        load({ resume: true });
      }
    }

    // An expired MP4 URL surfaces here (a laptop waking from sleep, say), and
    // so does a native HLS player that lost its way. hls.js reports its own
    // failures above.
    function onError() {
      if (recovering || hls) return;
      recovering = true;
      native = false;
      load({ resume: true }).finally(function () {
        setTimeout(function () { recovering = false; }, 5000);
      });
    }

    function onVisible() { if (!document.hidden) renewIfDue(); }

    video.addEventListener('error', onError);
    video.addEventListener('play', renewIfDue);
    document.addEventListener('visibilitychange', onVisible);

    load();
    return {
      reload: function () { return load({ resume: true }); },
      destroy: function () {
        destroyed = true;
        clearTimeout(renewTimer);
        video.removeEventListener('error', onError);
        video.removeEventListener('play', renewIfDue);
        document.removeEventListener('visibilitychange', onVisible);
        dropHls();
        if (native) {
          video.removeAttribute('src');
          video.load();
        }
        if (watermark) watermark.remove();
      },
    };
  }

  global.VidLock = { attach: attach, messages: MESSAGES };
})(window);
