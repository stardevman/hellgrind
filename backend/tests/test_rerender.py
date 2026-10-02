"""Özetler ve yeniden render planlayıcısı. Veritabanı gerektirmez."""
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


def scene(**kw):
    base = dict(prompt_spec={"prompt": "a"}, camera={"shot": "medium"}, engine="fal_wan", duration_s=5.0,
                prompt_revision=0, visual_hash=None, dialogue_hash=None, bed_hash=None,
                lipsync_video_key=None, final_video_key=None)
    return NS(**{**base, **kw})


def track(kind, content_hash, active=True, offset=0.0, gain=0.0):
    return NS(kind=kind, content_hash=content_hash, is_active=active, start_offset_s=offset, gain_db=gain)


def character(id="c1", embedding_version=1, lora_url="u", lora_scale=1.0):
    return NS(id=id, embedding_version=embedding_version, lora_url=lora_url, lora_scale=lora_scale)


def finished(sc, chars, tracks):
    """Sahneyi, bu girdilerle başarıyla tamamlanmış gibi işaretler."""
    fp = fingerprints(sc, chars, tracks)
    sc.visual_hash, sc.dialogue_hash, sc.bed_hash = fp.visual, fp.dialogue, fp.bed
    sc.lipsync_video_key, sc.final_video_key = "lipsync/x.mp4", "final/x.mp4"
    return sc


# ---------- stable_hash / seed_for ----------

def test_stable_hash_is_truncated_sha256_of_sorted_compact_json():
    assert stable_hash({"b": [1, 2], "a": 1}) == hashlib.sha256(b'{"a":1,"b":[1,2]}').hexdigest()[:32]
    assert len(stable_hash("x")) == 32


def test_stable_hash_ignores_key_order_but_not_list_order():
    assert stable_hash({"a": 1, "b": {"x": 1, "y": 2}}) == stable_hash({"b": {"y": 2, "x": 1}, "a": 1})
    assert stable_hash([1, 2]) != stable_hash([2, 1])


def test_seed_is_31_bit_repeatable_and_depends_on_every_part():
    assert seed_for("s", "vh", "probe", 0, 3) == seed_for("s", "vh", "probe", 0, 3)
    seeds = {seed_for("scene", "vh", tier, wave, idx) for tier in ("probe", "full")
             for wave in range(5) for idx in range(50)}
    assert len(seeds) == 500
    assert all(0 <= s < 2**31 for s in seeds)


def test_hashes_do_not_depend_on_pythonhashseed():
    """hash() kullanılsaydı her süreçte farklı sonuç çıkardı."""
    code = ("from app.core.hashing import seed_for, stable_hash; "
            "print(seed_for('a', 1, 'probe'), stable_hash({'k': ['v', 2], 'ş': 'İ'}))")
    outputs = {
        subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=BACKEND,
                       env={**os.environ, "PYTHONHASHSEED": hs, "PYTHONPATH": str(BACKEND)}).stdout
        for hs in ("1", "2", "random")
    }
    assert len(outputs) == 1


# ---------- özetler ----------

def test_prompt_revision_is_not_part_of_the_visual_fingerprint():
    assert visual_fingerprint(scene(prompt_revision=0), []) == visual_fingerprint(scene(prompt_revision=3), [])


@pytest.mark.parametrize("change", [
    {"prompt_spec": {"prompt": "b"}}, {"camera": {"shot": "wide"}}, {"engine": "seedance"}, {"duration_s": 6.0},
])
def test_visual_fingerprint_reacts_to_every_shot_input(change):
    assert visual_fingerprint(scene(**change), []) != visual_fingerprint(scene(), [])


@pytest.mark.parametrize("field,value", [("lora_url", "u2"), ("lora_scale", 0.8), ("embedding_version", 2), ("id", "c2")])
def test_visual_fingerprint_reacts_to_character_identity(field, value):
    assert (visual_fingerprint(scene(), [character()])
            != visual_fingerprint(scene(), [character(**{field: value})]))


def test_visual_fingerprint_ignores_character_order_and_unrelated_character_fields():
    a, b = character("a", lora_url=None), character("b")
    assert visual_fingerprint(scene(), [a, b]) == visual_fingerprint(scene(), [b, a])
    noisy = NS(**vars(character()), name="Kerem", voice_id="v1")
    assert visual_fingerprint(scene(), [noisy]) == visual_fingerprint(scene(), [character()])


def test_audio_fingerprint_covers_kind_hash_offset_and_gain_of_active_tracks_only():
    base = audio_fingerprint([track("music", "m1", offset=0.5, gain=-8)])
    assert base != audio_fingerprint([track("sfx", "m1", offset=0.5, gain=-8)])
    assert base != audio_fingerprint([track("music", "m2", offset=0.5, gain=-8)])
    assert base != audio_fingerprint([track("music", "m1", offset=0.6, gain=-8)])
    assert base != audio_fingerprint([track("music", "m1", offset=0.5, gain=-9)])
    assert audio_fingerprint([track("music", "x", active=False)]) == audio_fingerprint([])


