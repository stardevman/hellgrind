"""ÜCRETLİ: MiniMax H3 Max (taslak), Seedance 2.5 (final) ve FLUX 3 Max (yalnızca image-to-video; projeye özel) fal.ai
kuyruğunda. Bu modül testlerde yalnızca sahte
istemciyle çalışır; canlı üretim yönetmenin ayrıca onayıyla yapılır.

İstek gövdesi, kuru çalıştırmanın gösterdiğiyle aynı fonksiyondan gelir (app/engine/fal_plan.body). Yerel dosyalar
(başlangıç ve bitiş karesi, referans görselleri) fal deposuna SHA-256 önbellekli yüklenir (app/live/fal_queue). Sonuç
gelir gelmez indirilir; çağıran nesne deposuna yazar.

Bedel: rezervasyon fal_plan.cost (Seedance tahmini %25 paylı); işlenen tutar çıktının ölçülen boyu ve süresinden
(Seedance token × billing_factor) ya da istenen süre ve görsel sayısından (H3 Max). fal yanıtı fatura tutarı döndürmez:
kesin tutar fal.ai faturasındadır; kayıtta tahmin edilen birimler (Seedance token, H3 Max 0,05 $'lık birim) durur.
"""
import json
import logging
import subprocess

from app.budget import pricing
from app.config import settings
from app.engine import fal_plan
from app.engine.providers import GenRequest, GenResult
from app.live import guard
from app.live import fal_queue

log = logging.getLogger("hellgrind.live.fal_engines")


def probe(path: str) -> dict:
    """Çıktının ölçülen boyu ve süresi (ffprobe)."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height:format=duration", "-of", "json", path],
                         capture_output=True, text=True, check=True).stdout
    data = json.loads(out or "{}")
    stream = (data.get("streams") or [{}])[0]
    return {"width": int(stream.get("width") or 0), "height": int(stream.get("height") or 0),
            "duration_s": round(float((data.get("format") or {}).get("duration") or 0.0), 3)}


def strip_audio(path: str) -> str:
    """Klibin ses akışını yerinde siler (görüntü kopyalanır). Filmin sesi Hell Grind'den gelir."""
    import os
    tmp = path + ".noaudio.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path, "-map", "0:v", "-c:v", "copy", "-an", "-movflags",
                    "+faststart", tmp], check=True, capture_output=True)
    os.replace(tmp, path)
    return path


