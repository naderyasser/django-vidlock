# Static preview of the demo

The example site frozen as static pages by `python manage.py build_site ../site`
(run from `example/`): the real templates, styles and motion, with a plain copy
of the sample lesson in place of the sealed stream. Sign-in, uploads and the
encrypted stream need the Django server; see `example/`.

Deploy on Vercel: import the repository as it is (the root `vercel.json`
serves this folder, with no build), or set **Root Directory** to `site`. Not
`example/`: Vercel would try to run it as a Django server, which it is not
built for (ffmpeg, a disk, background work). Any static host works too.
