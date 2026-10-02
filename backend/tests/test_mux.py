"""FFmpeg miks: gerçek ses ölçümleriyle. Veritabanı gerektirmez."""
import numpy as np
import pytest

from app.post.mux import AudioInput, build_command, mux
from tests.media_utils import SR, db, loudness, make_sine, make_video, pcm, probe, rms, run, tone_amp


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    return make_video(tmp_path_factory.mktemp("v") / "video.mp4", 8)


def kinds(path):
    return [s["codec_type"] for s in probe(path)[0]]


# ---------- filtre grafiği (komut) ----------

def test_every_channel_is_48k_stereo_delayed_and_gained(video, tmp_path):
    cmd = build_command(video, [AudioInput("d.wav", 2.0, -3)], [AudioInput("m.wav", 0.5, -12)], str(tmp_path / "o.mp4"))
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "[1:a]aresample=48000,aformat=channel_layouts=stereo,adelay=delays=2000:all=1,volume=-3dB[d0]" in graph
    assert "[2:a]aresample=48000,aformat=channel_layouts=stereo,adelay=delays=500:all=1,volume=-12dB[b0]" in graph


def test_dialogue_ducks_the_bed_and_mix_is_loudness_normalised(video, tmp_path):
    cmd = build_command(video, [AudioInput("d.wav")], [AudioInput("m.wav")], str(tmp_path / "o.mp4"))
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "sidechaincompress" in graph and "loudnorm=I=-23.0:TP=-1.5:LRA=11" in graph
    assert graph.count("amix=") == 1 and "[dlgA][ducked]amix=inputs=2:normalize=0" in graph


def test_without_a_bed_or_without_dialogue_there_is_no_ducking(video, tmp_path):
    only_dialogue = build_command(video, [AudioInput("d.wav")], [], str(tmp_path / "o.mp4"))
    only_bed = build_command(video, [], [AudioInput("m.wav")], str(tmp_path / "o.mp4"))
    for cmd in (only_dialogue, only_bed):
        assert "sidechaincompress" not in cmd[cmd.index("-filter_complex") + 1]


def test_dialogue_lines_and_bed_tracks_are_mixed_separately(video, tmp_path):
    cmd = build_command(video, [AudioInput("a.wav"), AudioInput("b.wav")], [AudioInput("m.wav"), AudioInput("n.wav")],
                        str(tmp_path / "o.mp4"))
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "[d0][d1]amix=inputs=2:normalize=0:duration=longest,apad=whole_dur=8.000[dlg]" in graph
    assert "[b0][b1]amix=inputs=2:normalize=0:duration=longest,apad=whole_dur=8.000[bed]" in graph


def test_output_codec_arguments(video, tmp_path):
    cmd = build_command(video, [AudioInput("d.wav")], [], str(tmp_path / "o.mp4"))
    joined = " ".join(cmd)
    assert "-c:v copy" in joined and "-c:a aac" in joined and "-b:a 192k" in joined and "+faststart" in joined
    assert "-t 8.0" in joined or "-t 7.9" in joined


# ---------- gerçek miks ----------

def test_silent_mux_copies_the_video_without_an_audio_stream(video, tmp_path):
    out = mux(video, [], [], str(tmp_path / "silent.mp4"))
    assert kinds(out) == ["video"]
    assert probe(out)[0][0]["codec_name"] == "h264" and probe(out)[0][0]["nb_frames"] == probe(video)[0][0]["nb_frames"]


def test_output_has_one_video_and_one_aac_48k_stereo_stream(video, tmp_path):
    out = mux(video, [AudioInput(make_sine(tmp_path / "d.wav", 1000, 2))], [], str(tmp_path / "o.mp4"))
    audio = [s for s in probe(out)[0] if s["codec_type"] == "audio"][0]
    assert kinds(out) == ["video", "audio"]
    assert (audio["codec_name"], audio["sample_rate"], audio["channels"]) == ("aac", "48000", 2)


def test_audio_is_encoded_at_192k(video, tmp_path):
    run("-f", "lavfi", "-i", "anoisesrc=d=8:amplitude=0.3", "-ac", "1", str(tmp_path / "n.wav"))     # geniş bantlı içerik
    out = mux(video, [], [AudioInput(str(tmp_path / "n.wav"))], str(tmp_path / "o.mp4"))
    assert 150_000 < int([s for s in probe(out)[0] if s["codec_type"] == "audio"][0]["bit_rate"]) < 230_000


def test_video_stream_is_copied_not_reencoded(video, tmp_path):
    out = mux(video, [AudioInput(make_sine(tmp_path / "d.wav", 1000, 2))], [], str(tmp_path / "o.mp4"))
    v_in, v_out = probe(video)[0][0], [s for s in probe(out)[0] if s["codec_type"] == "video"][0]
    assert (v_in["codec_name"], v_in["pix_fmt"], v_in["nb_frames"]) == (v_out["codec_name"], v_out["pix_fmt"], v_out["nb_frames"])


