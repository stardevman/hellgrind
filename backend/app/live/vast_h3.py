"""ÜCRETLİ video üretimi: MiniMax H3, yönetmenin vast.ai'de kiraladığı GPU sunucusunda. Bu modül hiçbir yerde çalıştırılmadı.

İki arka uç (Ayarlar › MiniMax H3 › Arka uç; varsayılan ComfyUI):
- **SGLang** (deneysel; docs.sglang.io › MiniMax-H3, Ekim 2026): POST /v1/videos -> {id}; GET /v1/videos/{id} (queued | completed |
  failed; tamamlanınca inference_time_s); GET /v1/videos/{id}/content (MP4). Görseller istekte data URI olarak gider.
- **ComfyUI** (127.0.0.1'de; SSH tüneli ya da portalın kimlik doğrulamalı vekili üzerinden): POST /upload/image, POST
  /prompt, GET /history/{id}, GET /view. İş akışları yönetmenin ComfyUI'den "Export (API)" ile aldığı JSON'lardır
  (`h3_comfy_workflow_dir`: t2v, i2v, r2v, keyframe); değişen değerler {{prompt}}, {{seed}}, {{width}}, {{height}},
  {{frames}}, {{duration}}, {{fps}}, {{steps}}, {{first_frame}}, {{reference_1}}... yer tutucularıyla doldurulur.
  GPU süresi geçmişteki execution_start / execution_success zamanlarından okunur.

Görevler: t2va (yalnızca metin: hareket testi ve dönüş videosunun yedeği), fl2va (verilen ilk kare: çekimin storyboard
karesi ya da yönetmenin yüklediği kare; hareket testi; düzenlenmiş kare), ref2va (karakter referans seti + varsa ilk kare;
çekim başına denetçiden seçilir). Kompozisyon seed'le değil ilk kareyle kontrol edilir: test turu ve tam tur aynı kareden
başlar (aynı seed farklı çözünürlükte farklı kompozisyon verir).

Model CFG damıtılmıştır, negatif istem almaz: negatifler istemin sonuna pozitif kısıt olarak eklenir (style/constraints.py).
Süre 4-15 sn (24 fps); daha kısa çekim kesilir. Her klibin MP4 meta verisine ve kaydına kaynak bilgisi yazılır (model,
sürüm, arka uç, sunucu konumu, lisans; başka modelleri eğitmek için kullanılamaz, Lisans §V.3).

Çıktı güvencesi (app/live/h3_outputs.py): her iş gönderilince kayda geçer; çıktı bitince hemen indirilir, boyutu sunucunun
Content-Length'iyle, depodaki kopyası SHA-256 ile doğrulanır. Bağlantı: TLS doğrula / sabitle / kapalı (app/live/tls_pin.py).

Kilit: `guard("vast_api_key")` (makineyi vast.ai hesabı faturalar). Portal belirteci (`h3_api_key`) isteğe bağlıdır.
Bütçe: rezervasyon = saatlik ücret × video saniyesi başına GPU süresi × süre; işlenen tutar = ölçülen GPU süresi × saatlik
ücret. Boşta bekleme ve makine yönetimi: app/live/vast_box.py. Gönderilmiş iş yeniden denemede yeniden üretilmez.
"""
import base64
import copy
import hashlib
import io
import json
import logging
import math
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from app.budget import history, pricing
from app.config import settings
from app.core.hashing import stable_hash
from app.engine.providers import GenRequest, GenResult, TransientError
from app.live import PaidCallBlocked, guard, h3_outputs, optional, tls_pin, vast_box
from app.style.constraints import constrained_prompt

log = logging.getLogger("hellgrind.live.h3")

ENGINE = "h3"
KEY = "vast_api_key"
FPS = 24
SECONDS = (4, 15)                    # açık model 4-15 sn üretir
SIZES = {"480p": (864, 480), "720p": (1280, 704)}   # yönetmenin ölçtüğü tuvaller; fiyat tablosu bunlarla kalibre
FLOW_SHIFT, AUDIO_FLOW_SHIFT = 12.0, 3.0      # SGLang tarifindeki varsayılanlar
MAX_REFERENCES = 9                   # Ref2VA en çok 9 görsel alır
FIRST_FRAME_NOTE = "The video opens exactly on <Picture 1>, the first frame."
IMAGE_EDGE = 1536                    # istekte gönderilen görselin uzun kenarı
POLL_S = 5.0
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
DOWNLOAD_TIMEOUT = httpx.Timeout(300.0, connect=15.0)
DOWNLOAD_ATTEMPTS = 3
RETRY_BASE_S = 2.0
NOTED_PROMPT_CHARS = 400
TRANSPORT: httpx.BaseTransport | None = None  # testler sahte sunucu bağlar; gerçek ağ yalnızca ücretli modda kullanılır

LICENSE = "MiniMax H3 Community License"
NO_TRAINING = "MiniMax H3 output. Not to be used to improve any other AI model (MiniMax H3 Community License, section V.3)."
COMFY_KINDS = ("t2v", "i2v", "r2v", "keyframe", "edit_keyframe")
COMFY_REQUIRED = {"t2v": ("prompt",), "i2v": ("prompt", "first_frame"), "r2v": ("prompt", "reference_1"),
                  "keyframe": ("prompt",), "edit_keyframe": ("prompt", "reference_1")}
