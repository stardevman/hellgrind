"""Hell Grind · MiniMax H3 RunPod Serverless worker'ı (runpod/worker-comfyui tabanlı imajın /handler.py'si).

Taban imajın /start.sh'i ComfyUI'yi (127.0.0.1:8188) başlatır, sonra bu dosyayı çalıştırır. Resmî handler'dan farkı:
- **Video da döner:** ComfyUI geçmişindeki bütün çıktı anahtarları (images, gifs, videos...) okunur; resmî handler
  yalnızca "images" anahtarını toplar.
- **Doğrulama bilgisi:** her dosyanın boyutu ve SHA-256'sı yanıtta; Hell Grind indirdiğini bunlarla karşılaştırır.
- **Bedel bilgisi:** işi çalıştıran GPU'nun adı ve worker'ın ilk işiyse soğuk başlatma süresi (süreç açılışından işin
  başına; ComfyUI açılışı ve model yükleme). Faturalanan süre = RunPod'un executionTime'ı + bu süre.
- **Teslim:** dosyalar base64 olarak yanıtta (toplam en çok `inline_limit`, varsayılan 9 MB; RunPod /run sonuçlarını
  ~10 MB'ta keser). Daha büyükse ve S3 tanımlıysa (uç nokta ortamında BUCKET_ENDPOINT_URL, BUCKET_ACCESS_KEY_ID,
  BUCKET_SECRET_ACCESS_KEY, BUCKET_NAME ya da isteğin s3Config'i) dosya yüklenir ve 24 saatlik imzalı indirme adresi döner;
  S3 yoksa iş açık bir hatayla biter (çıktı sessizce kaybolmaz).

Girdi: {"input": {"workflow": {API biçimi}, "images": [{"name", "image": base64}], "hellgrind": {"kind": "video" |
"keyframe", "job", "inline_limit"}}}. Çıktı: {"files": [{"filename", "subfolder", "type": "base64" | "url", "data", "size",
"sha256"}], "gpu", "timing": {"cold_start_s", "execution_s", "comfy_s"}, "comfy": {"prompt_id"}, "version"}; hata:
{"error": "..."} (RunPod işi FAILED sayar).
"""
import base64
import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

VERSION = "hellgrind-h3-worker 1"
PROCESS_START = time.time()
COMFY = os.environ.get("COMFY_HOST", "127.0.0.1:8188")
OUTPUT_DIR = os.environ.get("COMFY_OUTPUT_DIR", "/comfyui/output")
INLINE_LIMIT = 9_000_000
COMFY_READY_S = float(os.environ.get("HG_COMFY_READY_S", "600"))
JOB_TIMEOUT_S = float(os.environ.get("HG_JOB_TIMEOUT_S", "3600"))
POLL_S = 1.0
VIDEO = (".mp4", ".webm", ".mov", ".mkv")
IMAGE = (".png", ".jpg", ".jpeg", ".webp")

_first_job = True


class WorkerError(RuntimeError):
    pass


# ---------- ComfyUI ile konuşma (yalnızca standart kütüphane) ----------

