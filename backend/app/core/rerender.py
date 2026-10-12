"""Stateless Re-render: hangi girdi grubu değiştiyse yalnızca onun aşağı akışı çalışır.

Sahne satırı son başarılı çalışmanın özetlerini (visual_hash, dialogue_hash, grade_hash, bed_hash) ve ürettiği
anahtarları (lipsync_video_key, graded_video_key, final_video_key) tutar; plan bunların şimdiki girdilerle farkından çıkar.
Aşama sırası: STORYBOARD -> VISUAL -> LIPSYNC -> GRADE -> MUX.
"""
from dataclasses import dataclass
from enum import Flag, auto

from app.core.hashing import audio_fingerprint, grade_fingerprint, visual_fingerprint


class Stage(Flag):
    NONE = 0
    STORYBOARD = auto()  # çekimin storyboard karesi (ücretsiz modda yerel; ücretli modda tek bir Flux karesi)
    VISUAL = auto()    # video üretimi + Quality Gate (en pahalı aşama)
    LIPSYNC = auto()   # diyalog sesi + dudak senkronu
    MUX = auto()       # FFmpeg miks ve muksing (yalnızca yerel işlem)
    GRADE = auto()     # renk: film geneli LUT + çekim ayarı (LIPSYNC ile MUX arasında çalışır; yalnızca yerel işlem)


ALL = Stage.STORYBOARD | Stage.VISUAL | Stage.LIPSYNC | Stage.MUX
PRODUCTION = Stage.VISUAL | Stage.LIPSYNC | Stage.MUX


@dataclass(frozen=True)
class Fingerprints:
    visual: str
    dialogue: str    # yalnızca diyalog kanalları
    bed: str         # müzik, sfx ve ambiyans
    grade: str | None = None   # renk ayarı + LUT; etkisizse None


def fingerprints(scene, characters, tracks, assets=(), style_version=None, lut_hash=None) -> Fingerprints:
    return Fingerprints(
        visual=visual_fingerprint(scene, characters, assets, style_version),
        dialogue=audio_fingerprint([t for t in tracks if t.kind == "dialogue"]),
        bed=audio_fingerprint([t for t in tracks if t.kind != "dialogue"]),
        grade=grade_fingerprint(scene, lut_hash),
    )


def board_stale(scene, fp: Fingerprints) -> bool:
    """Storyboard karesi yok ya da başka bir görsel özet için çizilmiş."""
    return getattr(scene, "board_key", None) is None or getattr(scene, "board_hash", None) != fp.visual


def plan_rerender(scene, characters, tracks, assets=(), style_version=None, lut_hash=None,
                  keep_visual: bool = False) -> tuple[Stage, Fingerprints]:
    """Hangi aşamaların çalışacağını ve güncel özetleri döndürür (özetleri saklamak çağıranın işidir).
    STORYBOARD yalnızca görüntü yeniden üretilecekse ve kare eskiyse planlanır: bitmiş bir sahne için kare istenmez.
    GRADE yalnızca etkisiz olmayan bir renk ayarı ya da LUT varsa planlanır; yalnızca renk değişince GRADE + MUX.
    keep_visual: mevcut klip korunur (seçim, ses, renk; eski stil): VISUAL planlanmaz (çağıran rafine #1'i denetler)."""
    fp = fingerprints(scene, characters, tracks, assets, style_version, lut_hash)
    grade = Stage.GRADE if fp.grade is not None else Stage.NONE
    external = (getattr(scene, "method", None) or "generate") == "external"
    # Dış çekim: storyboard ve görüntü üretimi yok; klip yüklenirken rafine #1 olarak kaydedilir (api/methods.py).
    if not external and not keep_visual and (scene.visual_hash is None or fp.visual != scene.visual_hash):
        return (ALL if board_stale(scene, fp) else PRODUCTION) | grade, fp
    if scene.lipsync_video_key is None or fp.dialogue != scene.dialogue_hash:
        return Stage.LIPSYNC | grade | Stage.MUX, fp      # görüntü motoru çağrılmaz
    if fp.grade != getattr(scene, "grade_hash", None) or (fp.grade and not getattr(scene, "graded_video_key", None)):
        return Stage.GRADE | Stage.MUX, fp        # yalnızca renk: görüntü ve dudak senkronu çalışmaz
    if scene.final_video_key is None or fp.bed != scene.bed_hash:
        return Stage.MUX, fp                      # yalnızca FFmpeg
    return Stage.NONE, fp


def stage_names(stages: Stage) -> list[str]:
    return [s.name for s in (Stage.STORYBOARD, Stage.VISUAL, Stage.LIPSYNC, Stage.GRADE, Stage.MUX) if s in stages]