@pytest.mark.parametrize("audio_seconds", [2, 30])
def test_duration_always_equals_the_video(video, tmp_path, audio_seconds):
    music = make_sine(tmp_path / "m.wav", 440, audio_seconds)
    out = mux(video, [], [AudioInput(music)], str(tmp_path / "o.mp4"))
    assert abs(probe(out)[1] - probe(video)[1]) < 0.15


def audio_duration(path):
    return float([s for s in probe(path)[0] if s["codec_type"] == "audio"][0]["duration"])


@pytest.mark.parametrize("name", ["dialogue only", "bed only", "dialogue and bed"])
def test_audio_stream_covers_the_whole_video(video, tmp_path, name):
    """Regresyon: ses akışı videodan kısa kalıyordu; -c copy ile birleştirilen kliplerde ses/görüntü kayardı."""
    speech, music = make_sine(tmp_path / "d.wav", 1000, 2), make_sine(tmp_path / "m.wav", 440, 3)
    dialogue = [AudioInput(speech)] if "dialogue" in name else []
    bed = [AudioInput(music)] if "bed" in name or "and" in name else []
    out = mux(video, dialogue, bed, str(tmp_path / "o.mp4"))
    assert audio_duration(out) == pytest.approx(probe(video)[1], abs=0.1)


def test_music_keeps_playing_after_the_last_dialogue_line(video, tmp_path):
    """Regresyon: sidechaincompress yan zincir (diyalog) bitince çıktıyı kesiyor, müzik son replikle susuyordu."""
    speech, music = make_sine(tmp_path / "d.wav", 1000, 2), make_sine(tmp_path / "m.wav", 440, 8)
    out = mux(video, [AudioInput(speech)], [AudioInput(music)], str(tmp_path / "o.mp4"))
    assert tone_amp(pcm(out, 4.5, 5.5), 440) > 0.05


def test_offset_delays_the_dialogue(video, tmp_path):
    out = mux(video, [AudioInput(make_sine(tmp_path / "d.wav", 1000, 2), offset_s=3.0)], [], str(tmp_path / "o.mp4"))
    before, during = pcm(out, 0.2, 2.8), pcm(out, 3.3, 4.7)
    assert np.abs(before).max() < 0.002 and rms(during) > 0.02


def test_two_dialogue_lines_play_one_after_another(video, tmp_path):
    first, second = make_sine(tmp_path / "a.wav", 600, 1.5), make_sine(tmp_path / "b.wav", 1400, 1.5)
    out = mux(video, [AudioInput(first, 0.5), AudioInput(second, 3.5)], [], str(tmp_path / "o.mp4"))
    early, late = pcm(out, 0.8, 1.8), pcm(out, 3.8, 4.8)
    assert tone_amp(early, 600) > 8 * tone_amp(early, 1400) and tone_amp(late, 1400) > 8 * tone_amp(late, 600)


def test_gain_db_scales_each_track(video, tmp_path):
    loud, quiet = make_sine(tmp_path / "a.wav", 440, 8), make_sine(tmp_path / "b.wav", 880, 8)
    out = mux(video, [], [AudioInput(loud, 0, 0), AudioInput(quiet, 0, -12)], str(tmp_path / "o.mp4"))
    x = pcm(out, 2, 6)
    assert db(tone_amp(x, 880) / tone_amp(x, 440)) == pytest.approx(-12, abs=2)


def test_dialogue_ducks_the_music(video, tmp_path):
    speech, music = make_sine(tmp_path / "d.wav", 1000, 2, volume=0.8), make_sine(tmp_path / "m.wav", 440, 8)
    ducked = mux(video, [AudioInput(speech, 0.0)], [AudioInput(music)], str(tmp_path / "ducked.mp4"))
    plain = mux(video, [], [AudioInput(music)], str(tmp_path / "plain.mp4"))

    def music_drop(path):               # konuşma sırasındaki / konuşma bittikten sonraki müzik genliği
        return db(tone_amp(pcm(path, 0.6, 1.6), 440) / tone_amp(pcm(path, 4.5, 5.5), 440))

    assert music_drop(plain) > -1.5                 # diyalog yoksa müzik sabit
    assert music_drop(ducked) < -10                 # eşik 0.05, oran 8: diyalog varken müzik ~18 dB bastırılır


@pytest.mark.parametrize("target", [-23.0, -16.0])
def test_loudness_is_normalised_to_the_requested_level(video, tmp_path, target):
    speech, music = make_sine(tmp_path / "d.wav", 300, 6, volume=0.3), make_sine(tmp_path / "m.wav", 500, 8, volume=0.1)
    out = mux(video, [AudioInput(speech, 0.5)], [AudioInput(music)], str(tmp_path / "o.mp4"), loudness_lufs=target)
    assert loudness(out) == pytest.approx(target, abs=1.5)