def http(method: str, path: str, body: bytes | None = None, headers: dict | None = None, timeout: float = 30.0):
    """(durum, gövde). Testler bu fonksiyonu sahteler."""
    req = urllib.request.Request(f"http://{COMFY}{path}", data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def wait_comfy(limit_s: float = COMFY_READY_S) -> None:
    deadline = time.time() + limit_s
    while True:
        try:
            if http("GET", "/system_stats", timeout=5)[0] == 200:
                return
        except OSError:
            pass
        if time.time() >= deadline:
            raise WorkerError(f"ComfyUI {limit_s:.0f} sn içinde açılmadı")
        time.sleep(0.5)


def upload_image(name: str, data_b64: str) -> None:
    data = base64.b64decode(data_b64.split(",", 1)[1] if data_b64.startswith("data:") else data_b64)
    boundary = uuid.uuid4().hex
    parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
             f"Content-Type: image/jpeg\r\n\r\n".encode() + data + b"\r\n",
             f"--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n".encode(),
             f"--{boundary}--\r\n".encode()]
    status, body = http("POST", "/upload/image", b"".join(parts),
                        {"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=60)
    if status != 200:
        raise WorkerError(f"giriş görseli yüklenemedi ({name}): HTTP {status} {body[:200]!r}")


def queue(workflow: dict) -> str:
    status, body = http("POST", "/prompt", json.dumps({"prompt": workflow, "client_id": "hellgrind"}).encode(),
                        {"Content-Type": "application/json"})
    if status != 200:
        try:
            detail = json.loads(body)
            errors = "; ".join(f"{node}: {(err.get('errors') or [{}])[0].get('message', err)}"
                               for node, err in (detail.get("node_errors") or {}).items())
            message = errors or json.dumps(detail.get("error") or detail)[:400]
        except ValueError:
            message = body[:400].decode("utf-8", "replace")
        raise WorkerError(f"ComfyUI iş akışını reddetti (HTTP {status}): {message}")
    return json.loads(body)["prompt_id"]


def poll(prompt_id: str, limit_s: float = JOB_TIMEOUT_S) -> dict:
    deadline = time.time() + limit_s
    while True:
        status, body = http("GET", f"/history/{prompt_id}")
        entry = json.loads(body).get(prompt_id) if status == 200 else None
        if entry:
            st = entry.get("status") or {}
            if st.get("status_str") == "error":
                message = next((d.get("exception_message") for n, d in st.get("messages", [])
                                if n == "execution_error" and isinstance(d, dict)), "çalıştırma hatası")
                raise WorkerError(f"ComfyUI işi başarısız: {str(message)[:500]}")
            if st.get("completed", True) or st.get("status_str") == "success":
                return entry
        if time.time() >= deadline:
            raise WorkerError(f"ComfyUI işi {limit_s:.0f} sn içinde bitmedi ({prompt_id})")
        time.sleep(POLL_S)


def comfy_seconds(entry: dict) -> float | None:
    stamps = {name: data.get("timestamp") for name, data in (entry.get("status") or {}).get("messages", [])
              if isinstance(data, dict)}
    start, end = stamps.get("execution_start"), stamps.get("execution_success")
    if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start:
        return round((end - start) / 1000.0, 2)
    return None


def output_items(entry: dict, kind: str) -> list[dict]:
    """Geçmişteki bütün kaydedilmiş çıktı dosyaları (her anahtar: images, gifs, videos...) içinden istenen türden olanlar.
    Önizleme ("temp") çıktıları alınmaz: iş akışında bir kaydetme düğümü olmalı."""
    wanted = IMAGE if kind == "keyframe" else VIDEO
    return [item for out in (entry.get("outputs") or {}).values() for value in out.values() if isinstance(value, list)
            for item in value if isinstance(item, dict) and item.get("type", "output") != "temp"
            and str(item.get("filename", "")).lower().endswith(wanted)]


def read_item(item: dict) -> bytes:
    path = os.path.join(OUTPUT_DIR if item.get("type", "output") == "output" else os.path.dirname(OUTPUT_DIR) + "/temp",
                        item.get("subfolder", ""), item["filename"])
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    q = urllib.parse.urlencode({"filename": item["filename"], "subfolder": item.get("subfolder", ""),
                                "type": item.get("type", "output")})
    status, body = http("GET", f"/view?{q}", timeout=300)
    if status != 200:
        raise WorkerError(f"çıktı okunamadı ({item['filename']}): HTTP {status}")
    return body


def gpu_name() -> str | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], capture_output=True, text=True,
                             timeout=10)
        return out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


# ---------- teslim ----------

def s3_config(job: dict) -> dict | None:
    cfg = job.get("s3Config") or {}
    endpoint = cfg.get("endpointUrl") or os.environ.get("BUCKET_ENDPOINT_URL")
    if not endpoint:
        return None
    return {"endpoint": endpoint, "key": cfg.get("accessId") or os.environ.get("BUCKET_ACCESS_KEY_ID"),
            "secret": cfg.get("accessSecret") or os.environ.get("BUCKET_SECRET_ACCESS_KEY"),
            "bucket": cfg.get("bucketName") or os.environ.get("BUCKET_NAME") or "hellgrind-runpod"}


