"""Object storage: where the source is fetched from and the sealed file goes.

Any class with these five methods works — set ``VIDLOCK['STORAGE']`` to
its dotted path. The default speaks the S3 API through boto3, which covers
AWS S3, Cloudflare R2 (free egress — the reason this project exists), MinIO
and Backblaze B2.

The bucket must be **private**. The sealed file is useless without its key,
but a public bucket would still hand out every source MP4 uploaded before
sealing. For hls.js to read byte ranges from it, the bucket's CORS must
allow GET with the ``Range`` header and expose ``Content-Range`` — see the
README.
"""

from vidlock import conf


class Storage:
    def download(self, key, path):
        raise NotImplementedError

    def upload(self, path, key, content_type):
        raise NotImplementedError

    def signed_url(self, key, ttl):
        raise NotImplementedError

    def delete(self, key):
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

    def signed_url(self, key, ttl):
        return self.client.generate_presigned_url(
            'get_object', Params={'Bucket': self.bucket, 'Key': key}, ExpiresIn=ttl
        )

    def delete(self, key):
        if not key:
            return False
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False


def default_storage():
    cls = conf.load('STORAGE') or S3Storage
    return cls()
