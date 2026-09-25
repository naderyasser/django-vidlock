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
 * Keys never cross the network in the clear: the page makes an ECDH key
 * pair (its private half never leaves WebCrypto) and the server seals each
 * key to it. A heartbeat keeps the device's stream lease, tells the server
 * the video is really playing, and reports MediaSource functions replaced
 * by an extension. When the account starts playing on another device past
 * MAX_STREAMS, this one stops with a message (and `onEvicted`).
 *
 * When the playback endpoint returns a `watermark`, it is drawn over the
 * video with the time, moves every few seconds, and a faint copy covers the
 * whole frame, in fullscreen as well. iOS's own fullscreen player draws
 * nothing over itself, so it is refused while a watermark is on.
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

  // ---- tamper tripwire ---------------------------------------------------
  // "Save the stream" extensions replace MediaSource functions to copy what
  // the player appends. Only a signal (an extension that loads first can hide
  // itself), so it is reported, never acted on here.

  const fnToString = Function.prototype.toString;
  function isNative(fn) {
    try { return typeof fn === 'function' && /\[native code\]/.test(fnToString.call(fn)); } catch (e) { return false; }
  }
  function watched() {
    const out = [];
    [global.SourceBuffer, global.ManagedSourceBuffer].forEach(function (cls) {
      if (cls && cls.prototype) out.push(cls.prototype.appendBuffer);
    });
    [global.MediaSource, global.ManagedMediaSource].forEach(function (cls) {
      if (cls && cls.prototype) out.push(cls.prototype.addSourceBuffer);
    });
    return out;
  }
  const ORIGINAL = watched();
  function tampered() {
    const now = watched();
    return !isNative(fnToString) || now.some(function (fn, i) { return fn !== ORIGINAL[i] || !isNative(fn); });
  }

  // ---- key exchange ------------------------------------------------------

  const subtle = global.crypto && global.crypto.subtle;
  const encoder = new TextEncoder();

  function b64url(buffer) {
    let text = '';
    new Uint8Array(buffer).forEach(function (b) { text += String.fromCharCode(b); });
    return btoa(text).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  // Resolves to {share, open(ArrayBuffer) -> Promise<ArrayBuffer>}, or null
  // where WebCrypto is missing (plain http off localhost): keys then come raw.
  function makeKeyExchange() {
    if (!subtle) return Promise.resolve(null);
    return subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits'])
      .then(function (pair) {
        return subtle.exportKey('raw', pair.publicKey).then(function (raw) {
          return {
            share: b64url(raw),
            open: function (sealed) {
              const bytes = new Uint8Array(sealed);
              if (bytes.length !== 65 + 12 + 32) return Promise.reject(new Error('not a sealed key'));
              return subtle.importKey('raw', bytes.slice(0, 65), { name: 'ECDH', namedCurve: 'P-256' }, false, [])
                .then(function (server) {
                  return subtle.deriveBits({ name: 'ECDH', public: server }, pair.privateKey, 256);
                })
                .then(function (bits) { return subtle.importKey('raw', bits, 'HKDF', false, ['deriveKey']); })
                .then(function (secret) {
                  return subtle.deriveKey(
                    { name: 'HKDF', hash: 'SHA-256', salt: encoder.encode('vidlock.wrap.v1'), info: encoder.encode('vidlock content key') },
                    secret, { name: 'AES-GCM', length: 128 }, false, ['decrypt']);
                })
                .then(function (key) {
                  return subtle.decrypt({ name: 'AES-GCM', iv: bytes.slice(65, 77) }, key, bytes.slice(77));
                });
            },
          };
        });
      })
      .catch(function () { return null; });
  }

  const MESSAGES = {
    en: {
      failed: 'The video could not be played. Reload the page in a moment.',
      retrying: 'Retrying…',
      unsupported: 'This browser cannot play protected video. Update Chrome, Firefox or Safari.',
      unavailable: 'The video could not be loaded.',
      offline: 'Could not reach the server.',
      elsewhere: 'This account is now watching on another device.',
      blocked: 'Playback is paused on this account. Please contact support.',
    },
    ar: {
      failed: 'تعذّر تشغيل الفيديو. أعد تحميل الصفحة بعد قليل.',
      retrying: 'جارٍ إعادة المحاولة…',
      unsupported: 'هذا المتصفح لا يشغّل الفيديو المحمي. حدّث Chrome أو Firefox أو Safari.',
      unavailable: 'تعذّر تحميل الفيديو.',
      offline: 'تعذّر الاتصال بالخادم.',
      elsewhere: 'هذا الحساب يشاهد الآن على جهاز آخر.',
      blocked: 'تم إيقاف التشغيل مؤقتًا على هذا الحساب. يرجى التواصل مع الدعم.',
    },
    fr: {
      failed: 'La vidéo n’a pas pu être lue. Rechargez la page dans un instant.',
      retrying: 'Nouvelle tentative…',
      unsupported: 'Ce navigateur ne lit pas les vidéos protégées. Mettez à jour Chrome, Firefox ou Safari.',
      unavailable: 'La vidéo n’a pas pu être chargée.',
      offline: 'Impossible de joindre le serveur.',
      elsewhere: 'Ce compte regarde maintenant sur un autre appareil.',
      blocked: 'La lecture est suspendue sur ce compte. Contactez le support.',
    },
    es: {
      failed: 'No se pudo reproducir el vídeo. Recarga la página en un momento.',
      retrying: 'Reintentando…',
      unsupported: 'Este navegador no reproduce vídeo protegido. Actualiza Chrome, Firefox o Safari.',
      unavailable: 'No se pudo cargar el vídeo.',
      offline: 'No se pudo contactar con el servidor.',
      elsewhere: 'Esta cuenta está viendo ahora en otro dispositivo.',
      blocked: 'La reproducción está pausada en esta cuenta. Contacta con soporte.',
    },
    pt: {
      failed: 'Não foi possível reproduzir o vídeo. Recarregue a página em instantes.',
      retrying: 'Tentando novamente…',
      unsupported: 'Este navegador não reproduz vídeo protegido. Atualize o Chrome, Firefox ou Safari.',
      unavailable: 'Não foi possível carregar o vídeo.',
      offline: 'Não foi possível contactar o servidor.',
      elsewhere: 'Esta conta está assistindo agora em outro dispositivo.',
      blocked: 'A reprodução está pausada nesta conta. Entre em contato com o suporte.',
    },
    de: {
      failed: 'Das Video konnte nicht abgespielt werden. Laden Sie die Seite gleich neu.',
      retrying: 'Neuer Versuch…',
      unsupported: 'Dieser Browser spielt geschützte Videos nicht ab. Aktualisieren Sie Chrome, Firefox oder Safari.',
      unavailable: 'Das Video konnte nicht geladen werden.',
      offline: 'Der Server ist nicht erreichbar.',
      elsewhere: 'Dieses Konto schaut jetzt auf einem anderen Gerät.',
      blocked: 'Die Wiedergabe ist für dieses Konto pausiert. Bitte wenden Sie sich an den Support.',
    },
    tr: {
      failed: 'Video oynatılamadı. Birazdan sayfayı yenileyin.',
      retrying: 'Yeniden deneniyor…',
      unsupported: 'Bu tarayıcı korumalı videoyu oynatamıyor. Chrome, Firefox veya Safari’yi güncelleyin.',
      unavailable: 'Video yüklenemedi.',
      offline: 'Sunucuya ulaşılamadı.',
      elsewhere: 'Bu hesap şu anda başka bir cihazda izliyor.',
      blocked: 'Bu hesapta oynatma duraklatıldı. Lütfen destekle iletişime geçin.',
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
      'color:#fff;text-shadow:0 0 3px #000,0 0 1px #000;transition:top 1.2s ease,left 1.2s ease}' +
      '.vidlock-pattern{position:absolute;inset:0;z-index:2147483646;pointer-events:none;background-repeat:repeat}';
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
    const opacity = String(settings.opacity != null ? settings.opacity : 0.4);
    const patternOpacity = String(settings.patternOpacity != null ? settings.patternOpacity : 0.08);
    const mark = document.createElement('div');
    mark.className = 'vidlock-mark';
    mark.setAttribute('aria-hidden', 'true');
    mark.style.opacity = opacity;
    // A faint copy tiled over the whole frame: cropping the moving one out of
    // a recording leaves this behind.
    const pattern = document.createElement('div');
    pattern.className = 'vidlock-pattern';
    pattern.setAttribute('aria-hidden', 'true');
    pattern.style.opacity = patternOpacity;
    let text = settings.text;
    let shown = '';

    function pad(n) { return (n < 10 ? '0' : '') + n; }
    function stamp() {
      const d = new Date();
      return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
    }
    function xml(value) {
      return value.replace(/[<>&'"]/g, function (c) { return '&#' + c.charCodeAt(0) + ';'; });
    }
    function paint() {
      shown = settings.clock === false ? text : text + ' · ' + stamp();
      mark.textContent = shown;
      const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="160">' +
        '<text x="160" y="80" text-anchor="middle" transform="rotate(-24 160 80)" font-family="sans-serif" ' +
        'font-size="15" fill="#fff" stroke="#000" stroke-width="0.4">' + xml(text) + '</text></svg>';
      pattern.style.backgroundImage = 'url("data:image/svg+xml,' + encodeURIComponent(svg) + '")';
    }
    paint();
    if (settings.pattern !== false) frame.appendChild(pattern);
    frame.appendChild(mark);

    function move() {
      if (mark.textContent.slice(-5) !== stamp().slice(-5) && settings.clock !== false) paint();
      const w = Math.max(0, frame.clientWidth - mark.offsetWidth - 8);
      const h = Math.max(0, frame.clientHeight - mark.offsetHeight - 8);
      mark.style.left = Math.round(4 + Math.random() * w) + 'px';
      mark.style.top = Math.round(4 + Math.random() * h) + 'px';
    }
    move();
    const timer = setInterval(move, (settings.interval || 8) * 1000);

    // Put it back if someone deletes, hides or edits it from the console.
    function restore(el, wanted) {
      if (el.style.display === 'none' || el.style.visibility === 'hidden') {
        el.style.display = '';
        el.style.visibility = '';
      }
      if (el.style.opacity !== wanted) el.style.opacity = wanted;
    }
    const guard = new MutationObserver(function () {
      if (mark.parentNode !== frame) frame.appendChild(mark);
      if (settings.pattern !== false && pattern.parentNode !== frame) frame.insertBefore(pattern, mark);
      if (mark.textContent !== shown) mark.textContent = shown;
      restore(mark, opacity);
      restore(pattern, patternOpacity);
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

    // iPhone's own fullscreen player shows the bare video, nothing over it.
    function onNativeFullscreen() {
      if (settings.allowNativeFullscreen) return;
      setTimeout(function () { if (video.webkitExitFullscreen) video.webkitExitFullscreen(); }, 0);
      if (settings.onNativeFullscreen) settings.onNativeFullscreen();
    }
    video.addEventListener('webkitbeginfullscreen', onNativeFullscreen);

    return {
      setText: function (value) { text = value; paint(); },
      remove: function () {
        clearInterval(timer);
        guard.disconnect();
        document.removeEventListener('fullscreenchange', onFullscreen);
        document.removeEventListener('webkitfullscreenchange', onFullscreen);
        global.removeEventListener('resize', move);
        video.removeEventListener('webkitbeginfullscreen', onNativeFullscreen);
        mark.remove();
        pattern.remove();
        if (madeFrame && frame.parentNode) {
          frame.parentNode.insertBefore(video, frame);
          frame.remove();
        }
      },
    };
  }

  // ---- player ------------------------------------------------------------

  // hls.js's loader, but key responses are opened with the page's key
  // exchange before hls.js sees them.
  function keyOpeningLoader(Base, isKey, exchange) {
    return class VidlockLoader extends Base {
      load(context, config, callbacks) {
        if (exchange && isKey(context.url)) {
          const success = callbacks.onSuccess;
          const failure = callbacks.onError;
          callbacks = Object.assign({}, callbacks, {
            onSuccess: function (response, stats, ctx, details) {
              const data = response.data;
              if (data && data.byteLength === 16) { success(response, stats, ctx, details); return; }
              exchange.open(data).then(function (key) {
                response.data = key;
                success(response, stats, ctx, details);
              }, function () {
                failure({ code: 0, text: 'vidlock: the key could not be opened' }, ctx, details, stats);
              });
            },
          });
        }
        return super.load(context, config, callbacks);
      }
    };
  }

  function attach(video, options) {
    options = options || {};
    const endpoint = options.endpoint;
    const hlsSrc = options.hlsSrc || DEFAULT_HLS || 'https://cdn.jsdelivr.net/npm/hls.js@1.7.3/dist/hls.light.min.js';
    const hlsIntegrity = options.hlsSrc ? options.hlsIntegrity || '' : DEFAULT_INTEGRITY;
    const onStatus = options.onStatus || function () {};
    const text = pickMessages(options);
    const headers = Object.assign({ 'X-Requested-With': 'XMLHttpRequest' }, options.headers || {});
    const exchangeReady = makeKeyExchange();
    let renewTimer = null;
    let renewDue = false;
    let beatTimer = null;
    let lastBeatAt = 0;
    let hls = null;
    let native = false;
    let fresh = {};
    let fatalErrors = 0;
    let recovering = false;
    let watermark = null;
    let destroyed = false;
    let stopped = false;

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

    function dropNative() {
      if (native) {
        native = false;
        video.removeAttribute('src');
        video.load();
      }
    }

    // The stream went to another device, or the account was paused: stop,
    // and stay stopped until reload() — which takes the stream back.
    function stop(message) {
      if (stopped) return;
      stopped = true;
      video.pause();
      dropHls();
      dropNative();
      clearTimeout(renewTimer);
      clearInterval(beatTimer);
      beatTimer = null;
      onStatus(message);
      if (options.onEvicted) options.onEvicted(message);
    }

    function beat() {
      if (!fresh.heartbeat || destroyed || stopped) return;
      const moved = Math.abs(video.currentTime - lastBeatAt) > 0.5;
      lastBeatAt = video.currentTime;
      fetch(fresh.heartbeat, {
        method: 'POST',
        credentials: 'same-origin',
        keepalive: true,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ position: video.currentTime, playing: moved && !video.paused, tamper: tampered() }),
      }).then(function (response) {
        if (response.status !== 409 && response.status !== 403) return;
        return response.json().then(function (data) {
          if (data.error === 'elsewhere') stop(data.message || text.elsewhere);
          else if (data.error === 'suspended') stop(data.message || text.blocked);
        });
      }).catch(function () {});
    }

    function startHeartbeat(data) {
      fresh.heartbeat = data.heartbeat_url;
      if (!data.heartbeat_url || beatTimer) return;
      beatTimer = setInterval(beat, Math.max(2, data.heartbeat_interval || 30) * 1000);
    }

    function onPlaying() { setTimeout(beat, 3000); }

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
      fresh.media = data.media_url;
      fresh.key = data.key_url;
      if (hls || native) return true;
      await loadHlsJs(hlsSrc, hlsIntegrity).catch(function () {});
      if (global.Hls && global.Hls.isSupported()) {
        const exchange = data.key_exchange ? await exchangeReady : null;
        const keyPath = new URL(data.key_url, location.href).pathname;
        const isKey = function (url) { return new URL(url, location.href).pathname === keyPath; };
        const base = (options.hlsConfig && options.hlsConfig.loader) || global.Hls.DefaultConfig.loader;
        hls = new global.Hls(Object.assign({}, options.hlsConfig || {}, {
          startPosition: position > 0 ? position : -1,
          loader: keyOpeningLoader(base, isKey, exchange),
          xhrSetup: function (xhr, url) {
            const target = new URL(url, location.href);
            if (isKey(url) && fresh.key) {
              // The newest token, for the same key of a rotated video.
              const next = new URL(fresh.key, location.href);
              const index = target.searchParams.get('k');
              if (index) next.searchParams.set('k', index);
              xhr.open('GET', next.href, true);
              if (exchange) xhr.setRequestHeader('X-Vidlock-Key-Share', exchange.share);
            } else if (target.origin !== location.origin && fresh.media) {
              xhr.open('GET', fresh.media, true);
            }
          },
        }));
        hls.on(global.Hls.Events.ERROR, function (event, detail) {
          const status = (detail.response && detail.response.code) ||
            (detail.networkDetails && detail.networkDetails.status);
          if (status === 409) { stop(text.elsewhere); return; }
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

    // Loads the player's own renewals and recoveries make must never take the
    // stream back from another device; only the viewer's reload() does.
    function endpointFor(resume) {
      return resume ? endpoint + (endpoint.indexOf('?') < 0 ? '?' : '&') + 'vidlock_resume=1' : endpoint;
    }

    async function load(opts) {
      if (destroyed) return;
      opts = opts || {};
      const resume = !!opts.resume;
      if (stopped && !opts.takeover) return;
      try {
        const response = await fetch(endpointFor(resume && !opts.takeover), { credentials: 'same-origin', headers: headers });
        const data = await response.json().catch(function () { return {}; });
        if (destroyed) return;
        if (data.format === 'elsewhere' || data.format === 'blocked') {
          stop(data.error || (data.format === 'blocked' ? text.blocked : text.elsewhere));
          return;
        }
        if (!response.ok || !data.url) {
          onStatus(data.error || text.unavailable);
          return;
        }
        stopped = false;
        const position = opts.at != null ? opts.at : (resume ? video.currentTime : 0);
        const wasPlaying = opts.playing != null ? opts.playing : (resume && !video.paused);
        if (!(await attachSource(data, position, wasPlaying))) return;
        showWatermark(data);
        startHeartbeat(data);
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
      if (recovering || hls || stopped) return;
      recovering = true;
      native = false;
      load({ resume: true }).finally(function () {
        setTimeout(function () { recovering = false; }, 5000);
      });
    }

    function onVisible() { if (!document.hidden) renewIfDue(); }

    video.addEventListener('error', onError);
    video.addEventListener('play', renewIfDue);
    video.addEventListener('playing', onPlaying);
    document.addEventListener('visibilitychange', onVisible);

    load();
    return {
      // Also takes the stream back after it moved to another device.
      reload: function () {
        stopped = false;
        return load({ resume: true, takeover: true, at: video.currentTime, playing: true });
      },
      destroy: function () {
        destroyed = true;
        clearTimeout(renewTimer);
        clearInterval(beatTimer);
        video.removeEventListener('error', onError);
        video.removeEventListener('play', renewIfDue);
        video.removeEventListener('playing', onPlaying);
        document.removeEventListener('visibilitychange', onVisible);
        dropHls();
        dropNative();
        if (watermark) watermark.remove();
      },
    };
  }

  global.VidLock = { attach: attach, messages: MESSAGES };
})(window);
