"""FFmpeg muksing: gerçek video ve ses ile ölçüm. Veritabanı gerektirmez."""
import subprocess

import pytest

from app.post.mux import AudioInput, build_command, mux
from tests.media_utils import band_db, loudness_lufs, make_tone, make_video, pcm, probe

RATE = 16000
VIDEO_S = 6.0


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    return make_video(tmp_path_factory.mktemp("v") / "v.mp4", seconds=VIDEO_S)


def loud_tone(path, freq, seconds, amplitude=0.5):
    """Gerçek konuşma gibi yüksek seviyeli ton (varsayılan sine çok kısık: 0,125)."""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
                    "-af", f"volume={amplitude / 0.125}", "-ac", "1", str(path)], check=True)
    return str(path)


def band(out, t0, t1, freq):
    return band_db(pcm(out, RATE), RATE, t0, t1, freq - 25, freq + 25)


# ---------- çıktı biçimi ----------

def test_output_has_one_video_and_one_stereo_aac_stream_and_the_video_is_copied(tmp_path, video):
    music = make_tone(tmp_path / "m.wav", 300, VIDEO_S)                 # mono girdi: stereo'ya çıkar
    out = mux(video, [], [AudioInput(music)], str(tmp_path / "o.mp4"))
    info, src = probe(out), probe(video)
    assert len(info["video"]) == 1 and len(info["audio"]) == 1
    audio = info["audio"][0]
    assert (audio["codec_name"], int(audio["channels"]), int(audio["sample_rate"])) == ("aac", 2, 48000)
    assert 185_000 < int(info["audio"][0].get("bit_rate", 192_000)) < 200_000
    assert info["video"][0]["codec_name"] == src["video"][0]["codec_name"] == "h264"
    assert info["video"][0]["nb_frames"] == src["video"][0]["nb_frames"]


def test_output_streams_start_with_the_moov_atom(tmp_path, video):
    out = mux(video, [], [AudioInput(make_tone(tmp_path / "m.wav", 300, VIDEO_S))], str(tmp_path / "o.mp4"))
    head = open(out, "rb").read(8192)
    assert 0 <= head.find(b"moov") < head.find(b"mdat")


@pytest.mark.parametrize("audio_seconds", [1.0, 6.0, 15.0])
def test_duration_always_equals_the_video_whatever_the_audio_length(tmp_path, video, audio_seconds):
    tone = make_tone(tmp_path / "a.wav", 440, audio_seconds)
    out = mux(video, [AudioInput(tone, 0.5)], [], str(tmp_path / "o.mp4"))
    assert probe(out)["duration"] == pytest.approx(VIDEO_S, abs=0.1)
    assert probe(out)["audio"][0]["duration"] and float(probe(out)["audio"][0]["duration"]) == pytest.approx(VIDEO_S, abs=0.15)


def test_silent_scene_copies_the_video_without_an_audio_stream(tmp_path, video):
    out = mux(video, [], [], str(tmp_path / "o.mp4"))
    info = probe(out)
    assert info["audio"] == [] and len(info["video"]) == 1
    assert info["video"][0]["nb_frames"] == probe(video)["video"][0]["nb_frames"]
    assert info["duration"] == pytest.approx(VIDEO_S, abs=0.1)


def test_dialogue_only_and_bed_only_both_work(tmp_path, video):
    tone = make_tone(tmp_path / "a.wav", 440, 2.0)
    for name, args in (("dialogue", ([AudioInput(tone, 1.0)], [])), ("bed", ([], [AudioInput(tone)]))):
        out = mux(video, *args, str(tmp_path / f"{name}.mp4"))
        assert len(probe(out)["audio"]) == 1 and probe(out)["duration"] == pytest.approx(VIDEO_S, abs=0.1)


# ---------- zamanlama ve kazanç ----------

def test_dialogue_starts_at_its_offset(tmp_path, video):
    out = mux(video, [AudioInput(loud_tone(tmp_path / "d.wav", 1000, 1.5), 2.5)], [], str(tmp_path / "o.mp4"))
    assert band(out, 0.2, 2.3, 1000) < band(out, 2.7, 3.9, 1000) - 60       # ofsetten önce sessiz, sonra var


def test_several_dialogues_are_all_audible_at_their_own_times(tmp_path, video):
    a, b = loud_tone(tmp_path / "a.wav", 600, 1.0), loud_tone(tmp_path / "b.wav", 1400, 1.0)
    out = mux(video, [AudioInput(a, 0.5), AudioInput(b, 3.0)], [], str(tmp_path / "o.mp4"))
    assert band(out, 0.7, 1.3, 600) > band(out, 0.7, 1.3, 1400) + 40
    assert band(out, 3.2, 3.8, 1400) > band(out, 3.2, 3.8, 600) + 40


