import shutil

from vidlock.storage import Storage

#: key -> local path (downloads) or bytes (uploads); tests reset it.
OBJECTS = {}
DELETED = []
#: (key, ttl) for every signed URL handed out.
SIGNED = []


class MemoryStorage(Storage):
    def download(self, key, path):
        shutil.copyfile(OBJECTS[key], path)

    def upload(self, path, key, content_type):
        with open(path, 'rb') as fh:
            OBJECTS[key] = (fh.read(), content_type)

    def signed_url(self, key, ttl):
        SIGNED.append((key, ttl))
        return f'https://bucket.example/{key}?sig=1'

    def delete(self, key):
        DELETED.append(key)
        OBJECTS.pop(key, None)
        return True


#: Where LiveStorage's "bucket" is served; the browser test sets it to the
#: live server on another origin (127.0.0.1 vs localhost) to exercise CORS.
BASE = {'url': ''}


class LiveStorage(MemoryStorage):
    def signed_url(self, key, ttl):
        SIGNED.append((key, ttl))
        return f'{BASE["url"]}/bucket/{key}?sig=1'
