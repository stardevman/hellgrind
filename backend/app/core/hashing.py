import hashlib
import json


def stable_hash(obj) -> str:
    """Süreçler ve makineler arasında aynı sonucu veren içerik özeti."""
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def seed_for(*parts) -> int:
    """Tekrarlanabilir 31-bit seed."""
    return int(stable_hash(list(parts))[:8], 16) & 0x7FFFFFFF


def visual_fingerprint(scene, characters) -> str:
    # prompt_revision bilinçli olarak dahil değil: düzeltme döngüsünün kendi değişiklikleri
    # yönetmenin girdisi sayılmaz ve yeniden render tetiklememelidir.
    return stable_hash({
        "prompt": scene.prompt_spec,
        "camera": scene.camera,
        "engine": scene.engine,
        "duration": scene.duration_s,
        "chars": sorted((str(c.id), c.embedding_version, c.lora_url, c.lora_scale) for c in characters),
    })


def audio_fingerprint(tracks) -> str:
    return stable_hash(sorted(
        (t.kind, t.content_hash, round(float(t.start_offset_s), 3), round(float(t.gain_db), 2))
        for t in tracks if t.is_active
    ))