IMAGE_KINDS = ("keyframe", "edit_keyframe")   # görsel çıktılı iş akışları (Qwen-Image; Qwen-Image-Edit karakter referansıyla)
OPTIONAL_IMAGE_INPUTS = ("image2", "image3", "last_frame")  # Qwen-Image-Edit'in image2/3'ü, FL2VA'nın son karesi
LAST_FRAME_NOTE = "The video ends exactly on the provided last frame."
COMFY_CLIENT_ID = "hellgrind"
VIDEO_EXT = (".mp4", ".webm", ".mov", ".mkv")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
PLACEHOLDER = re.compile(r"\{\{\s*([a-z_0-9]+)\s*\}\}")
NOT_FOUND = object()                 # sunucu işi tanımıyor (yeniden indirmede)
_AUTO = object()


# ---------- hatalar ----------

class JobFailed(TransientError):
    """Sunucu işi başarısız bildirdi (ör. bellek yetmedi): yeni bir gönderimle yeniden denenebilir."""


def _status(exc: BaseException) -> int | None:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def is_transient(exc: BaseException) -> bool:
    """429, 5xx ve zaman aşımı yeniden denenebilir. Bağlanamamak ve tünelin kapalı olması (530) hesap sorunudur."""
    status = _status(exc)
    if status is not None:
        return status == 429 or (status >= 500 and status != 530)
    return isinstance(exc, (TimeoutError, httpx.TransportError)) and not isinstance(exc, httpx.ConnectError)


def account_problem(exc: BaseException) -> str | None:
    """Yeniden denemeyle düzelmeyen, yönetmenin elinde olan sorunlar için Türkçe açıklama."""
    status = _status(exc)
    if status in (401, 403):
        return ("H3 sunucusu erişimi reddetti: Ayarlar › MiniMax H3'te portal belirtecini (OPEN_BUTTON_TOKEN ya da "
                "WEB_PASSWORD) ve \"Bağlantıyı sına\" sonucunu kontrol edin.")
    if isinstance(exc, tls_pin.PinMismatch):
        return str(exc)
    if isinstance(exc, httpx.ConnectError) and "CERTIFICATE_VERIFY_FAILED" in str(exc):
        return ("H3 sunucusunun sertifikası doğrulanamadı (kendinden imzalı portal): Ayarlar › MiniMax H3 › TLS'yi "
                "\"pin\" (ilk bağlantıda sabitle) yapın.")
    if status == 530 or isinstance(exc, httpx.ConnectError):
        how = ("SSH tüneli açık mı (ssh -N -L 8188:127.0.0.1:8188 ...) ya da portal adresi doğru mu?"
               if settings.h3_backend == "comfyui" else "vast.ai'de makine çalışıyor mu ve sunucu adresi güncel mi?")
        return (f"H3 sunucusuna bağlanılamadı: {how} Bu çekimde tamamlanmış işlerin GPU süresi harcamaya yazıldı.")
    return None


def _classify(exc: BaseException) -> BaseException:
    if (reason := account_problem(exc)) is not None:
        return PaidCallBlocked(reason)
    if isinstance(exc, TransientError):
        return exc
    if is_transient(exc):
        return TransientError(f"{type(exc).__name__}: {exc}")
    if isinstance(exc, httpx.HTTPStatusError):
        detail = (exc.response.text or "").strip()[:300]
        return RuntimeError(f"H3 sunucusu isteği reddetti (HTTP {exc.response.status_code}): {detail}")
    return exc


def _reraise(exc: BaseException):
    classified = _classify(exc)
    if classified is exc:
        raise exc
    raise classified from exc


# ---------- süre, tuval, görseller ----------

def seconds_for(duration_s: float) -> int:
    """Üretilen ve GPU'da harcanan süre: 4-15 sn, tam saniye."""
    return int(min(SECONDS[1], max(SECONDS[0], math.ceil(duration_s - 1e-6))))


def size_for(resolution: str) -> tuple[int, int]:
    return SIZES.get(resolution, SIZES["720p"])


def priced_resolution(resolution: str) -> str:
    return "480p" if size_for(resolution) == SIZES["480p"] else "720p"


def frames_for(seconds: int) -> int:
    """H3'ün 17 karelik blok ızgarası (17k+5, 24 fps): 4 sn -> 107, 5 sn -> 124."""
    return 17 * math.ceil((FPS * seconds - 5) / 17) + 5


def data_uri(path: str) -> str:
    """Görseli JPEG'e çevirip (uzun kenar en çok IMAGE_EDGE) data URI yapar."""
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((IMAGE_EDGE, IMAGE_EDGE))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def uri_bytes(uri: str) -> bytes:
    return base64.b64decode(uri.split(",", 1)[1])


_reference_uris: dict[str, str] = {}
_reference_lock = threading.Lock()


