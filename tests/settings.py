SECRET_KEY = 'tests-only'
USE_TZ = True
DEBUG = False
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.messages',
    'django.contrib.sessions',
    'django.contrib.staticfiles',
    'vidlock',
    'tests.testapp',
]
MIDDLEWARE = [
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
]
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ]
        },
    }
]
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
ROOT_URLCONF = 'tests.testapp.urls'
STATIC_URL = '/static/'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
SILENCED_SYSTEM_CHECKS = ['vidlock.W006', 'vidlock.W007']
VIDLOCK = {
    'BACKEND': 'tests.testapp.backend.LessonBackend',
    'STORAGE': 'tests.testapp.storage.MemoryStorage',
    'ON_KEY_ABUSE': 'tests.testapp.backend.report',
}