class FalVideoProvider:
    """Yerel sağlayıcıyla aynı arayüz: `engine`, `estimate(req)`, `generate(req)`, `upload(path)`. Video kendisi
    puanlanır (önce görsel yok); bütün yeniden denemeler kuyruk katmanındadır (`whole_wave` yok, tek klip)."""

    image_first = False
    key_name = "fal_key"

    def __init__(self, engine: str):
        if engine not in fal_plan.FAL_ENGINES:
            raise ValueError(f"{engine} bir fal.ai video motoru değil")
        self.engine = engine
        self._client = None

    def client(self):
        if self._client is None:
            self._client = fal_queue.client()
        return self._client

    def resolution(self, requested: str) -> str:
        return fal_plan.norm_resolution(self.engine, requested)

    def mode(self, req: GenRequest) -> str:
        if self.engine in fal_plan.I2V_ONLY:
            return "i2v"
        return req.extra.get("fal_mode") or ("i2v" if req.image_url else "ref")

    def images(self, req: GenRequest) -> int:
        if req.extra.get("fal_images") is not None:
            return int(req.extra["fal_images"])
        refs = len(req.extra.get("fal_refs") or []) if self.mode(req) == "ref" else 0
        return refs + int(bool(req.image_url)) + int(bool(req.extra.get("last_frame")))

    def estimate(self, req: GenRequest) -> float:
        return fal_plan.cost(self.engine, self.resolution(req.resolution), req.duration_s, self.images(req), self.mode(req),
                             bool(req.extra.get("fal_draft")))["reserve_usd"]

    def upload(self, path: str) -> str:
        guard("fal_key")
        url, _, _ = fal_queue.upload_cached(self.client(), path)
        return url

    def arguments(self, req: GenRequest) -> tuple[str, dict]:
        mode = self.mode(req)
        refs = [fal_queue.upload_key(self.client(), key) for key in req.extra.get("fal_refs") or []] \
            if mode == "ref" else []
        sounds = [fal_queue.upload_key(self.client(), key) for key in req.extra.get("fal_audio_refs") or []] \
            if mode == "ref" else []
        return fal_plan.body(self.engine, mode, prompt=req.prompt, duration_s=req.duration_s,
                             resolution_=self.resolution(req.resolution),
                             audio_=bool(req.extra.get("fal_audio", settings.seedance25_audio)), seed=req.seed,
                             image_url=req.image_url, end_image_url=req.extra.get("last_frame"), reference_urls=refs,
                             draft_=bool(req.extra.get("fal_draft")), audio_urls=sounds)

    def billed(self, req: GenRequest, args: dict, measured: dict) -> dict:
        """İşlenen tutar ve tahmini faturalanan birimler."""
        res = self.resolution(req.resolution)
        if self.engine == "seedance25":
            size = (measured["width"], measured["height"]) if measured.get("width") else None
            seconds = measured.get("duration_s") or fal_plan.billed_seconds(self.engine, req.duration_s)
            c = pricing.seedance25_cost("480p" if args.get("draft") else res, seconds, size=size, measured=True)
            return {"usd": c["usd"], "tokens": c["billed_tokens"], "measured_tokens": c["tokens"],
                    "per_1k_tokens_usd": c["per_1k_tokens_usd"]}
        if self.engine == "flux3max":                  # video saniyesi başına: üretilen (ölçülen) süre, yoksa istenen
            seconds = measured.get("duration_s") or fal_plan.billed_seconds(self.engine, req.duration_s)
            c = pricing.flux3max_cost(res, max(float(seconds), fal_plan.billed_seconds(self.engine, req.duration_s)))
            return {"usd": c["usd"], "seconds": c["duration_s"], "per_second_usd": c["per_second_usd"]}
        c = pricing.h3max_cost(res, fal_plan.billed_seconds(self.engine, req.duration_s), fal_plan.images_in(args),
                               mode=self.mode(req))
        return {"usd": c["usd"], "units": c["units"], "reference_tokens": c["reference_tokens"]}

    def generate(self, req: GenRequest) -> GenResult:
        client = self.client()
        endpoint, args = self.arguments(req)
        scene_id, project_id = req.extra.get("scene_id"), req.extra.get("project_id")
        out = fal_queue.run(client, endpoint, args, timeout_s=fal_plan.TIMEOUT_S[self.engine], scene_id=scene_id,
                            project_id=project_id, info={"engine": self.engine, "mode": self.mode(req)})
        video = out.result.get("video") or {}
        if not video.get("url"):
            raise fal_queue.FalError(f"fal.ai yanıtında video yok ({out.request_id}): {list(out.result)}")
        path = fal_queue.download(video["url"], ".mp4", video.get("file_size"))
        if not req.extra.get("fal_audio"):             # modelin sesi istenmedi (ör. taslakta zorla açıldı): atılır
            strip_audio(path)
        measured = probe(path)
        billed = self.billed(req, args, measured)
        meta = {"endpoint": endpoint, "mode": self.mode(req), "request_id": out.request_id, "resumed": out.resumed,
                "elapsed_s": out.elapsed_s, "seed": out.result.get("seed"), "seed_sent": args.get("seed"),
                "expanded_prompt": out.result.get("expanded_prompt"), "draft_id": out.result.get("draft_id"),
                "timings": out.result.get("timings"), "billed": billed,
                "output": {**measured, "file_size": video.get("file_size"), "content_type": video.get("content_type")},
                "resolution": args.get("resolution"), "duration": args.get("duration"),
                "references": req.extra.get("fal_ref_names") or []}
        fal_queue._scene_request(scene_id, result={k: meta[k] for k in ("seed", "draft_id", "billed", "endpoint")},
                                 request_id=out.request_id)
        return GenResult(local_path=path, request_id=out.request_id, cost_usd=billed["usd"], engine=self.engine, meta=meta)


def complete_draft(draft_id: str, codec: str = "auto") -> dict:
    """Seedance 2.5 taslağını (draft: true, 480p) 7 gün içinde 1080p'ye tamamlar. Gövde; çağıran kuyruğa verir."""
    return {"draft_id": draft_id, "resolution": "1080p", "codec": codec}


def get_live_provider(engine: str) -> FalVideoProvider:
    guard("fal_key")                        # sağlayıcı kurulurken: engelliyse hiçbir varyasyon satırı ya da rezervasyon oluşmaz
    from app.live import require_package
    require_package("fal_client")
    return FalVideoProvider(engine)
