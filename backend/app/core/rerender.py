"""Stateless Re-render: hangi girdi grubu değiştiyse yalnızca onun aşağı akışı çalışır.

Sahne satırı son başarılı çalışmanın özetlerini (visual_hash, dialogue_hash, bed_hash) ve ürettiği
anahtarları (lipsync_video_key, final_video_key) tutar; plan bunların şimdiki girdilerle farkından çıkar.
"""
from dataclasses import dataclass
from enum import Flag, auto

from app.core.hashing import audio_fingerprint, visual_fingerprint


class Stage(Flag):
    NONE = 0
    VISUAL = auto()    # video üretimi + Quality Gate (en pahalı aşama)
    LIPSYNC = auto()   # diyalog sesi + dudak senkronu
    MUX = auto()       # FFmpeg miks ve muksing (yalnızca yerel işlem)


ALL = Stage.VISUAL | Stage.LIPSYNC | Stage.MUX


@dataclass(frozen=True)
class Fingerprints:
    visual: str
    dialogue: str    # yalnızca diyalog kanalları
    bed: str         # müzik, sfx ve ambiyans


def fingerprints(scene, characters, tracks, assets=()) -> Fingerprints:
    return Fingerprints(
        visual=visual_fingerprint(scene, characters, assets),
        dialogue=audio_fingerprint([t for t in tracks if t.kind == "dialogue"]),
        bed=audio_fingerprint([t for t in tracks if t.kind != "dialogue"]),
    )


def plan_rerender(scene, characters, tracks, assets=()) -> tuple[Stage, Fingerprints]:
    """Hangi aşamaların çalışacağını ve güncel özetleri döndürür (özetleri saklamak çağıranın işidir)."""
    fp = fingerprints(scene, characters, tracks, assets)
    if scene.visual_hash is None or fp.visual != scene.visual_hash:
        return ALL, fp
    if scene.lipsync_video_key is None or fp.dialogue != scene.dialogue_hash:
        return Stage.LIPSYNC | Stage.MUX, fp      # görüntü motoru çağrılmaz
    if scene.final_video_key is None or fp.bed != scene.bed_hash:
        return Stage.MUX, fp                      # yalnızca FFmpeg
    return Stage.NONE, fp


def stage_names(stages: Stage) -> list[str]:
    return [s.name for s in (Stage.VISUAL, Stage.LIPSYNC, Stage.MUX) if s in stages]