def reference_uri(key: str) -> str:
    """Depodaki referans görseli bir kez indirip data URI'ye çevirir. Anahtar içerik-adreslidir: önbellek güvenlidir."""
    import tempfile
    from app import storage
    with _reference_lock:                        # dalga iş parçacıklıdır: aynı görsel bir kez çevrilsin
        if key not in _reference_uris:
            os.makedirs(settings.work_dir, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
                path = storage.download(key, os.path.join(tmp, "reference" + os.path.splitext(key)[1]))
                _reference_uris[key] = data_uri(path)
        return _reference_uris[key]


# ---------- iş ----------

@dataclass
class Job:
    task: str                     # t2va | fl2va | ref2va | keyframe | edit_keyframe
    url: str                      # sunucu adresi
    prompt: str
    seed: int
    seconds: int
    width: int
    height: int
    steps: int
    keyframe: str | None = None                                    # data URI
    references: list[tuple[str, str]] = field(default_factory=list)   # (karakter, data URI)
    unmapped: tuple[str, ...] = ()                                 # pozitif kısıta çevrilemeyen negatifler
    negative: str = ""            # yalnızca Qwen-Image-Edit karesi (cfg 4 olumsuz istemi kullanır; H3 kullanmaz)
    last_frame: str | None = None  # FL2VA: son kare (data URI; sonraki çekimin ilk karesi)
    audio: bool = True            # proje ayarı audio=off: ses VAE'si yüklenmez, ses çözülmez, video sessiz
    edit_mode: str = "full"       # Qwen-Image-Edit: full (40 adım, cfg 4) | medium (20, cfg 4) | lightning (4, cfg 1)

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.references]

    def key(self) -> str:
        return stable_hash([settings.h3_platform, settings.h3_backend, settings.h3_unet_variant, self.task, self.url, self.prompt, self.seed, self.seconds, self.width,
                            self.height, self.steps, self.keyframe, self.references,
                            *([self.negative, self.edit_mode] if self.task == "edit_keyframe" else []),
                            *([self.last_frame] if self.last_frame else []),
                            *(["no-audio"] if not self.audio else [])])


def reference_prompt(prompt: str, names: list[str]) -> str:
    """Ref2VA görselleri istemdeki <Picture n> etiketiyle eşler (bir tabanlı sıra); aynı karakterin görselleri gruplanır."""
    groups: dict[str, list[int]] = {}
    for i, name in enumerate(names, start=1):
        groups.setdefault(name, []).append(i)
    parts = []
    for name, idx in groups.items():
        tags = ", ".join(f"<Picture {i}>" for i in idx)
        parts.append(f"{tags} {'show' if len(idx) > 1 else 'shows'} {name}")
    return (f"Character references: {'; '.join(parts)}. Keep each character's face, hair, proportions and outfit "
            f"exactly as in their pictures. {prompt}")


def ref_url() -> str:
    """Ref2VA adresi: SGLang'de ayrı bir sunucudur; ComfyUI'de boşsa aynı sunucu (r2v iş akışı) kullanılır."""
    if settings.h3_ref_endpoint_url:
        return settings.h3_ref_endpoint_url
    return settings.h3_endpoint_url if settings.h3_backend == "comfyui" else ""


def build_job(req: GenRequest, server: str | None = None) -> Job:
    """server: işin gideceği tek adres (RunPod uç noktası); verilmezse vast.ai ayarlarındaki FL2VA / Ref2VA adresleri."""
    mode = req.extra.get("h3_mode") or "fl2va"
    prompt, constraints = constrained_prompt(req.prompt, req.negative)
    width, height = size_for(req.resolution)
    common = dict(seed=req.seed, seconds=seconds_for(req.duration_s), width=width, height=height,
                  steps=int(settings.h3_steps), keyframe=req.image_url, unmapped=constraints.unmapped,
                  audio=req.extra.get("audio", True) is not False)
    if mode == "ref2va":
        refs = [r for r in req.extra.get("reference_set") or req.extra.get("stills") or [] if r.get("key")][:MAX_REFERENCES]
        if not refs:
            raise PaidCallBlocked("Ref2VA için çekimdeki karakterlerin referans görseli yok: Varlıklar'dan durağan görsel, "
                                  "ifade sayfası ya da dönüş videosu yükleyin ya da çekimi FL2VA'ya alın.")
        url = server or ref_url()
        if not url:
            raise PaidCallBlocked("Ref2VA sunucusu tanımlı değil: Ayarlar › MiniMax H3'ten Ref2VA adresini girin ya da "
                                  "çekimi FL2VA'ya alın.")
        references = [(r["name"], reference_uri(r["key"])) for r in refs]
        return Job(task="ref2va", url=url.rstrip("/"), prompt=reference_prompt(prompt, [n for n, _ in references]),
                   references=references, **common)
    task = "fl2va" if req.image_url else "t2va"
    last = req.extra.get("last_frame") if task == "fl2va" else None
    if task == "fl2va":                                  # resmî FL2VA şablonu ilk kareye <Picture 1> diye başvurur
        prompt = f"{prompt} {FIRST_FRAME_NOTE}" + (f" {LAST_FRAME_NOTE}" if last else "")
    return Job(task=task, url=(server or settings.h3_endpoint_url).rstrip("/"), prompt=prompt, last_frame=last, **common)


