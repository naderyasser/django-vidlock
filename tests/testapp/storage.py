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


#: key -> content type for every upload target handed out.
TARGETS = {}


def _memory_upload_target(self, key, content_type, ttl):
    TARGETS[key] = content_type
    return {'url': f'https://bucket.example/{key}?put=1', 'headers': {'Content-Type': content_type}}


def _memory_size(self, key):
    found = OBJECTS.get(key)
    return len(found[0]) if found else None


MemoryStorage.upload_target = _memory_upload_target
MemoryStorage.size = _memory_size
