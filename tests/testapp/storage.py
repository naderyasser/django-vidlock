import shutil

from vidlock.storage import Storage

#: key -> local path (downloads) or bytes (uploads); tests reset it.
OBJECTS = {}
DELETED = []


class MemoryStorage(Storage):
    def download(self, key, path):
        shutil.copyfile(OBJECTS[key], path)

    def upload(self, path, key, content_type):
        with open(path, 'rb') as fh:
            OBJECTS[key] = (fh.read(), content_type)

    def signed_url(self, key, ttl):
        return f'https://bucket.example/{key}?sig=1'

    def delete(self, key):
        DELETED.append(key)
        OBJECTS.pop(key, None)
        return True