def _client(url: str, token: str, observer=_AUTO) -> httpx.Client:
    """H3 sunucusu istemcisi. pin kipinde her bağlantının sertifikası el sıkışmada sabitlenen parmak iziyle karşılaştırılır;
    adres henüz sabitlenmediyse önce kimlik doğrulamalı sağlık sorgusu yapılır (ilk başarılı bağlantı sabitler)."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if TRANSPORT is not None:                         # testlerin sahte sunucusu
        options: dict = {"transport": TRANSPORT}
    else:
        if observer is _AUTO:
            observer = tls_pin.observer_for(url)
            if observer is not None and observer.expected is None:
                healthy(url, token)                   # başarılıysa sabitler
                observer = tls_pin.observer_for(url)
        options = tls_pin.client_options(url, observer)
    return httpx.Client(base_url=url, headers=headers, timeout=TIMEOUT, follow_redirects=True, **options)


def _note(job: Job, job_id: str, negative: str) -> None:
    # Kuyruğa giren iş kayda hemen geçer: sonucu alınamasa da GPU'da çalışmış olabilir.
    history.step(settings.h3_model, job_id, task=job.task, backend=settings.h3_backend, unet_variant=settings.h3_unet_variant,
                 resolution=f"{job.width}x{job.height}", duration=job.seconds, seed=job.seed, steps=job.steps,
                 references=len(job.references) or None, keyframe=bool(job.keyframe) or None,
                 negative_dropped=bool(negative) or None, negatives_unmapped=list(job.unmapped) or None,
                 prompt=job.prompt[:NOTED_PROMPT_CHARS])


def _register(job: Job, job_id: str, kind: str, project_id: str | None, prefix: str | None = None) -> None:
    h3_outputs.submitted(job.url, job_id, kind, job.task, project_id,
                         {"seed": job.seed, "resolution": f"{job.width}x{job.height}", "seconds": job.seconds,
                          **({"prefix": prefix} if prefix else {})})


@dataclass
class Download:
    """Sunucudan indirilen çıktı: yerel yol, bayt sayısı (Content-Length ile doğrulandı), SHA-256, sunucudaki adı."""
    path: str
    size: int
    sha256: str
    name: str


# ---------- SGLang ----------

def sglang_body(job: Job) -> dict:
    conditions = [{"type": "image", "uri": uri, "role": "reference"} for _, uri in job.references]
    if job.keyframe:                                   # referanslardan sonra: <Picture n> referansları gösterir
        conditions.append({"type": "image", "uri": job.keyframe, "role": "keyframe", "frame_index": 0})
    return {"model": settings.h3_model, "prompt": job.prompt, "seconds": job.seconds, "task": job.task,
            "conditions": conditions,
            "target": {"short_edge": job.height, "aspect_ratio": "16:9", "duration_seconds": float(job.seconds)},
            "num_outputs_per_prompt": 1, "num_inference_steps": job.steps, "flow_shift": FLOW_SHIFT,
            "audio_flow_shift": AUDIO_FLOW_SHIFT, "seed": job.seed}


class SGLang:
    name = "sglang"
    health_path = "/health"

    def __init__(self, client: httpx.Client):
        self.c = client

    def submit(self, job: Job, kind: str | None = None) -> str:
        r = self.c.post("/v1/videos", json=sglang_body(job))
        r.raise_for_status()
        return r.json()["id"]

    def poll(self, job_id: str) -> dict | None:
        r = self.c.get(f"/v1/videos/{job_id}")
        r.raise_for_status()
        job = r.json()
        status = job.get("status")
        if status == "completed":
            gpu = job.get("inference_time_s")
            return {"gpu_seconds": float(gpu) if isinstance(gpu, (int, float)) and gpu > 0 else None}
        if status in ("failed", "deleted"):
            message = (job.get("error") or {}).get("message") or status
            raise JobFailed(f"H3 işi başarısız ({job_id}): {str(message)[:300]}")
        return None

    def download(self, job_id: str, done: dict, image: bool = False) -> Download:
        return _stream(self.c, f"/v1/videos/{job_id}/content", None, ".mp4", f"{job_id}.mp4")

    def queue_empty(self) -> bool:
        r = self.c.get("/v1/videos")
        r.raise_for_status()
        return not any(v.get("status") in ("queued", "in_progress") for v in r.json().get("data", []))


# ---------- ComfyUI ----------

def workflow_path(kind: str) -> str:
    return os.path.join(settings.h3_comfy_workflow_dir, f"{kind}.json")


def placeholders(value) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        found.update(PLACEHOLDER.findall(value))
    elif isinstance(value, dict):
        for v in value.values():
            found |= placeholders(v)
    elif isinstance(value, list):
        for v in value:
            found |= placeholders(v)
    return found


def validate_workflow(kind: str, text: str) -> dict:
    """ComfyUI "Export (API)" biçimi mi ve gerekli yer tutucular var mı. Hatalar Türkçe ValueError."""
    if kind not in COMFY_KINDS:
        raise ValueError(f"Bilinmeyen iş akışı: {kind} ({', '.join(COMFY_KINDS)})")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON okunamadı: {e}") from e
    if not isinstance(data, dict) or not data or not all(isinstance(n, dict) and "class_type" in n and "inputs" in n
                                                          for n in data.values()):
        raise ValueError("ComfyUI'nin API biçimi değil: her düğümün class_type ve inputs alanı olmalı (ComfyUI'de "
                         "Workflow › Export (API) ile kaydedin).")
    missing = [p for p in COMFY_REQUIRED[kind] if p not in placeholders(data)]
    if missing:
        raise ValueError(f"{kind}.json yer tutucuları eksik: {', '.join('{{' + m + '}}' for m in missing)}")
    return data


def load_workflow(kind: str) -> dict:
    path = workflow_path(kind)
    if not os.path.exists(path):
        raise PaidCallBlocked(f"ComfyUI iş akışı yok ({kind}.json): Ayarlar › MiniMax H3 › ComfyUI iş akışları'ndan "
                              "yükleyin.")
    with open(path, encoding="utf-8") as f:
        return validate_workflow(kind, f.read())


def fill(value, values: dict):
    """Yer tutucuları doldurur: dizenin tamamı tek bir yer tutucuysa değer kendi türüyle (sayı) konur."""
    if isinstance(value, str):
        whole = PLACEHOLDER.fullmatch(value.strip())
        if whole and whole.group(1) in values:
            return values[whole.group(1)]
        return PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), value)
    if isinstance(value, dict):
        return {k: fill(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, values) for v in value]
    return value


REF_SLOT = re.compile(r"^\{\{\s*reference_(\d+)\s*\}\}$")


AUDIO_DECODERS = ("VAEDecodeAudio",)


def strip_audio(workflow: dict) -> dict:
    """Proje ayarı audio=off: H3'ün ürettiği ses çözülmez. VAEDecodeAudio düğümleri ve onlara bağlanan girdiler (ör.
    CreateVideo.audio) çıkarılır; artık kullanılmayan VAE yükleyicisi (ses VAE'si) de yüklenmez. Ortak ses+video gürültü
    giderme (örnekleyici) aynen kalır: yalnızca çıktıdaki ses çözme adımı atlanır."""
    removed = {nid for nid, node in workflow.items() if node.get("class_type") in AUDIO_DECODERS}
    if not removed:
        return workflow
    feeds = set()
    for nid in removed:
        for value in (workflow[nid].get("inputs") or {}).values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                feeds.add(value[0])
        workflow.pop(nid)
    for node in workflow.values():
        inputs = node.get("inputs") or {}
        for key in [k for k, v in inputs.items() if isinstance(v, list) and len(v) == 2 and v[0] in removed]:
            inputs.pop(key)
    used = {v[0] for node in workflow.values() for v in (node.get("inputs") or {}).values()
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str)}
    for nid in feeds:
        if nid in workflow and nid not in used and workflow[nid].get("class_type") == "VAELoader":
            workflow.pop(nid)
    return workflow


def prune_unfilled(workflow: dict, values: dict) -> dict:
    """Değeri gönderilmeyen isteğe bağlı görseli (ör. `{{last_frame}}`) yükleyen LoadImage düğümünü ve ona bağlanan
    isteğe bağlı girdiyi çıkarır (zorunlu girdiye bağlıysa dokunmaz)."""
    links: dict[str, list[tuple[str, str]]] = {}
    for node_id, node in workflow.items():
        for key, value in (node.get("inputs") or {}).items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                links.setdefault(value[0], []).append((node_id, key))
    for node_id, node in list(workflow.items()):
        m = PLACEHOLDER.fullmatch(str((node.get("inputs") or {}).get("image", "")).strip())
        if not m or m.group(1) in values or REF_SLOT.match(m.group(0)):
            continue
        users = links.get(node_id, [])
        if users and all(key in OPTIONAL_IMAGE_INPUTS for _, key in users):
            for user, key in users:
                workflow[user]["inputs"].pop(key, None)
            workflow.pop(node_id)
    return workflow


def prune_references(workflow: dict, n: int) -> dict:
    """Gönderilen görselden fazla referans yuvasını çıkarır: `{{reference_k}}` (k > n) görselini yükleyen LoadImage
    düğümü, ona yalnızca isteğe bağlı referans girdilerinden (Ref2VA'nın `ref_images.ref_image_*`, Qwen-Image-Edit'in
    `image2` / `image3`) bağlanılıyorsa silinir. Zorunlu bir girdiden bağlanılan yuva kalır ve son görselle doldurulur
    (eski iş akışları)."""
    links: dict[str, list[tuple[str, str]]] = {}
    for node_id, node in workflow.items():
        for key, value in (node.get("inputs") or {}).items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                links.setdefault(value[0], []).append((node_id, key))
    for node_id, node in list(workflow.items()):
        m = REF_SLOT.match(str((node.get("inputs") or {}).get("image", "")))
        if not m or int(m.group(1)) <= n:
            continue
        users = links.get(node_id, [])
        if users and all(key.startswith("ref_images.") or key in OPTIONAL_IMAGE_INPUTS for _, key in users):
            for user, key in users:
                workflow[user]["inputs"].pop(key, None)
            workflow.pop(node_id)
    return workflow


def comfy_seconds(entry: dict) -> float | None:
    """GPU süresi: geçmişteki execution_start ile execution_success arası (ms)."""
    stamps = {name: data.get("timestamp") for name, data in (entry.get("status") or {}).get("messages", [])
              if isinstance(data, dict)}
    start, end = stamps.get("execution_start"), stamps.get("execution_success")
    if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start:
        return (end - start) / 1000.0
    return None


class Comfy:
    name = "comfyui"
    health_path = "/system_stats"
    _uploaded: dict[tuple[str, str], str] = {}
    _lock = threading.Lock()

    def __init__(self, client: httpx.Client):
        self.c = client

    def upload(self, uri: str) -> str:
        data = uri_bytes(uri)
        digest = hashlib.sha256(data).hexdigest()[:16]
        cache_key = (str(self.c.base_url), digest)
        with self._lock:
            if cache_key in self._uploaded:
                return self._uploaded[cache_key]
        r = self.c.post("/upload/image", files={"image": (f"hg_{digest}.jpg", data, "image/jpeg")},
                        data={"type": "input", "overwrite": "true"})
        r.raise_for_status()
        body = r.json()
        name = f"{body['subfolder']}/{body['name']}" if body.get("subfolder") else body["name"]
        with self._lock:
            self._uploaded[cache_key] = name
        return name

    def values(self, job: Job, kind: str) -> dict:
        values = {"prompt": job.prompt, "seed": job.seed, "width": job.width, "height": job.height, "fps": FPS,
                  "duration": job.seconds, "frames": frames_for(job.seconds), "steps": job.steps,
                  "filename_prefix": prefix_for(job, kind), "unet_variant": settings.h3_unet_variant}
        if job.keyframe:
            values["first_frame"] = self.upload(job.keyframe)
        names = [self.upload(uri) for _, uri in job.references]
        for i in range(1, MAX_REFERENCES + 1):            # iş akışındaki fazla yuvalar son görselle dolar
            if names:
                values[f"reference_{i}"] = names[min(i, len(names)) - 1]
        return values

    def submit(self, job: Job, kind: str | None = None) -> str:
        kind = kind or comfy_kind(job)
        values = self.values(job, kind)
        workflow = prune_unfilled(prune_references(copy.deepcopy(load_workflow(kind)), len(job.references)), values)
        workflow = fill(workflow if job.audio else strip_audio(workflow), values)
        r = self.c.post("/prompt", json={"prompt": workflow, "client_id": COMFY_CLIENT_ID})
        if r.status_code == 400:
            body = r.json() if r.content else {}
            errors = "; ".join(f"{node}: {err.get('errors', [{}])[0].get('message', err)}"
                               for node, err in (body.get("node_errors") or {}).items())
            raise RuntimeError(f"ComfyUI iş akışını reddetti ({kind}.json): {errors or body.get('error') or r.text[:300]}")
        r.raise_for_status()
        return r.json()["prompt_id"]

    def poll(self, prompt_id: str) -> dict | None:
        r = self.c.get(f"/history/{prompt_id}")
        r.raise_for_status()
        entry = r.json().get(prompt_id)
        if not entry:
            return None
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            message = next((d.get("exception_message") for n, d in status.get("messages", [])
                            if n == "execution_error" and isinstance(d, dict)), "çalıştırma hatası")
            raise JobFailed(f"ComfyUI işi başarısız ({prompt_id}): {str(message)[:300]}")
        if not status.get("completed", True) and status.get("status_str") != "success":
            return None
        return {"gpu_seconds": comfy_seconds(entry), "outputs": entry.get("outputs") or {},
                "status": {"status_str": status.get("status_str"), "messages": status.get("messages")}}

    def queued(self, prompt_id: str) -> bool:
        """İş sunucunun kuyruğunda mı (çalışıyor ya da bekliyor)."""
        r = self.c.get("/queue")
        r.raise_for_status()
        q = r.json()
        return any(isinstance(item, list) and len(item) > 1 and item[1] == prompt_id
                   for item in (q.get("queue_running") or []) + (q.get("queue_pending") or []))

    def download(self, prompt_id: str, done: dict, image: bool = False) -> Download:
        wanted = IMAGE_EXT if image else VIDEO_EXT
        files = [item for out in done["outputs"].values() for items in out.values() if isinstance(items, list)
                 for item in items if isinstance(item, dict) and str(item.get("filename", "")).lower().endswith(wanted)]
        files.sort(key=lambda f: f.get("type") != "output")             # kaydedilmiş çıktı geçici olandan önce
        if not files:
            raise RuntimeError(f"ComfyUI işi {'görsel' if image else 'video'} çıktısı vermedi ({prompt_id}): iş akışında "
                               "bir kaydetme düğümü olmalı.")
        f = files[0]
        suffix = os.path.splitext(f["filename"])[1].lower()
        name = f"{f.get('subfolder')}/{f['filename']}" if f.get("subfolder") else f["filename"]
        return _stream(self.c, "/view", {"filename": f["filename"], "subfolder": f.get("subfolder", ""),
                                         "type": f.get("type", "output")}, suffix, name)

    def queue_empty(self) -> bool:
        r = self.c.get("/queue")
        r.raise_for_status()
        q = r.json()
        return not q.get("queue_running") and not q.get("queue_pending")


def comfy_kind(job: Job) -> str:
    return {"t2va": "t2v", "fl2va": "i2v", "ref2va": "r2v"}.get(job.task, job.task)


def prefix_for(job: Job, kind: str) -> str:
    """Kaydetme düğümünün işe özgü dosya öneki: ComfyUI geçmişi kaybolsa da çıktı makinede bulunabilir."""
    return f"hellgrind/{kind}_{job.key()[:16]}"


def backend_for(client: httpx.Client):
    return Comfy(client) if settings.h3_backend == "comfyui" else SGLang(client)


class ShortDownload(TransientError):
    """İndirilen bayt sayısı sunucunun bildirdiğinden farklı (yarım indirme)."""


def _stream(client: httpx.Client, path: str, params: dict | None, suffix: str, name: str) -> Download:
    """Dosyayı indirir, bayt sayısını sunucunun Content-Length'iyle doğrular ve SHA-256'sını hesaplar; ağ kesintisinde ya
    da yarım indirmede yeniden dener (iş sunucuda durur, yeniden üretilmez)."""
    os.makedirs(settings.work_dir, exist_ok=True)
    out = os.path.join(settings.work_dir, f"{uuid.uuid4()}{suffix}")
    last: BaseException | None = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            digest, size = hashlib.sha256(), 0
            with client.stream("GET", path, params=params, timeout=DOWNLOAD_TIMEOUT) as response:
                response.raise_for_status()
                expected = response.headers.get("content-length")
                encoded = response.headers.get("content-encoding", "identity") not in ("", "identity")
                with open(out, "wb") as f:
                    for chunk in response.iter_bytes():
                        f.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
            if expected and expected.isdigit() and not encoded and int(expected) != size:
                raise ShortDownload(f"yarım indirme: {size} / {expected} bayt ({name})")
            if size == 0:
                raise ShortDownload(f"boş dosya ({name})")
            return Download(path=out, size=size, sha256=digest.hexdigest(), name=name)
        except (httpx.TransportError, httpx.HTTPStatusError, ShortDownload) as e:
            last = e
            if os.path.exists(out):
                os.remove(out)
            if isinstance(e, httpx.HTTPStatusError) and not is_transient(e):
                raise
            log.warning("H3 indirme hatası (deneme %d/%d): %s", attempt, DOWNLOAD_ATTEMPTS, e)
            time.sleep(min(RETRY_BASE_S * 2 ** (attempt - 1), 8))
    raise TransientError(f"H3 çıktısı indirilemedi: {last}")


def _to_mp4(path: str) -> str:
    out = os.path.splitext(path)[0] + ".mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", path, "-c:v", "libx264", "-preset",
                    "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", out],
                   check=True, capture_output=True)
    os.remove(path)
    return out


def _wait(backend, job_id: str, job_key: str) -> dict:
    deadline = time.monotonic() + float(settings.h3_timeout_s)
    while True:
        done = backend.poll(job_id)
        if done is not None:
            return done
        vast_box.heartbeat(job_key)
        if time.monotonic() >= deadline:
            raise TransientError(f"H3 işi {settings.h3_timeout_s} sn içinde bitmedi ({job_id})")
        time.sleep(POLL_S)


def healthy(url: str, token: str) -> bool:
    """Sunucu hazır mı (kimlik doğrulamalı sağlık sorgusu). pin kipinde ilk başarılı yanıt sertifikayı sabitler."""
    observer = tls_pin.observer_for(url) if TRANSPORT is None else None
    with _client(url, token, observer) as c:
        ok = c.get(backend_for(c).health_path).status_code == 200
    if ok and observer is not None:
        observer.confirm()
    return ok


def collect(backend, job: Job, job_id: str, done: dict, image: bool = False, extra: dict | None = None) -> str:
    """Çıktıyı indirir, doğrular ve yerel depoya yazar (çıktı güvencesi); kullanıma hazır yerel yol (video: MP4)."""
    download = backend.download(job_id, done, image=image)
    h3_outputs.archive(job.url, job_id, download, extra)
    if not image and not download.path.endswith(".mp4"):
        return _to_mp4(download.path)
    return download.path


# ---------- kaynak bilgisi (lisans) ----------

def provenance(job: Job, box) -> dict:
    """Klibin kaynağı: model, sürüm, arka uç, sunucu ve konumu, lisans. Kayda ve MP4 meta verisine yazılır."""
    location = box.geolocation if box is not None else (vast_box.cached() or {}).get("geolocation")
    return {"model": settings.h3_model, "model_revision": settings.h3_model_revision or None,
            "backend": settings.h3_backend, "unet_variant": settings.h3_unet_variant, "task": job.task, "steps": job.steps,
            "server": urlparse(job.url).hostname, "server_location": location,
            "vast_instance_id": settings.vast_instance_id or None, "license": LICENSE, "training_allowed": False}


def finish(path: str, duration_s: float | None, prov: dict) -> str:
    """Klibe kaynak bilgisini yazar; çekim 4 sn'den kısaysa çekim süresine keser (fazlasının GPU süresi harcanmıştır)."""
    out = path[:-4] + "_hg.mp4"
    cut = ["-t", f"{duration_s:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k"] \
        if duration_s else ["-c", "copy"]
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", path, *cut,
                    "-metadata", f"comment={json.dumps(prov, ensure_ascii=False)}", "-metadata", f"copyright={NO_TRAINING}",
                    "-metadata", f"title={prov['model']} {prov['task']}", "-movflags", "+faststart", out],
                   check=True, capture_output=True)
    os.replace(out, path)
    return path