def test_audio_fingerprint_ignores_track_order_and_float_noise():
    a, b = track("dialogue", "d1", offset=0.0), track("dialogue", "d2", offset=1.5)
    assert audio_fingerprint([a, b]) == audio_fingerprint([b, a])
    assert audio_fingerprint([track("music", "m", offset=1.5)]) == audio_fingerprint([track("music", "m", offset=1.5000001)])


def test_dialogue_and_bed_are_fingerprinted_separately():
    everything = [track("dialogue", "d1"), track("music", "m1"), track("sfx", "s1"), track("ambience", "a1")]
    only_dialogue = [track("dialogue", "d1")]
    fp, fp_dialogue = fingerprints(scene(), [], everything), fingerprints(scene(), [], only_dialogue)
    assert fp.dialogue == fp_dialogue.dialogue
    assert fp.bed != fp_dialogue.bed


# ---------- plan_rerender ----------

def test_scene_that_never_ran_needs_every_stage():
    assert plan_rerender(scene(), [], [])[0] == ALL


def test_nothing_changed_runs_nothing():
    tracks = [track("dialogue", "d1"), track("music", "m1")]
    stages, _ = plan_rerender(finished(scene(), [], tracks), [], tracks)
    assert stages == Stage.NONE and stage_names(stages) == []


def test_music_change_runs_only_mux():
    sc = finished(scene(), [], [track("dialogue", "d1"), track("music", "m1")])
    stages, _ = plan_rerender(sc, [], [track("dialogue", "d1"), track("music", "m2")])
    assert stages == Stage.MUX and stage_names(stages) == ["MUX"]


@pytest.mark.parametrize("kind", ["sfx", "ambience"])
def test_sfx_and_ambience_belong_to_the_bed(kind):
    sc = finished(scene(), [], [track("dialogue", "d1")])
    assert plan_rerender(sc, [], [track("dialogue", "d1"), track(kind, "x")])[0] == Stage.MUX


def test_music_gain_change_runs_only_mux():
    sc = finished(scene(), [], [track("music", "m1", gain=-8)])
    assert plan_rerender(sc, [], [track("music", "m1", gain=-12)])[0] == Stage.MUX


def test_dialogue_change_runs_lipsync_and_mux_but_not_visual():
    sc = finished(scene(), [], [track("dialogue", "d1")])
    stages, _ = plan_rerender(sc, [], [track("dialogue", "d2")])
    assert stages == Stage.LIPSYNC | Stage.MUX
    assert Stage.VISUAL not in stages


def test_dialogue_timing_change_counts_as_dialogue_change():
    sc = finished(scene(), [], [track("dialogue", "d1", offset=0.0)])
    assert plan_rerender(sc, [], [track("dialogue", "d1", offset=0.5)])[0] == Stage.LIPSYNC | Stage.MUX


def test_missing_lipsync_video_runs_lipsync_and_mux():
    tracks = [track("dialogue", "d1")]
    sc = finished(scene(), [], tracks)
    sc.lipsync_video_key = None
    assert plan_rerender(sc, [], tracks)[0] == Stage.LIPSYNC | Stage.MUX


def test_missing_final_video_runs_only_mux():
    tracks = [track("dialogue", "d1")]
    sc = finished(scene(), [], tracks)
    sc.final_video_key = None
    assert plan_rerender(sc, [], tracks)[0] == Stage.MUX


def test_character_version_bump_runs_every_stage():
    sc = finished(scene(), [character(embedding_version=1)], [])
    assert plan_rerender(sc, [character(embedding_version=2)], [])[0] == ALL


def test_prompt_change_runs_every_stage():
    sc = finished(scene(), [], [])
    sc.prompt_spec = {"prompt": "b"}
    assert plan_rerender(sc, [], [])[0] == ALL


def test_prompt_revision_bump_alone_runs_nothing():
    sc = finished(scene(), [], [])
    sc.prompt_revision = 2
    assert plan_rerender(sc, [], [])[0] == Stage.NONE


def test_visual_change_wins_over_simultaneous_audio_changes():
    sc = finished(scene(), [], [track("dialogue", "d1"), track("music", "m1")])
    sc.camera = {"shot": "wide"}
    assert plan_rerender(sc, [], [track("dialogue", "d2"), track("music", "m2")])[0] == ALL


def test_dialogue_and_music_changing_together_run_lipsync_and_mux_once():
    sc = finished(scene(), [], [track("dialogue", "d1"), track("music", "m1")])
    stages, _ = plan_rerender(sc, [], [track("dialogue", "d2"), track("music", "m2")])
    assert stages == Stage.LIPSYNC | Stage.MUX


def test_plan_returns_the_current_fingerprints_for_the_caller_to_store():
    tracks = [track("dialogue", "d1"), track("music", "m1")]
    stages, fp = plan_rerender(scene(), [], tracks)
    assert stages == ALL and fp == fingerprints(scene(), [], tracks)


def test_stage_names_are_in_pipeline_order():
    assert stage_names(ALL) == ["STORYBOARD", "VISUAL", "LIPSYNC", "MUX"]
    assert stage_names(Stage.NONE) == []