def upload_s3(cfg: dict, job_id: str, filename: str, data: bytes) -> str:
    import boto3                                    # taban imajda (runpod paketinin bağımlılığı)
    client = boto3.client("s3", endpoint_url=cfg["endpoint"], aws_access_key_id=cfg["key"],
                          aws_secret_access_key=cfg["secret"])
    key = f"hellgrind/{job_id}/{filename}"
    client.put_object(Bucket=cfg["bucket"], Key=key, Body=data)
    return client.generate_presigned_url("get_object", Params={"Bucket": cfg["bucket"], "Key": key}, ExpiresIn=24 * 3600)


def deliver(job: dict, items: list[tuple[dict, bytes]], inline_limit: int) -> list[dict]:
    encoded = sum((len(data) + 2) // 3 * 4 for _, data in items)
    cfg = s3_config(job) if encoded > inline_limit else None
    if encoded > inline_limit and cfg is None:
        raise WorkerError(f"çıktı yanıta sığmıyor ({encoded} bayt base64 > {inline_limit}) ve S3 tanımlı değil: uç noktanın "
                          "ortamına BUCKET_ENDPOINT_URL, BUCKET_ACCESS_KEY_ID, BUCKET_SECRET_ACCESS_KEY, BUCKET_NAME ekleyin")
    files = []
    for item, data in items:
        entry = {"filename": item["filename"], "subfolder": item.get("subfolder", ""), "size": len(data),
                 "sha256": hashlib.sha256(data).hexdigest()}
        if cfg:
            entry.update(type="url", data=upload_s3(cfg, str(job.get("id") or uuid.uuid4()), item["filename"], data))
        else:
            entry.update(type="base64", data=base64.b64encode(data).decode("ascii"))
        files.append(entry)
    return files


def cleanup(items: list[dict]) -> None:
    for item in items:
        path = os.path.join(OUTPUT_DIR, item.get("subfolder", ""), item["filename"])
        try:
            os.remove(path)
        except OSError:
            pass


# ---------- handler ----------

def handler(job: dict) -> dict:
    global _first_job
    started = time.time()
    cold = started - PROCESS_START if _first_job else 0.0
    _first_job = False
    try:
        inp = job.get("input") or {}
        workflow = inp.get("workflow")
        if not isinstance(workflow, dict) or not workflow:
            raise WorkerError("girdide workflow (ComfyUI API biçimi) yok")
        meta = inp.get("hellgrind") or {}
        kind = meta.get("kind") or "video"
        wait_comfy()
        for img in inp.get("images") or []:
            upload_image(img["name"], img["image"])
        prompt_id = queue(workflow)
        entry = poll(prompt_id)
        items = output_items(entry, kind)
        if not items:
            raise WorkerError(f"ComfyUI işi {'görsel' if kind == 'keyframe' else 'video'} çıktısı vermedi: iş akışında bir "
                              "kaydetme düğümü olmalı")
        chosen = [(items[0], read_item(items[0]))]
        files = deliver(job, chosen, int(meta.get("inline_limit") or INLINE_LIMIT))
        cleanup([items[0]])
        return {"files": files, "gpu": gpu_name(), "version": VERSION, "comfy": {"prompt_id": prompt_id},
                "timing": {"cold_start_s": round(cold, 1), "execution_s": round(time.time() - started, 1),
                           "comfy_s": comfy_seconds(entry)}}
    except Exception as e:                           # noqa: BLE001 — RunPod {"error"}'u FAILED sayar
        return {"error": f"{type(e).__name__}: {str(e)[:1000]}", "version": VERSION,
                "timing": {"cold_start_s": round(cold, 1), "execution_s": round(time.time() - started, 1)}}


if __name__ == "__main__":
    import runpod                                    # taban imajda
    runpod.serverless.start({"handler": handler})
