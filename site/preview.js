
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
