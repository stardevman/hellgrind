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
    """
    payload = {
        "prompt": scene.prompt_spec,
        "camera": scene.camera,
        "engine": scene.engine,
        "duration": scene.duration_s,
        "characters": sorted(
            [str(c.id), c.embedding_version, c.lora_url, c.lora_scale] for c in characters
        ),
    }
    if assets:
        payload["assets"] = sorted([str(a.id), a.version] for a in assets)
    if style_version is not None:
        payload["style"] = style_version
    return stable_hash(payload)


def audio_fingerprint(tracks) -> str:
    """Yalnızca aktif kanallar: (tür, içerik özeti, başlangıç ofseti, kazanç). Sıra ve kayan nokta gürültüsü etkisiz."""
    return stable_hash(sorted(
        [t.kind, str(t.content_hash), round(float(t.start_offset_s), 3), round(float(t.gain_db), 3)]
        for t in tracks if t.is_active
    ))