# ---------- sağlayıcı ----------

class H3Provider:
    """Yerel sağlayıcıyla aynı arayüz: `engine`, `estimate(req)`, `generate(req)`, `upload(path)`. Önce görsel yoktur:
    video, çekimin onaylı ilk karesinden (ya da referans setinden) doğrudan üretilir ve kalite kapısı videoyu puanlar."""

    engine = ENGINE
    key_name = KEY
    image_first = False

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict[str, str] = {}          # iş özeti -> iş kimliği (yeniden deneme aynı işi bekler)

    def billed_seconds(self, duration_s: float) -> float:
        return float(seconds_for(duration_s))

    def resolution(self, requested: str) -> str:
        return priced_resolution(requested)

    def estimate(self, req: GenRequest) -> float:
        return pricing.estimate_video(ENGINE, self.resolution(req.resolution), self.billed_seconds(req.duration_s))

    def upload(self, path: str) -> str:
        """İlk kare: istekte data URI olarak gider (ComfyUI'de gönderimden önce sunucuya yüklenir)."""
        return data_uri(path)

    def generate(self, req: GenRequest) -> GenResult:
        guard(KEY)
        token = optional("h3_api_key")
        job = build_job(req)
        box = vast_box.ensure_running(lambda: healthy(job.url, token))
        key = job.key()
        try:
            with _client(job.url, token) as client:
                backend = backend_for(client)
                with self._lock:
                    job_id = self._jobs.get(key)
                if job_id is None:
                    job_id = backend.submit(job)
                    _note(job, job_id, req.negative)
                    _register(job, job_id, "video", req.extra.get("project_id"),
                              prefix_for(job, comfy_kind(job)) if isinstance(backend, Comfy) else None)
                    with self._lock:
                        self._jobs[key] = job_id
                else:
                    history.step(settings.h3_model, job_id, task=job.task, reused=True)
                vast_box.track(key, req.extra.get("project_id"))
                try:
                    done = _wait(backend, job_id, key)
                except JobFailed as e:
                    with self._lock:
                        self._jobs.pop(key, None)            # başarısız iş yeniden gönderilir
                    h3_outputs.failed(job.url, job_id, str(e))
                    raise
                h3_outputs.completed(job.url, job_id, done)
                path = collect(backend, job, job_id, done, extra={"provenance": provenance(job, box)})
        except Exception as e:
            _reraise(e)
        finally:
            vast_box.untrack(key)
        with self._lock:
            self._jobs.pop(key, None)
        prov = provenance(job, box)
        cut = req.duration_s if self.billed_seconds(req.duration_s) - req.duration_s > 0.05 else None
        finish(path, cut, prov)
        gpu = done.get("gpu_seconds")
        if gpu:
            history.step(settings.h3_model, job_id, gpu_seconds=round(gpu, 1))
        cost = pricing.estimate_gpu(ENGINE, gpu) if gpu else self.estimate(req)
        return GenResult(local_path=path, request_id=job_id, cost_usd=cost, engine=ENGINE,
                         meta={"task": job.task, "billed_seconds": self.billed_seconds(req.duration_s),
                               "size": f"{job.width}x{job.height}", "gpu_seconds": gpu, "references": job.names,
                               "negative_dropped": bool(req.negative), "negatives_unmapped": list(job.unmapped),
                               "seed_sent": True, "provenance": prov})


