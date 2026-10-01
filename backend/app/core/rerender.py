"""Stateless Re-render: hangi girdi grubu değiştiyse yalnızca onun aşağı akışı çalışır."""
from dataclasses import dataclass
from enum import Flag, auto

from app.core.hashing import audio_fingerprint, visual_fingerprint


class Stage(Flag):
    NONE = 0
    VISUAL = auto()    # video üretimi + Quality Gate
    LIPSYNC = auto()   # ses üretimi + dudak senkronu
    MUX = auto()       # FFmpeg miks + muksing


ALL = Stage.VISUAL | Stage.LIPSYNC | Stage.MUX


@dataclass(frozen=True)
class Fingerprints:
    visual: str
    dialogue: str
    bed: str


def fingerprints(scene, characters, tracks) -> Fingerprints:
    return Fingerprints(
        visual=visual_fingerprint(scene, characters),
        dialogue=audio_fingerprint([t for t in tracks if t.kind == "dialogue"]),
        bed=audio_fingerprint([t for t in tracks if t.kind != "dialogue"]),
    )


def plan_rerender(scene, characters, tracks) -> tuple[Stage, Fingerprints]:
    fp = fingerprints(scene, characters, tracks)
    if scene.visual_hash is None or fp.visual != scene.visual_hash:
        return ALL, fp
    if scene.lipsync_video_key is None or fp.dialogue != scene.dialogue_hash:
        return Stage.LIPSYNC | Stage.MUX, fp      # video motoru tetiklenmez
    if scene.final_video_key is None or fp.bed != scene.bed_hash:
        return Stage.MUX, fp                      # yalnızca FFmpeg
    return Stage.NONE, fp


def stage_names(stages: Stage) -> list[str]:
    return [s.name for s in (Stage.VISUAL, Stage.LIPSYNC, Stage.MUX) if s in stages]
