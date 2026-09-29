/*
 * vidlock direct uploads: the browser sends the video straight to the
 * bucket; Django only signs (vidlock.uploads).
 *
 *   VidLock.upload(file, {endpoint, onProgress}) -> Promise<ticket>
 *
 * Or on a plain Django form, with no script of your own:
 *
 *   <form method="post" data-vidlock-upload="{% url 'vidlock:upload' %}">
 *     {% csrf_token %} ... <input type="file" name="video" accept="video/*">
 *     <progress hidden></progress> <p data-vidlock-upload-error></p> <button>Upload</button>
 *   </form>
 *
 * On submit the file goes to the bucket (the <progress> fills), then the
 * form is posted with a `vidlock_ticket` field in place of the file. Your
 * view: key = vidlock.uploads.finish(request.user, request.POST['vidlock_ticket']).
 */
(function () {
  'use strict';
  const VidLock = (window.VidLock = window.VidLock || {});

  function csrfToken(form) {
    const field = form && form.querySelector('input[name=csrfmiddlewaretoken]');
    if (field) return field.value;
    const cookie = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return cookie ? decodeURIComponent(cookie[1]) : '';
  }

  function put(target, file, onProgress) {
    return new Promise(function (resolve, reject) {
      const xhr = new XMLHttpRequest();
      xhr.open(target.method || 'PUT', target.url);
      Object.keys(target.headers || {}).forEach(function (name) { xhr.setRequestHeader(name, target.headers[name]); });
      xhr.upload.onprogress = function (e) { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
      xhr.onload = function () {
        if (xhr.status >= 200 && xhr.status < 300) resolve();
        else reject(new Error('The upload was refused (' + xhr.status + ').'));
      };
      xhr.onerror = function () { reject(new Error('The upload was interrupted. Check the connection and try again.')); };
      xhr.send(file);
    });
  }

  VidLock.upload = function (file, options) {
    options = options || {};
    return fetch(options.endpoint, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': options.csrfToken || csrfToken(options.form) },
      body: JSON.stringify({ name: file.name, size: file.size }),
    })
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (body) {
          if (!response.ok) throw new Error(body.error || 'The upload could not start (' + response.status + ').');
          return body;
        });
      })
      .then(function (target) {
        return put(target, file, options.onProgress).then(function () { return target.ticket; });
      });
  };

  function attach(form) {
    const input = form.querySelector('input[type=file]');
    if (!input) return;
    const bar = form.querySelector('progress');
    let done = false;
    form.addEventListener('submit', function (event) {
      if (done || !input.files || !input.files.length) return;
      event.preventDefault();
      const buttons = form.querySelectorAll('button, input[type=submit]');
      buttons.forEach(function (b) { b.disabled = true; });
      if (bar) { bar.hidden = false; bar.max = 1; bar.value = 0; }
      const box = form.querySelector('[data-vidlock-upload-error]');
      if (box) box.textContent = '';
      form.dispatchEvent(new CustomEvent('vidlock:upload-start'));
      VidLock.upload(input.files[0], {
        endpoint: form.getAttribute('data-vidlock-upload'),
        form: form,
        onProgress: function (fraction) {
          if (bar) bar.value = fraction;
          form.dispatchEvent(new CustomEvent('vidlock:upload-progress', { detail: fraction }));
        },
      }).then(function (ticket) {
        const field = document.createElement('input');
        field.type = 'hidden'; field.name = 'vidlock_ticket'; field.value = ticket;
        form.appendChild(field);
        input.disabled = true; // the file is in the bucket; do not send it again
        done = true;
        buttons.forEach(function (b) { b.disabled = false; });
        if (form.requestSubmit) form.requestSubmit(); else form.submit();
      }).catch(function (error) {
        buttons.forEach(function (b) { b.disabled = false; });
        if (bar) bar.hidden = true;
        const box = form.querySelector('[data-vidlock-upload-error]');
        if (box) box.textContent = error.message;
        form.dispatchEvent(new CustomEvent('vidlock:upload-error', { detail: error.message }));
      });
    });
  }

  function scan() { document.querySelectorAll('form[data-vidlock-upload]').forEach(attach); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', scan);
  else scan();
})();
