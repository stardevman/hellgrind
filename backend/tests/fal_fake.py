"""Sahte fal_client: çağrıları kaydeder, uygulamaya göre yanıt verir ya da hata fırlatır. Hiçbir ağ isteği yapmaz."""
import types

from app.live import fal_video


class HTTPError(Exception):
    """fal-client'ın FalClientHTTPError'u gibi: status_code taşır."""

    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class FakeFal:
    """Sahte fal_client modülü: çağrıları kaydeder, uygulamaya göre yanıt verir ya da hata fırlatır."""

    def __init__(self):
        self.calls, self.uploads, self.keys, self.behaviour = [], [], [], {}
        self.counter = 0

    def responder(self, application, arguments):
        if application in self.behaviour:
            return self.behaviour[application](arguments)
        if application == fal_video.FLUX:
            return {"images": [{"url": "https://fal.test/frame.jpg"}]}
        if application == fal_video.LIPSYNC:
            return {"video": {"url": "https://fal.test/synced.mp4"}}
        return {"video": {"url": "https://fal.test/clip.mp4"}, "seed": arguments.get("seed")}

    def module(self):
        fake = self

        class Handle:
            def __init__(self, request_id, result):
                self.request_id, self._result = request_id, result

            def get(self):
                if isinstance(self._result, BaseException):
                    raise self._result
                return self._result

        class SyncClient:
            def __init__(self, key=None, default_timeout=120.0):
                fake.keys.append(key)

            def submit(self, application, arguments, **kw):
                fake.counter += 1
                fake.calls.append((application, arguments, kw))
                try:
                    result = fake.responder(application, arguments)
                except BaseException as e:                       # noqa: BLE001 — hata, handle.get()'te fırlatılır
                    result = e
                return Handle(f"req-{fake.counter}", result)

            def upload_file(self, path):
                fake.uploads.append(str(path))
                return f"https://fal.test/upload/{len(fake.uploads)}"

        mod = types.ModuleType("fal_client")
        mod.SyncClient = SyncClient
        return mod

    def apps(self):
        return [a for a, _, _ in self.calls]
