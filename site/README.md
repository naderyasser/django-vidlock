# Static preview of the demo

The example site frozen as static pages by `python manage.py build_site ../site`
(run from `example/`): the real templates, styles and motion, with a plain copy
of the sample lesson in place of the sealed stream. Sign-in, uploads and the
encrypted stream need the Django server; see `example/`.

Deploy on Vercel: import the repository and set **Root Directory** to `site`
(no build command). Any static host works the same way.