def keyframe_image(prompt: str, seed: int, project_id: str | None = None) -> GenResult:
    """Storyboard ilk karesi, H3 makinesindeki ComfyUI görsel iş akışıyla (keyframe.json). Bu kare H3 çıktısı değildir:
    iş akışındaki görsel modelin çıktısıdır. Çağıran `pricing.estimate_keyframe()` kadar rezervasyon almış olmalı."""
    guard(KEY)
    token = optional("h3_api_key")
    width, height = SIZES["720p"]
    job = Job(task="keyframe", url=settings.h3_endpoint_url.rstrip("/"), prompt=prompt, seed=seed, seconds=SECONDS[0],
              width=width, height=height, steps=int(settings.h3_steps))
    vast_box.ensure_running(lambda: healthy(job.url, token))
    key = job.key()
    try:
        with _client(job.url, token) as client:
            backend = Comfy(client)
            prompt_id = backend.submit(job, kind="keyframe")
            history.step(f"ComfyUI keyframe.json", prompt_id, seed=seed, resolution=f"{width}x{height}",
                         prompt=prompt[:NOTED_PROMPT_CHARS])
            _register(job, prompt_id, "keyframe", project_id, prefix_for(job, "keyframe"))
            vast_box.track(key, project_id)
            try:
                done = _wait(backend, prompt_id, key)
            except JobFailed as e:
                h3_outputs.failed(job.url, prompt_id, str(e))
                raise
            h3_outputs.completed(job.url, prompt_id, done)
            path = collect(backend, job, prompt_id, done, image=True)
    except Exception as e:
        _reraise(e)
    finally:
        vast_box.untrack(key)
    gpu = done.get("gpu_seconds")
    cost = pricing.estimate_gpu(ENGINE, gpu) if gpu else pricing.estimate_keyframe(ENGINE)
    return GenResult(local_path=path, request_id=prompt_id, cost_usd=cost, engine=ENGINE, meta={"kind": "keyframe"})


def comfy_keyframes_ready() -> bool:
    """Storyboard ilk karesi H3 makinesinde çizilebilir mi (ComfyUI arka ucu ve keyframe.json var)."""
    return settings.h3_backend == "comfyui" and os.path.exists(workflow_path("keyframe"))


def queue_empty() -> bool:
    """Sunucunun kendi kuyruğu boş mu (boşta durdurmadan önce; başka bir istemcinin işi kesilmesin)."""
    token = optional("h3_api_key")
    with _client(settings.h3_endpoint_url.rstrip("/"), token) as client:
        return backend_for(client).queue_empty()


def get_live_provider() -> H3Provider:
    """Sağlayıcı kurulurken kilit ve adres denetlenir: engelliyse hiçbir varyasyon satırı ya da rezervasyon oluşmaz."""
    guard(KEY)
    if not settings.h3_endpoint_url:
        raise PaidCallBlocked("H3 sunucu adresi tanımlı değil: Ayarlar › MiniMax H3'ten vast.ai sunucusunun adresini girin.")
    return H3Provider()
