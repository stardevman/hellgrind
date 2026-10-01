from types import SimpleNamespace as NS

from app.core.hashing import audio_fingerprint, seed_for, stable_hash
from app.core.rerender import ALL, Stage, fingerprints, plan_rerender, stage_names


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
