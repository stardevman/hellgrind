import hashlib
import os
import pathlib
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest

from app.core.hashing import audio_fingerprint, seed_for, stable_hash, visual_fingerprint
from app.core.rerender import ALL, Stage, fingerprints, plan_rerender, stage_names

BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _scene(**kw):
    base = dict(prompt_spec={"prompt": "a"}, camera={"shot": "medium"}, engine="fal_wan", duration_s=5.0,
                visual_hash=None, dialogue_hash=None, bed_hash=None, lipsync_video_key=None, final_video_key=None)
    return NS(**{**base, **kw})


def _track(kind, h, active=True, off=0.0, gain=0.0):
    return NS(kind=kind, content_hash=h, is_active=active, start_offset_s=off, gain_db=gain)


def _done(scene, chars, tracks):
    fp = fingerprints(scene, chars, tracks)
    scene.visual_hash, scene.dialogue_hash, scene.bed_hash = fp.visual, fp.dialogue, fp.bed
    scene.lipsync_video_key, scene.final_video_key = "l.mp4", "f.mp4"
    return scene


def test_stable_hash_is_order_independent():
    assert stable_hash({"a": 1, "b": 2}) == stable_hash({"b": 2, "a": 1})
    assert seed_for("x", 1) == seed_for("x", 1) != seed_for("x", 2)


def test_new_scene_needs_everything():
    stages, _ = plan_rerender(_scene(), [], [])
    assert stages == ALL


def test_music_change_only_muxes():
    tracks = [_track("dialogue", "d1"), _track("music", "m1")]
    sc = _done(_scene(), [], tracks)
    stages, _ = plan_rerender(sc, [], [_track("dialogue", "d1"), _track("music", "m2")])
    assert stages == Stage.MUX
    assert stage_names(stages) == ["MUX"]


def test_dialogue_change_skips_video():
    tracks = [_track("dialogue", "d1")]
    sc = _done(_scene(), [], tracks)
    stages, _ = plan_rerender(sc, [], [_track("dialogue", "d2")])
    assert stages == Stage.LIPSYNC | Stage.MUX
    assert Stage.VISUAL not in stages


def test_prompt_change_rerenders_all():
    sc = _done(_scene(), [], [])
    sc.prompt_spec = {"prompt": "b"}
    assert plan_rerender(sc, [], [])[0] == ALL


def test_character_version_bump_rerenders_visual():
    ch = [NS(id="c1", embedding_version=1, lora_url="u", lora_scale=1.0)]
    sc = _done(_scene(), ch, [])
    ch2 = [NS(id="c1", embedding_version=2, lora_url="u", lora_scale=1.0)]
    assert plan_rerender(sc, ch2, [])[0] == ALL


def test_nothing_changed():
    tracks = [_track("dialogue", "d1"), _track("music", "m1")]
    sc = _done(_scene(), [], tracks)
    assert plan_rerender(sc, [], tracks)[0] == Stage.NONE


def test_inactive_tracks_ignored():
    assert audio_fingerprint([_track("music", "x", active=False)]) == audio_fingerprint([])


# ---------- özetler ----------

def test_stable_hash_is_truncated_sha256_of_canonical_json():
    assert stable_hash({"b": [1, 2], "a": 1}) == hashlib.sha256(b'{"a":1,"b":[1,2]}').hexdigest()[:32]


def test_seed_is_31_bit_and_varies_with_every_part():
    seeds = {seed_for("scene", "vh", "probe", wave, idx) for wave in range(5) for idx in range(50)}
    assert len(seeds) == 250
    assert all(0 <= s < 2**31 for s in seeds)


def test_hashes_do_not_depend_on_pythonhashseed():
    """Python'un hash() fonksiyonu kullanılsaydı her süreçte farklı sonuç çıkardı."""
    code = "from app.core.hashing import seed_for, stable_hash; print(seed_for('a', 1, 'probe'), stable_hash({'k': ['v', 2]}))"
    outputs = {
        subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=BACKEND,
                       env={**os.environ, "PYTHONHASHSEED": hs, "PYTHONPATH": str(BACKEND)}).stdout
        for hs in ("1", "2", "random")
    }
    assert len(outputs) == 1


