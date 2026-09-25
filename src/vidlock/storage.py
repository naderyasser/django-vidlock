"""Object storage: where the source is fetched from and the sealed file goes.

Any class with these four methods works — set ``VIDLOCK['STORAGE']`` to
its dotted path. Two come with vidlock:

* ``S3Storage`` (the default) speaks the S3 API through boto3: AWS S3,
  Cloudflare R2 (free egress — the reason this project exists), MinIO,
  Backblaze B2, Wasabi, DigitalOcean Spaces, Scaleway…
* ``DjangoStorage`` wraps any Django storage backend you already use, such
  as django-storages' Google Cloud Storage or Azure Blob backends.

The bucket must be **private**. The sealed file is useless without its key,
but a public bucket would still hand out every source MP4 uploaded before
sealing. For hls.js to read byte ranges from it, the bucket's CORS must
allow GET with the ``Range`` header and expose ``Content-Range`` — see the
README.
"""

from __future__ import annotations

import shutil

from vidlock import conf

#: The longest a presigned S3 URL (SigV4) may live.
MAX_SIGNED_TTL = 7 * 24 * 3600


class Storage:
    def download(self, key: str, path: str) -> None:
        """Copy the object ``key`` to the local file ``path``."""
        raise NotImplementedError

    def upload(self, path: str, key: str, content_type: str) -> str | None:
        """Store the local file ``path`` as ``key``. May return the name it was
        actually stored under, when the storage picks its own."""
        raise NotImplementedError

    def signed_url(self, key: str, ttl: int) -> str:
        """A URL that reads ``key`` (with Range requests) for ``ttl`` seconds."""
        raise NotImplementedError

    def delete(self, key: str) -> bool:
        """Remove an object. Must not raise: deleting a video should never fail
        because storage is briefly unreachable. Return whether it worked."""
        raise NotImplementedError


class S3Storage(Storage):
    def __init__(
        self, bucket=None, endpoint_url=None, access_key_id=None, secret_access_key=None, region=None
    ):
        self.bucket = bucket or conf.get('S3_BUCKET')
        self._options = {
            'endpoint_url': endpoint_url or conf.get('S3_ENDPOINT_URL'),
            'aws_access_key_id': access_key_id or conf.get('S3_ACCESS_KEY_ID'),
            'aws_secret_access_key': secret_access_key or conf.get('S3_SECRET_ACCESS_KEY'),
            'region_name': region or conf.get('S3_REGION'),
        }
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                's3',
                # Path addressing: R2 and MinIO serve no virtual-host subdomains.
                config=Config(s3={'addressing_style': 'path'}, signature_version='s3v4'),
                **self._options,
            )
        return self._client

    def download(self, key, path):
        self.client.download_file(self.bucket, key, path)

    def upload(self, path, key, content_type):
        self.client.upload_file(path, self.bucket, key, ExtraArgs={'ContentType': content_type})
        return key

    def signed_url(self, key, ttl):
        return self.client.generate_presigned_url(
            'get_object', Params={'Bucket': self.bucket, 'Key': key}, ExpiresIn=min(int(ttl), MAX_SIGNED_TTL)
        )

    def delete(self, key):
        if not key:
            return False
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False


class DjangoStorage(Storage):
    """Any Django storage backend, by its alias in ``settings.STORAGES``
    (``VIDLOCK['DJANGO_STORAGE']``, default ``'default'``).

    The backend must be private and able to sign URLs that expire —
    django-storages' S3, Google Cloud and Azure backends do, given
    ``querystring_auth``/``default_acl`` settings that keep objects private.
    ``FileSystemStorage`` serves public URLs and is refused.
    """

    def __init__(self, alias: str | None = None, backend=None):
        self._alias = alias or conf.get('DJANGO_STORAGE')
        self._backend = backend

    @property
    def backend(self):
        if self._backend is None:
            from django.core.files.storage import storages

            self._backend = storages[self._alias]
        return self._backend

    def download(self, key, path):
        with self.backend.open(key, 'rb') as src, open(path, 'wb') as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)

    def upload(self, path, key, content_type):
        from django.core.files import File

        with open(path, 'rb') as fh:
            upload = File(fh, name=key)
            upload.content_type = content_type
            return self.backend.save(key, upload)

    def signed_url(self, key, ttl):
        from django.core.files.storage import FileSystemStorage

        if isinstance(self.backend, FileSystemStorage):
            from django.core.exceptions import ImproperlyConfigured

            raise ImproperlyConfigured(
                'vidlock.storage.DjangoStorage needs a private backend that signs URLs; '
                'FileSystemStorage would publish every source upload.'
            )
        try:
            return self.backend.url(key, expire=min(int(ttl), MAX_SIGNED_TTL))
        except TypeError:
            # Backends whose url() takes no expiry sign with their own setting.
            return self.backend.url(key)

    def delete(self, key):
        if not key:
            return False
        try:
            self.backend.delete(key)
            return True
        except Exception:
            return False


def default_storage():
    cls = conf.load('STORAGE') or S3Storage
    return cls()
