"""Sahte RunPod (httpx MockTransport; ağa çıkılmaz): Serverless iş API'si (/run, /status, /health), REST v2 uç nokta ayarı
(GET/PATCH /v2/serverless/{id}) ve GraphQL bakiye. İş, `polls_to_finish` sorgudan sonra tamamlanır; çıktıyı bizim worker'ın
biçiminde (files: base64 ya da url, boyut, SHA-256, GPU, soğuk başlatma) verir."""
import base64
import hashlib
import json

import httpx

KEY = "Bearer test-runpod_api_key"


class FakeRunpod:
    def __init__(self, video: bytes, image: bytes = b"", endpoint: str = "ep123"):
        self.video, self.image, self.endpoint = video, image, endpoint
        self.balance = 50.0
        self.config = {"id": endpoint, "name": "hellgrind-h3", "dataCenterIds": ["CA-MTL-1"], "networkVolumes": ["vol1"],
                       "gpu": {"pools": ["ADA_32_PRO", "BLACKWELL_96"]},
                       "workers": {"min": 0, "max": 3, "idleTimeout": 60}, "flashboot": "FLASHBOOT"}
        self.health = {"jobs": {"inQueue": 0, "inProgress": 0}, "workers": {"idle": 0, "running": 0}}
        self.polls_to_finish = 2
        self.jobs: dict[str, dict] = {}
        self.runs: list[dict] = []
        self.patches: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.failing = 0                      # sıradaki bu kadar iş FAILED biter
        self.mode = "base64"                  # base64 | url | official (resmî worker-comfyui biçimi)
        self.gpu = "NVIDIA GeForce RTX 5090"
        self.cold_start_s = 42.0              # yalnızca ilk iş
        self.execution_ms = 30_000
        self.tamper = False                   # bildirilen SHA-256 dosyayla uyuşmasın
        self.expired: set[str] = set()        # sonucu silinmiş işler (404)
        self.graphql_error = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _output(self, job: dict) -> dict:
        data = self.image if job["kind"] == "keyframe" else self.video
        name = "hg_00001_.png" if job["kind"] == "keyframe" else "hellgrind/i2v_00001_.mp4"
        sha = hashlib.sha256(data).hexdigest()
        if self.mode == "official":
            return {"images": [{"filename": name.rsplit("/", 1)[-1], "type": "base64",
                                "data": base64.b64encode(data).decode()}]}
        entry = {"filename": name.rsplit("/", 1)[-1], "subfolder": "hellgrind", "size": len(data),
                 "sha256": "0" * 64 if self.tamper else sha}
        if self.mode == "url":
            entry.update(type="url", data=f"https://bucket.test/out/{job['id']}/{entry['filename']}")
        else:
            entry.update(type="base64", data=base64.b64encode(data).decode())
        first = len([j for j in self.jobs.values() if j.get("done")]) == 0
        return {"files": [entry], "gpu": self.gpu, "version": "hellgrind-h3-worker 1",
                "timing": {"cold_start_s": self.cold_start_s if first else 0.0, "execution_s": self.execution_ms / 1000},
                "comfy": {"prompt_id": f"cp-{job['id']}"}}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("Authorization") != KEY and request.url.host != "bucket.test":
            return httpx.Response(401, json={"error": "unauthorized"})
        path, host = request.url.path, request.url.host
        if host == "bucket.test":
            job_id = path.split("/")[2]
            data = self.image if self.jobs[job_id]["kind"] == "keyframe" else self.video
            return httpx.Response(200, content=data)
        if host == "api.runpod.io" and path == "/graphql":
            if self.graphql_error:
                return httpx.Response(200, json={"errors": [{"message": "deprecated"}]})
            return httpx.Response(200, json={"data": {"myself": {"clientBalance": self.balance, "currentSpendPerHr": 0.0,
                                                                 "spendLimit": 80}}})
        if host == "api.runpod.io" and path == f"/v2/serverless/{self.endpoint}":
            if request.method == "PATCH":
                body = json.loads(request.content)
                self.patches.append(body)
                self.config.update({k: v for k, v in body.items() if k != "workers"})
                self.config["workers"] = {**self.config["workers"], **body.get("workers", {})}
            return httpx.Response(200, json=self.config)
        if host != "api.runpod.ai":
            return httpx.Response(404)
        prefix = f"/v2/{self.endpoint}"
        if not path.startswith(prefix):
            return httpx.Response(404, json={"error": "endpoint not found"})
        rest = path[len(prefix):]
        if rest == "/health":
            return httpx.Response(200, json=self.health)
        if rest == "/run" and request.method == "POST":
            body = json.loads(request.content)
            self.runs.append(body)
            job_id = f"job-{len(self.runs)}"
            failed = self.failing > 0
            self.failing -= int(failed)
            self.jobs[job_id] = {"id": job_id, "polls": 0, "failed": failed,
                                 "kind": body["input"]["hellgrind"]["kind"]}
            return httpx.Response(200, json={"id": job_id, "status": "IN_QUEUE"})
        if rest.startswith("/status/"):
            job_id = rest.rsplit("/", 1)[1]
            if job_id in self.expired or job_id not in self.jobs:
                return httpx.Response(404, json={"error": "job not found"})
            job = self.jobs[job_id]
            job["polls"] += 1
            if job["failed"]:
                return httpx.Response(200, json={"id": job_id, "status": "FAILED", "error": "CUDA out of memory"})
            if job["polls"] < self.polls_to_finish:
                return httpx.Response(200, json={"id": job_id, "status": "IN_QUEUE" if job["polls"] == 1 else "IN_PROGRESS"})
            if "output" not in job:
                job["output"] = self._output(job)
                job["done"] = True
            return httpx.Response(200, json={"id": job_id, "status": "COMPLETED", "delayTime": 61_000,
                                             "executionTime": self.execution_ms, "workerId": "w1", "output": job["output"]})
        return httpx.Response(404)
