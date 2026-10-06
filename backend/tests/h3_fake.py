"""Sahte MiniMax H3 sunucusu (SGLang'in /v1/videos ucu): httpx MockTransport. Hiçbir ağ isteği yapmaz.

İstekleri kaydeder; iş, `polls_to_finish` sorgudan sonra tamamlanır ve `inference_time_s` bildirir. İstenirse gönderim,
iş ya da indirme hata verir.
"""
import json
import re

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
        if request.method == "GET" and path == "/v1/videos":
            return httpx.Response(200, json={"data": [{"id": k, "status": "completed"} for k in self.jobs]})
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


class FakeComfy:
    """Sahte ComfyUI (127.0.0.1'deki sunucu, SSH tüneli ya da portal üzerinden): /upload/image, /prompt, /history, /view,
    /queue, /system_stats. İş, `polls_to_finish` sorgudan sonra biter; GPU süresi geçmişteki zaman damgalarından okunur."""

    def __init__(self, video: bytes, image: bytes = b"", gpu_ms: int = 30_000):
        self.video, self.image, self.gpu_ms = video, image, gpu_ms
        self.polls_to_finish = 2
        self.requests: list[httpx.Request] = []
        self.uploads: list[str] = []
        self.prompts: list[dict] = []
        self.polls: dict[str, int] = {}
        self.failing = 0
        self.reject_next = False
        self.output_ext = ".mp4"
        self.queue_running: list = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/system_stats":
            return httpx.Response(200, json={"system": {"os": "posix"}})
        if path == "/queue":
            return httpx.Response(200, json={"queue_running": self.queue_running, "queue_pending": []})
        if request.method == "POST" and path == "/upload/image":
            name = re.search(rb'filename="([^"]+)"', request.content).group(1).decode()
            self.uploads.append(name)
            return httpx.Response(200, json={"name": name, "subfolder": "", "type": "input"})
        if request.method == "POST" and path == "/prompt":
            body = json.loads(request.content)
            if self.reject_next:
                self.reject_next = False
                return httpx.Response(400, json={"error": {"type": "prompt_outputs_failed_validation"},
                                                 "node_errors": {"7": {"errors": [{"message": "Value not in list"}]}}})
            prompt_id = f"p{len(self.prompts) + 1}"
            self.prompts.append(body["prompt"])
            failed = self.failing > 0
            self.failing -= int(failed)
            self.polls[prompt_id] = -1000 if failed else 0
            return httpx.Response(200, json={"prompt_id": prompt_id, "number": len(self.prompts), "node_errors": {}})
        if path.startswith("/history/"):
            prompt_id = path.rsplit("/", 1)[1]
            if self.polls[prompt_id] < 0:
                return httpx.Response(200, json={prompt_id: {"status": {"status_str": "error", "completed": False,
                    "messages": [["execution_error", {"exception_message": "Allocation on device failed"}]]}, "outputs": {}}})
            self.polls[prompt_id] += 1
            if self.polls[prompt_id] < self.polls_to_finish:
                return httpx.Response(200, json={})
            kind = "images"
            name = f"hg_{prompt_id}{self.output_ext}"
            return httpx.Response(200, json={prompt_id: {
                "status": {"status_str": "success", "completed": True,
                           "messages": [["execution_start", {"timestamp": 1_000}], ["execution_cached", {"timestamp": 1_001}],
                                        ["execution_success", {"timestamp": 1_000 + self.gpu_ms}]]},
                "outputs": {"9": {kind: [{"filename": name, "subfolder": "hellgrind", "type": "output"}]}}}})
        if path == "/view":
            name = request.url.params["filename"]
            return httpx.Response(200, content=self.image if name.endswith((".png", ".jpg")) else self.video)
        return httpx.Response(404)


class FakeVast:
    """Sahte vast.ai API'si: makine durumu, başlat/durdur, teklif arama, kiralama, silme. Varsayılan makine "4242"dir;
    kiralanan makine önce "loading", sonra "running" olur ve portalın 8188 portu dışarıda 41234'e eşlenir."""

    def __init__(self, status: str = "running", geolocation: str = "Norway, NO", dph: float = 0.87):
        self.status, self.geolocation, self.dph = status, geolocation, dph
        self.start_fails = False                      # durdurulan makinenin GPU'su başkasına kiralanmış
        self.requests: list[httpx.Request] = []
        self.puts: list[dict] = []
        self.searches: list[dict] = []
        self.rents: list[tuple[str, dict]] = []
        self.deletes: list[str] = []
        self.offers: list[dict] = [
            {"id": 7001, "ask_contract_id": 7001, "gpu_name": "RTX 5090", "dph_total": 0.81, "geolocation": "Norway, NO",
             "cpu_ram": 128000, "disk_space": 400, "reliability": 0.995, "inet_down": 900},
        ]
        self.new: dict[str, dict] = {}                # kiralanan makineler: kimlik -> durum

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _instance(self, iid: str) -> dict:
        if iid in self.new:
            box = self.new[iid]
            if box["boot"]:
                box["status"] = box["boot"].pop(0)
            return {"id": int(iid), "actual_status": box["status"], "dph_total": 0.81, "geolocation": "Norway, NO",
                    "gpu_name": "RTX 5090", "public_ipaddr": "203.0.113.9",
                    "ports": {"8188/tcp": [{"HostIp": "0.0.0.0", "HostPort": "41234"}],
                              "1111/tcp": [{"HostIp": "0.0.0.0", "HostPort": "41235"}]}}
        if iid in self.deletes:
            return {}
        return {"id": 1, "actual_status": self.status, "intended_status": self.status, "dph_total": self.dph,
                "geolocation": self.geolocation, "gpu_name": "RTX 5090"}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("Authorization") != "Bearer test-vast_api_key":
            return httpx.Response(401, json={"error": "bad key"})
        path = request.url.path
        if request.method == "POST" and path.endswith("/bundles"):
            self.searches.append(json.loads(request.content))
            return httpx.Response(200, json={"offers": self.offers})
        if request.method == "PUT" and "/asks/" in path:
            offer = path.rsplit("/", 1)[1]
            self.rents.append((offer, json.loads(request.content)))
            iid = str(9000 + len(self.rents))
            self.new[iid] = {"status": "loading", "boot": ["loading", "running"]}
            return httpx.Response(200, json={"success": True, "new_contract": int(iid)})
        iid = path.rsplit("/", 1)[1]
        if request.method == "DELETE":
            self.deletes.append(iid)
            return httpx.Response(200, json={"success": True, "msg": "Instance destroyed successfully"})
        if request.method == "PUT":
            body = json.loads(request.content)
            self.puts.append({**body, **({"id": iid} if iid in self.new else {})})
            if iid in self.new:
                self.new[iid]["status"], self.new[iid]["boot"] = body["state"], []
            elif not (self.start_fails and body["state"] == "running"):
                self.status = body["state"]
            return httpx.Response(200, json={"success": True})
        data = self._instance(iid)
        if not data:
            return httpx.Response(404, json={"error": "no such instance"})
        return httpx.Response(200, json={"instances": data})
