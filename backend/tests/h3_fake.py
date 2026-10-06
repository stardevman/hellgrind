"""Sahte MiniMax H3 sunucusu (SGLang'in /v1/videos ucu): httpx MockTransport. Hiçbir ağ isteği yapmaz.

İstekleri kaydeder; iş, `polls_to_finish` sorgudan sonra tamamlanır ve `inference_time_s` bildirir. İstenirse gönderim,
iş ya da indirme hata verir.
"""
import json

import httpx


class FakeH3:
    def __init__(self, video: bytes, inference_time_s: float | None = 42.0):
        self.video = video
        self.inference_time_s = inference_time_s
        self.polls_to_finish = 2
        self.requests: list[httpx.Request] = []
        self.jobs: dict[str, dict] = {}
        self.submit_errors: list[int] = []        # sıradaki gönderimlerin HTTP durumları
        self.failing_jobs = 0                     # sıradaki bu kadar iş "failed" biter
        self.content_errors: list = []            # sıradaki indirmeler: HTTP durumu ya da fırlatılacak hata
        self.never_finish = False

    # ---------- kayıtlar ----------

    @property
    def submits(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.method == "POST" and r.url.path.endswith("/v1/videos")]

    @property
    def hosts(self) -> list[str]:
        return [f"{r.url.scheme}://{r.url.host}" for r in self.requests if r.method == "POST"]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ---------- sunucu ----------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "POST" and path == "/v1/videos":
            if self.submit_errors:
                return httpx.Response(self.submit_errors.pop(0), json={"detail": "istek reddedildi"})
            job_id = f"video_{len(self.jobs) + 1}"
            failed = self.failing_jobs > 0
            self.failing_jobs -= int(failed)
            self.jobs[job_id] = {"polls": 0, "failed": failed}
            return httpx.Response(200, json={"id": job_id, "object": "video", "status": "queued", "progress": 0})
        if request.method == "GET" and path.endswith("/content"):
            if self.content_errors:
                error = self.content_errors.pop(0)
                if isinstance(error, BaseException):
                    raise error
                return httpx.Response(error)
            return httpx.Response(200, content=self.video, headers={"content-type": "video/mp4"})
        if request.method == "GET" and path.startswith("/v1/videos/"):
            job_id = path.rsplit("/", 1)[1]
            job = self.jobs[job_id]
            job["polls"] += 1
            if job["failed"]:
                return httpx.Response(200, json={"id": job_id, "status": "failed", "error": {"message": "CUDA out of memory"}})
            if self.never_finish or job["polls"] < self.polls_to_finish:
                return httpx.Response(200, json={"id": job_id, "status": "queued", "progress": 0})
            done = {"id": job_id, "status": "completed", "progress": 100, "completed_at": 1}
            if self.inference_time_s is not None:
                done["inference_time_s"] = self.inference_time_s
            return httpx.Response(200, json=done)
        return httpx.Response(404)
