SECRET_KEY = 'tests-only'
USE_TZ = True
INSTALLED_APPS = [
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'vidlock',
    'tests.testapp',
]
MIDDLEWARE = [
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
]
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
ROOT_URLCONF = 'tests.testapp.urls'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
VIDLOCK = {
    'BACKEND': 'tests.testapp.backend.LessonBackend',
    'STORAGE': 'tests.testapp.storage.MemoryStorage',
    'ON_KEY_ABUSE': 'tests.testapp.backend.report',
}
