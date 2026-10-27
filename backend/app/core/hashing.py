"""İçerik özetleri: aynı girdi, hangi süreçte ve hangi makinede olursa olsun aynı sonucu verir.

Python'un yerleşik hash() fonksiyonu kullanılmaz: str için süreç başına rastgele tuzlanır (PYTHONHASHSEED).
"""
import hashlib
import json


def stable_hash(obj) -> str:
    """Anahtarları sıralı JSON'un sha256'sı, ilk 32 karakter. UUID/Decimal gibi değerler str'e çevrilir."""
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def seed_for(*parts) -> int:
    """Parçalardan tekrarlanabilir 31-bit seed (üretim motorlarının çoğu işaretli 32-bit tam sayı bekler)."""
    return int(stable_hash(list(parts))[:8], 16) & 0x7FFFFFFF


def visual_fingerprint(scene, characters, assets=(), style_version: int | None = None) -> str:
    """Görüntüyü belirleyen her şey: istem, kamera, motor, süre, karakterlerin kimliği, bağlı varlıkların sürümü ve
    projenin etkin stil kılavuzu sürümü.

    prompt_revision bilinçli olarak dahil değil: düzeltme döngüsünün kendi istem değişiklikleri yönetmenin
    girdisi sayılmaz, yeniden render tetiklememelidir.

    Özetler her zaman veritabanından okunan satırlarla hesaplanmalı: REAL sütunlar (duration_s, lora_scale)
    4 baytlık kesinlikle saklanır, yazmadan önceki Python değeriyle hesaplanan özet farklı çıkabilir.

    Varlıklar (Faz 12) yalnızca varsa özete girer: bağlı varlığı olmayan sahnenin özeti öncekiyle aynı kalır ve
    yeniden render tetiklenmez. Varlığın durumu (onay, kilit) değil, sürümü girer: referans değişince sürüm artar.
    Stil kılavuzu (Faz 13) da aynı biçimde yalnızca etkin bir sürüm varsa girer; sürümler değişmez olduğu için numara yeter.

    Yöntem (Faz 15): dış çekimin görüntüsünü yalnızca yüklenen dosya belirler (özeti, dosyanın içerik özetidir);
    previs rehberli çekimde rehber dosyanın özeti de girer.

    MiniMax H3 koşulu: yönetmenin yüklediği ilk kare (özeti) ve Ref2VA seçimi yalnızca varsa girer; varsayılan FL2VA
    (storyboard karesinden) özeti değiştirmez. Storyboard karesi zaten bu özetten türer.
    """
    method = getattr(scene, "method", None) or "generate"
    if method == "external":
        return stable_hash({"external": scene.external_hash})
    # fal.ai motorları (H3 Max, Seedance 2.5) MiniMax H3 ile aynı kare zincirini ve koşulu kullanır: özette "h3" sayılır.
    # Böylece H3'ten H3 Max'e geçiş ya da taslaktan "Seedance 2.5 ile yükselt" aynı kareyi ve referansları kullanır
    # (storyboard karesi yeniden çizilmez); motor değişince görüntü açıkça yeniden üretilir (force / yükseltme).
    engine = "h3" if scene.engine in ("h3max", "seedance25", "flux3max") else scene.engine
    spec = scene.prompt_spec
    if isinstance(spec, dict) and "override" in spec:      # yönetmen istemi (parça istemi) görüntüyü kendiliğinden yenilemez
        spec = {k: v for k, v in spec.items() if k != "override"}
    payload = {
        "prompt": spec,
        "camera": scene.camera,
        "engine": engine,
        "duration": scene.duration_s,
        "characters": sorted(
            [str(c.id), c.embedding_version, c.lora_url, c.lora_scale] for c in characters
        ),
    }
    if assets:
        payload["assets"] = sorted([str(a.id), a.version] for a in assets)
    if style_version is not None:
        payload["style"] = style_version
    if method == "previs_guide":
        payload["guide"] = scene.guide_hash
    if getattr(scene, "keyframe_hash", None):
        payload["keyframe"] = scene.keyframe_hash
    if getattr(scene, "camera_plan", None):             # kesme kamerası (30° kuralı): kare ve video istemine girer
        payload["camera_plan"] = scene.camera_plan
    from app.core.h3mode import effective_mode
    if (engine or "h3") == "h3" and effective_mode(scene, characters, assets) == "ref2va":
        payload["h3_mode"] = "ref2va"
    return stable_hash(payload)


GRADE_IDENTITY = {"exposure": 0.0, "contrast": 1.0, "saturation": 1.0, "gamma": 1.0, "temperature": 6500}


def grade_settings(raw: dict | None) -> dict:
    """Çekimin renk ayarı, eksikler etkisiz değerle; kayan nokta gürültüsü özeti değiştirmesin diye yuvarlanır."""
    merged = {**GRADE_IDENTITY, **(raw or {})}
    return {k: (int(round(merged[k])) if k == "temperature" else round(float(merged[k]), 3)) for k in GRADE_IDENTITY}


def film_grade_token(lut_hash: str | None, project_settings: dict | None) -> str | None:
    """Film geneli renk girdileri: LUT ve kare tutma (on twos, proje ayarı). Kare tutma kapalıyken LUT özeti aynen döner:
    eski renk özetleri değişmez. Açıkken her çekimin renk özeti değişir: yalnızca GRADE + MUX çalışır."""
    if not (project_settings or {}).get("on_twos"):
        return lut_hash
    return stable_hash({"lut": lut_hash, "hold": 2})


def grade_fingerprint(scene, lut_hash: str | None) -> str | None:
    """Renk aşamasını belirleyen her şey: çekimin ayarı ve film geneli LUT. Etkisiz ayar ve LUT yoksa None: GRADE
    aşaması hiç gerekmez (Faz 16 öncesi sahnelerin planı değişmez)."""
    grade = grade_settings(getattr(scene, "grade", None))
    if grade == GRADE_IDENTITY and not lut_hash:
        return None
    return stable_hash({"grade": grade, "lut": lut_hash})


def audio_fingerprint(tracks) -> str:
    """Yalnızca aktif kanallar: (tür, içerik özeti, başlangıç ofseti, kazanç). Sıra ve kayan nokta gürültüsü etkisiz."""
    return stable_hash(sorted(
        [t.kind, str(t.content_hash), round(float(t.start_offset_s), 3), round(float(t.gain_db), 3)]
        for t in tracks if t.is_active
    ))