def test_gain_in_db_is_applied_per_channel(tmp_path, video):
    a, b = make_tone(tmp_path / "a.wav", 300, VIDEO_S), make_tone(tmp_path / "b.wav", 700, VIDEO_S)
    out = mux(video, [], [AudioInput(a, 0.0, 0.0), AudioInput(b, 0.0, -12.0)], str(tmp_path / "o.mp4"))
    assert band(out, 1, 5, 300) - band(out, 1, 5, 700) == pytest.approx(12.0, abs=1.5)


# ---------- ducking ----------

def test_music_ducks_under_dialogue_and_comes_back_afterwards(tmp_path, video):
    music = make_tone(tmp_path / "m.wav", 300, VIDEO_S)
    dialogue = loud_tone(tmp_path / "d.wav", 1000, 2.0)
    out = mux(video, [AudioInput(dialogue, 2.0)], [AudioInput(music)], str(tmp_path / "o.mp4"))
    before, during, after = band(out, 0.5, 1.8, 300), band(out, 2.3, 3.7, 300), band(out, 4.5, 5.8, 300)
    assert during < before - 6                          # diyalog varken müzik bastırılır
    assert after > before - 2.5                         # diyalog bitince geri gelir: müzik son replikle susmaz
    assert band(out, 2.3, 3.7, 1000) > band(out, 0.2, 1.8, 1000) + 60


def test_without_dialogue_the_music_is_not_ducked(tmp_path, video):
    music = make_tone(tmp_path / "m.wav", 300, VIDEO_S)
    out = mux(video, [], [AudioInput(music)], str(tmp_path / "o.mp4"))
    assert abs(band(out, 0.5, 1.8, 300) - band(out, 2.3, 3.7, 300)) < 1.0


# ---------- ses yüksekliği ----------

@pytest.mark.parametrize("target", [-23.0, -16.0])
def test_loudness_is_normalised_to_the_requested_integrated_level(tmp_path, video, target):
    out = mux(video, [AudioInput(loud_tone(tmp_path / "d.wav", 800, 4.0), 0.5)], [AudioInput(make_tone(tmp_path / "m.wav", 300, VIDEO_S))],
              str(tmp_path / "o.mp4"), loudness_lufs=target)
    assert loudness_lufs(out) == pytest.approx(target, abs=1.5)


def test_very_quiet_and_very_loud_inputs_end_up_at_the_same_level(tmp_path, video):
    quiet = mux(video, [AudioInput(loud_tone(tmp_path / "q.wav", 800, 4.0, amplitude=0.02), 0.5)], [], str(tmp_path / "q.mp4"))
    loud = mux(video, [AudioInput(loud_tone(tmp_path / "l.wav", 800, 4.0, amplitude=0.9), 0.5)], [], str(tmp_path / "l.mp4"))
    assert loudness_lufs(quiet) == pytest.approx(loudness_lufs(loud), abs=2.0)


# ---------- komut ----------

def test_command_wires_every_stage_of_the_filter_graph(tmp_path, video):
    d, m = make_tone(tmp_path / "d.wav", 440, 1.0), make_tone(tmp_path / "m.wav", 300, 1.0)
    cmd = build_command(video, [AudioInput(d, 1.5, 2.0)], [AudioInput(m, 0.0, -6.0)], "out.mp4", -23.0)
    graph = cmd[cmd.index("-filter_complex") + 1]
    for piece in ("aresample=48000", "aformat=channel_layouts=stereo", "adelay=delays=1500:all=1", "volume=2.0dB",
                  "volume=-6.0dB", "apad=whole_dur=6.000", "sidechaincompress", "loudnorm=I=-23.0:TP=-1.5:LRA=11"):
        assert piece in graph, piece
    assert cmd[cmd.index("-c:v") + 1] == "copy" and cmd[cmd.index("-c:a") + 1] == "aac" and cmd[cmd.index("-b:a") + 1] == "192k"
    assert cmd[cmd.index("-t") + 1] == "6.000"


def test_sidechain_only_appears_when_there_are_both_dialogue_and_bed(tmp_path, video):
    tone = make_tone(tmp_path / "t.wav", 440, 1.0)
    only_dialogue = " ".join(build_command(video, [AudioInput(tone)], [], "o.mp4"))
    only_bed = " ".join(build_command(video, [], [AudioInput(tone)], "o.mp4"))
    both = " ".join(build_command(video, [AudioInput(tone)], [AudioInput(tone)], "o.mp4"))
    assert "sidechaincompress" not in only_dialogue and "sidechaincompress" not in only_bed and "sidechaincompress" in both


def test_silent_command_copies_video_and_drops_audio(tmp_path, video):
    cmd = build_command(video, [], [], "o.mp4")
    assert "-an" in cmd and "-filter_complex" not in cmd and cmd[cmd.index("-c:v") + 1] == "copy"


def test_a_missing_audio_file_fails_loudly(tmp_path, video):
    with pytest.raises(subprocess.CalledProcessError):
        mux(video, [AudioInput(str(tmp_path / "yok.wav"))], [], str(tmp_path / "o.mp4"))