def test_prompt_revision_is_not_part_of_visual_fingerprint():
    assert visual_fingerprint(_scene(prompt_revision=0), []) == visual_fingerprint(_scene(prompt_revision=3), [])


@pytest.mark.parametrize("change", [
    {"prompt_spec": {"prompt": "b"}}, {"camera": {"shot": "wide"}}, {"engine": "seedance"}, {"duration_s": 6.0},
])
def test_visual_fingerprint_reacts_to_shot_inputs(change):
    assert visual_fingerprint(_scene(**change), []) != visual_fingerprint(_scene(), [])


@pytest.mark.parametrize("field,value", [("lora_url", "u2"), ("lora_scale", 0.8), ("embedding_version", 2)])
def test_visual_fingerprint_reacts_to_character_identity(field, value):
    base = dict(id="c1", embedding_version=1, lora_url="u", lora_scale=1.0)
    assert visual_fingerprint(_scene(), [NS(**base)]) != visual_fingerprint(_scene(), [NS(**{**base, field: value})])


def test_visual_fingerprint_ignores_character_order():
    a = NS(id="a", embedding_version=1, lora_url=None, lora_scale=1.0)
    b = NS(id="b", embedding_version=1, lora_url="u", lora_scale=1.0)
    assert visual_fingerprint(_scene(), [a, b]) == visual_fingerprint(_scene(), [b, a])


def test_dialogue_and_bed_are_fingerprinted_separately():
    tracks = [_track("dialogue", "d1"), _track("music", "m1"), _track("sfx", "s1"), _track("ambience", "a1")]
    only_dialogue = [_track("dialogue", "d1")]
    fp, fp_dialogue = fingerprints(_scene(), [], tracks), fingerprints(_scene(), [], only_dialogue)
    assert fp.dialogue == fp_dialogue.dialogue
    assert fp.bed != fp_dialogue.bed


# ---------- planlayıcı kenar durumları ----------

def test_music_gain_change_only_muxes():
    sc = _done(_scene(), [], [_track("dialogue", "d1"), _track("music", "m1", gain=-8)])
    stages, _ = plan_rerender(sc, [], [_track("dialogue", "d1"), _track("music", "m1", gain=-12)])
    assert stages == Stage.MUX


def test_sfx_and_ambience_belong_to_the_bed():
    sc = _done(_scene(), [], [_track("dialogue", "d1")])
    for kind in ("sfx", "ambience"):
        stages, _ = plan_rerender(sc, [], [_track("dialogue", "d1"), _track(kind, "x")])
        assert stages == Stage.MUX


def test_dialogue_timing_change_replans_lipsync_and_mux():
    sc = _done(_scene(), [], [_track("dialogue", "d1", off=0.0)])
    stages, _ = plan_rerender(sc, [], [_track("dialogue", "d1", off=0.5)])
    assert stages == Stage.LIPSYNC | Stage.MUX


def test_missing_lipsync_video_replans_lipsync_and_mux():
    sc = _done(_scene(), [], [_track("dialogue", "d1")])
    sc.lipsync_video_key = None
    assert plan_rerender(sc, [], [_track("dialogue", "d1")])[0] == Stage.LIPSYNC | Stage.MUX


def test_missing_final_video_only_muxes():
    sc = _done(_scene(), [], [_track("dialogue", "d1")])
    sc.final_video_key = None
    assert plan_rerender(sc, [], [_track("dialogue", "d1")])[0] == Stage.MUX


def test_visual_change_wins_over_simultaneous_audio_change():
    sc = _done(_scene(), [], [_track("dialogue", "d1"), _track("music", "m1")])
    sc.camera = {"shot": "wide"}
    assert plan_rerender(sc, [], [_track("dialogue", "d2"), _track("music", "m2")])[0] == ALL


def test_plan_returns_current_fingerprints_for_the_caller_to_store():
    tracks = [_track("dialogue", "d1"), _track("music", "m1")]
    stages, fp = plan_rerender(_scene(), [], tracks)
    assert fp == fingerprints(_scene(), [], tracks) and stages == ALL
