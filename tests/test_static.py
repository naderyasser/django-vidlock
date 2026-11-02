"""The packaged static files survive ManifestStaticFilesStorage, which
rejects a file that names a missing source map."""

from django.core.management import call_command


def test_collectstatic_with_the_manifest_storage(settings, tmp_path):
    settings.STATIC_ROOT = str(tmp_path)
    settings.STORAGES = {
        **settings.STORAGES,
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.ManifestStaticFilesStorage'},
    }
    call_command('collectstatic', interactive=False, verbosity=0)
    assert (tmp_path / 'staticfiles.json').exists()
    assert list(tmp_path.glob('vidlock/vendor/hls.light.min.*.js'))
