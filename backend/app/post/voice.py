"""Ses-önce akışı: diyalog animasyondan önce kaydedilir; çekim süresi ve tempo sesten çıkar.

Replikler sekans ve çekim sırasıyla listelenir. Her repliğin sesi geçici (Okur, espeak ya da ElevenLabs sentezi) ya da final
(yüklenen kayıt) olabilir. Ölçülen süre, repliğin çekimdeki yeriyle karşılaştırılır: yer, ofsetinden sonraki repliğe ya da
çekimin sonundaki paya kadardır. Fark DRIFT_S'yi aşarsa uyarı verilir. Planlayıcı değişmez: görüntüsü üretilmiş çekimde
süre farkı yalnızca uyarıdır (CLAUDE.md Faz 3: diyalog değişince görüntü yeniden üretilmez).

Ses denetimi (Duyu-small, app/quality/asr.py): geçici ses üretilince ve final kayıt yüklenince repliğin sesi yerelde
yazıya dökülür ve metinle karşılaştırılır; sonuç kanalın params.check alanına yazılır (dosya anahtarı ve metinle
birlikte, böylece ses ya da metin değişince eskir). Uyuşmayan replik panelde amber rozetle görünür; kilidi engellemez.

Sekansın sesi kilitlenince görüntüsü henüz üretilmemiş çekimlerin süresi ve replik ofsetleri ölçülen sürelerden yeniden
hesaplanır: giriş payı + replikler (aralarında boşlukla) + çıkış payı (scenes.duration_source = "voice"; panelde
"sesten"). Kilitli sekansta replik değiştirilemez. Proje ayarı `voice_first` açıkken sesi kilitlenmemiş sekansta görüntü
üretimi başlamaz (api/plan.py).
"""
import datetime as dt
import json
import os
import subprocess
import tempfile

import numpy as np
from sqlalchemy import select

from app import storage
from app.core.series import episode_characters_q
from app.config import project_settings, settings
from app.core.hashing import stable_hash
from app.db import session_scope
from app.models import AudioTrack, Character, Project, Scene, Sequence, Variation

PAD_IN_S = 0.4         # ilk replik çekimin başından bu kadar sonra başlar (ingest.DIALOGUE_START_S ile aynı)
GAP_S = 0.35           # replikler arası boşluk (rules.LINE_GAP_S ile aynı)
PAD_OUT_S = 0.6        # son replikten sonra çekimin sonuna kalan pay (rules.LEAD_IN_S - PAD_IN_S)
DRIFT_S = 0.5          # ölçülen süre ile çekimdeki yer arasındaki fark bunu aşarsa uyarı
MIN_SHOT_S, MAX_SHOT_S = 2.0, 10.0      # sesten hesaplanan çekim süresinin sınırları (SceneIn ile aynı)
PEAKS = 96             # dalga formundaki tepe sayısı
DRIFT_TEXT = "Replik süresi çekimden belirgin farklı; görüntünün yeniden üretilmesi gerekebilir."
AUDIO_EXTENSIONS = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg")


class VoiceLocked(Exception):
    """Sekansın sesi kilitli: replik değiştirilemez (mesaj Türkçe)."""


class VoiceNotReady(Exception):
    """Kilit için her repliğin ölçülmüş sesi olmalı (mesaj Türkçe)."""


# ---------- süre hesabı (saf) ----------

def retime(durations: list[float]) -> tuple[list[float], float]:
    """Ölçülen replik sürelerinden ofsetler ve çekim süresi (sınırlanmadan): giriş payı + replikler + boşluklar + çıkış payı."""
    offsets, at = [], PAD_IN_S
    for d in durations:
        offsets.append(round(at, 2))
        at += float(d) + GAP_S
    end = (at - GAP_S) if durations else PAD_IN_S
    return offsets, round(end + PAD_OUT_S, 2)


def clamp_shot(seconds: float) -> float:
    return round(min(max(seconds, MIN_SHOT_S), MAX_SHOT_S), 2)


def slots(offsets: list[float], shot_s: float) -> list[float]:
    """Her repliğin çekimdeki yeri (sn): sonraki repliğin ofsetine (boşluk düşülerek) ya da çekimin sonundaki paya kadar."""
    out = []
    for i, start in enumerate(offsets):
        end = offsets[i + 1] - GAP_S if i + 1 < len(offsets) else float(shot_s) - PAD_OUT_S
        out.append(round(max(0.0, end - start), 2))
    return out


def drifts(measured: float | None, slot: float) -> bool:
    return measured is not None and abs(float(measured) - float(slot)) > DRIFT_S


def parse_line_file(name: str) -> tuple[str, int] | None:
    """"S001_SH01_2.wav" -> ("S001_SH01", 2): çekim kodu ve çekimdeki replik sırası (1'den). Tanınmazsa None."""
    import re
    m = re.fullmatch(r"(s\d{3}_sh\d{2})[_-](\d{1,2})\.(wav|mp3|m4a|aac|flac|ogg)", name.strip(), re.IGNORECASE)
    if not m or int(m.group(2)) < 1:
        return None
    return m.group(1).upper(), int(m.group(2))


# ---------- dalga formu ----------

def peaks_of(path: str, n: int = PEAKS) -> list[float]:
    """Sesin dalga formu: n parçanın her birinde en yüksek genlik (0-1, en yükseğe göre ölçekli)."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    samples = np.abs(np.frombuffer(raw, np.int16).astype(np.float32))
    if not samples.size:
        return [0.0] * n
    chunks = np.array_split(samples, n)
    out = np.array([c.max() if c.size else 0.0 for c in chunks])
    top = out.max() or 1.0
    return [round(float(x / top), 3) for x in out]


def peaks_for(file_key: str) -> list[float]:
    """Depodaki ses dosyasının dalga formu, içerik-adresli önbellekle (peaks/{özet}.json)."""
    key = f"peaks/{stable_hash([file_key, PEAKS])}.json"
    if storage.exists(key):
        return json.loads(storage.read_bytes(key))
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        path = storage.download(file_key, os.path.join(tmp, "a" + os.path.splitext(file_key)[1]))
        values = peaks_of(path)
    storage.client().put_object(Bucket=settings.s3_bucket, Key=key, Body=json.dumps(values).encode(),
                                ContentType="application/json")
    return values


def measure(file_key: str) -> float:
    """Depodaki ses dosyasının süresi (sn)."""
    from app.quality.anomaly import probe_duration
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        return round(probe_duration(storage.download(file_key, os.path.join(tmp, "a" + os.path.splitext(file_key)[1]))), 2)


# ---------- durum ----------

def line_hash(t: AudioTrack) -> str:
    """Repliğin senaryodaki kimliği: sentezlenen replikte içerik özeti, yüklenen kayıtta yerini aldığı repliğin özeti.
    Senaryo yeniden işlenince final kayıt bu özetle eşleşir ve korunur (hermes/ingest.py)."""
    return (t.params or {}).get("line_hash") or t.content_hash


def engine_of(t: AudioTrack) -> str | None:
    """Geçici sesin motoru, dosya anahtarından (audio/okur/…, audio/espeak/…, audio/elevenlabs/…, audio/tone/…)."""
    if t.source != "tts" or not t.file_key:
        return None
    parts = t.file_key.split("/")
    return parts[1] if len(parts) > 2 and parts[0] == "audio" else None


def has_video(s, sc: Scene) -> bool:
    """Çekimin görüntüsü üretildi mi (süresi sesten yeniden hesaplanamaz; yalnızca uyarı)."""
    if sc.visual_hash is not None:
        return True
    return s.scalar(select(Variation.id).where(Variation.scene_id == sc.id, Variation.refined_rank == 1).limit(1)) is not None


def dialogue(s, scene_id) -> list[AudioTrack]:
    return list(s.scalars(select(AudioTrack).where(AudioTrack.scene_id == scene_id, AudioTrack.is_active,
                                                   AudioTrack.kind == "dialogue")
                          .order_by(AudioTrack.start_offset_s, AudioTrack.version)))


def media(key: str | None) -> str | None:
    return f"/api/media?key={key}" if key else None


def shot_state(s, sc: Scene, names: dict) -> dict:
    lines = dialogue(s, sc.id)
    measured = [float(t.duration_s) if t.file_key and t.duration_s else None for t in lines]
    offsets = [float(t.start_offset_s) for t in lines]
    room = slots(offsets, float(sc.duration_s or 0))
    voice_s = retime(measured)[1] if lines and all(m is not None for m in measured) else None
    video = has_video(s, sc)
    out_lines = []
    for i, (t, m, slot) in enumerate(zip(lines, measured, room), start=1):
        out_lines.append({
            "track_id": str(t.id), "n": i, "character_id": str(t.character_id) if t.character_id else None,
            "character": names.get(t.character_id), "text": t.text, "emotion": t.emotion, "source": t.source,
            "final": t.source == "upload", "engine": engine_of(t), "url": media(t.file_key), "duration_s": m,
            "start_offset_s": round(float(t.start_offset_s), 2), "slot_s": slot, "drift": drifts(m, slot),
            "filename": (t.params or {}).get("filename"), "version": t.version, "check": check_view(t)})
    drift = voice_s is not None and abs(voice_s - float(sc.duration_s or 0)) > DRIFT_S
    return {"scene_id": str(sc.id), "scene_code": sc.scene_code, "heading": (sc.script_text or "").split("\n")[0],
            "description": sc.description, "duration_s": round(float(sc.duration_s or 0), 2),
            "duration_source": getattr(sc, "duration_source", None) or "script", "voice_s": voice_s,
            "voice_clamped": clamp_shot(voice_s) if voice_s is not None else None, "has_video": video,
            "status": sc.status, "drift": drift, "drift_text": DRIFT_TEXT if drift and video else None,
            "lines": out_lines}


def state(s, project_id) -> dict:
    """Ses sayfasının verisi: sekanslar (kilit), çekimler (süreler) ve replikler (kaynak, süre, yer)."""
    p = s.get(Project, project_id)
    names = {c.id: c.name for c in s.scalars(episode_characters_q(project_id))}
    seqs = list(s.scalars(select(Sequence).where(Sequence.project_id == project_id).order_by(Sequence.seq_order)))
    scenes = list(s.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order)))
    groups = []
    for q in seqs + [None]:
        shots = [shot_state(s, sc, names) for sc in scenes if (sc.sequence_id == (q.id if q else None))]
        if q is None and not shots:
            continue
        groups.append({"id": str(q.id) if q else None, "code": q.code if q else "", "title": q.title if q else "Sekanssız",
                       "locked": bool(q and q.voice_locked_at), "locked_at": q.voice_locked_at.timestamp()
                       if q and q.voice_locked_at else None, "shots": shots,
                       "lines": sum(len(x["lines"]) for x in shots),
                       "final": sum(1 for x in shots for ln in x["lines"] if ln["final"])})
    lines = [ln for g in groups for x in g["shots"] for ln in x["lines"]]
    cfg = project_settings(p.settings)
    checker = checker_on()
    return {"sequences": groups, "voice_first": bool(cfg.get("voice_first")), "checker": checker,
            "counts": {"lines": len(lines), "final": sum(ln["final"] for ln in lines),
                       "temp": sum(not ln["final"] for ln in lines),
                       "unmeasured": sum(ln["duration_s"] is None for ln in lines),
                       "mismatch": sum(1 for ln in lines if ln["check"] and not ln["check"]["ok"]),
                       "unchecked": sum(1 for ln in lines if checker and ln["url"] and not ln["check"]),
                       "drift": sum(1 for g in groups for x in g["shots"] if x["drift"]),
                       "unlocked": sum(1 for g in groups if g["id"] and not g["locked"])},
            "pads": {"in": PAD_IN_S, "gap": GAP_S, "out": PAD_OUT_S, "drift": DRIFT_S},
            "drift_text": DRIFT_TEXT}


# ---------- geçici ses ----------

def ensure_temp(project_id: str, sequence_id: str | None = None) -> list[str]:
    """Henüz seslendirilmemiş (ya da süresi ölçülmemiş) sentez repliklerinin geçici sesini üretir. Dönen: çekim kodları.
    Yerel motor Okur (model yoksa espeak); ücretli modda ve projede ücretli ses açıkken ElevenLabs (bütçe rezervasyonundan geçer)."""
    from app.post.pipeline import ensure_dialogue_audio, workspace
    with session_scope() as s:
        cfg = project_settings(s.get(Project, project_id).settings)
        q = select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order)
        if sequence_id:
            q = q.where(Scene.sequence_id == sequence_id)
        work = [(sc, [t for t in dialogue(s, sc.id) if t.source == "tts" and not (t.file_key and t.duration_s)])
                for sc in s.scalars(q)]
        work = [(sc, ts) for sc, ts in work if ts]
        s.expunge_all()
    done = []
    with workspace() as tmp:
        for sc, ts in work:
            for t in ts:
                ensure_dialogue_audio(t, sc, tmp, cfg)
            done.append(sc.scene_code)
    return done


# ---------- Okur kaynak bildirimi ----------

OKUR_NOTICE = """Geçici diyalog sesleri: Okur TTS v1.0 (https://huggingface.co/ahakan/okur-tts)
Model ağırlıkları: Creative Commons Attribution 4.0 (CC BY 4.0), https://creativecommons.org/licenses/by/4.0/
Yapımcı: Ahmet Hakan Beşel, 2026. Ses: anonim bir Mozilla Common Voice gönüllüsü (CC0).
Eğitim verisi: Mozilla Common Voice (CC0), ISSAI Turkish Speech Corpus (MIT), MediaSpeech ve Google FLEURS (CC BY 4.0).
Sesler perde ve hız bakımından değiştirilmiştir (karakter başına perde kaydırma, duyguya göre hız).
"""


def okur_used(s, project_id) -> bool:
    """Projenin aktif diyaloglarından biri Okur ile seslendirilmiş mi (teslim paketine OKUR_TTS.txt girer)."""
    return s.scalar(select(AudioTrack.id).join(Scene, Scene.id == AudioTrack.scene_id)
                    .where(Scene.project_id == project_id, AudioTrack.is_active, AudioTrack.kind == "dialogue",
                           AudioTrack.file_key.like("audio/okur/%")).limit(1)) is not None


# ---------- ses denetimi ----------

def checker_on() -> bool:
    """Ses denetimi açık mı: ayar kapalı değil ve Duyu-small'ın model dosyaları var."""
    from app.quality import asr
    return settings.voice_check != "off" and asr.available(settings.quality_model_dir)


def check_of(t: AudioTrack) -> dict | None:
    """Kanalın geçerli denetim sonucu: yalnızca bugünkü ses dosyası ve metin için yapılmışsa."""
    c = (t.params or {}).get("check")
    return c if c and t.file_key and c.get("file_key") == t.file_key and c.get("text") == (t.text or "") else None


def check_view(t: AudioTrack) -> dict | None:
    c = check_of(t)
    return {"ok": c["ok"], "cer": c["cer"], "heard": c["heard"]} if c else None


def heard(file_key: str, text: str) -> str:
    """Ses dosyasının yazıya dökümü, içerik-adresli önbellekle (asr/{özet}.json: dosya, model ve dil)."""
    from app.quality import asr
    lang = asr.lang_for(text)
    key = f"asr/{stable_hash([file_key, asr.MODEL_ID, lang])}.json"
    if storage.exists(key):
        return json.loads(storage.read_bytes(key))["text"]
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        result = asr.transcribe(storage.download(file_key, os.path.join(tmp, "a" + os.path.splitext(file_key)[1])),
                                settings.quality_model_dir, lang)
    storage.client().put_object(Bucket=settings.s3_bucket, Key=key, ContentType="application/json",
                                Body=json.dumps({"text": result, "model": asr.MODEL_ID, "lang": lang},
                                                ensure_ascii=False).encode())
    return result


def ensure_checks(project_id: str, track_ids: list[str] | None = None) -> dict:
    """Sesi olan ama denetlenmemiş (ya da sesi/metni değişmiş) aktif replikleri yazıya döker ve metinle karşılaştırır.
    Dönen: {"checked": sayı, "mismatch": ["S001_SH01 · 2", ...]}. Denetim kapalıysa ya da model yoksa hiçbir şey yapmaz."""
    from app.quality import asr
    if not checker_on():
        return {"checked": 0, "mismatch": []}
    with session_scope() as s:
        q = (select(AudioTrack, Scene.scene_code).join(Scene, Scene.id == AudioTrack.scene_id)
             .where(Scene.project_id == project_id, AudioTrack.kind == "dialogue", AudioTrack.is_active,
                    AudioTrack.file_key.is_not(None)).order_by(Scene.seq_order, AudioTrack.start_offset_s))
        if track_ids:
            q = q.where(AudioTrack.id.in_(track_ids))
        rows = list(s.execute(q))
        lines = {}
        for t, code in rows:                          # repliğin çekimdeki sırası (panelde "S001_SH01 · 2")
            lines.setdefault(code, []).append(t.id)
        todo = [(t.id, t.file_key, t.text or "", f"{code} · {lines[code].index(t.id) + 1}")
                for t, code in rows if check_of(t) is None]
    checked, mismatch = 0, []
    for track_id, file_key, text, label in todo:
        result = {"file_key": file_key, "text": text, "heard": heard(file_key, text), "model": asr.MODEL_ID}
        result.update(asr.compare(text, result["heard"]))
        with session_scope() as s:
            t = s.get(AudioTrack, track_id)
            if t is None or t.file_key != file_key:   # bu arada yeni bir ses gelmiş: sonuç eskidir
                continue
            t.params = {**(t.params or {}), "check": result}
        checked += 1
        if not result["ok"]:
            mismatch.append(label)
    return {"checked": checked, "mismatch": mismatch}


# ---------- kilit ----------

def ensure_editable(s, sc: Scene) -> None:
    seq = s.get(Sequence, sc.sequence_id) if sc.sequence_id else None
    if seq is not None and seq.voice_locked_at is not None:
        raise VoiceLocked(f"{seq.code} sekansının sesi kilitli; replik değiştirmek için önce kilidi açın.")


def lock(s, seq: Sequence) -> dict:
    """Sekansın sesini kilitler. Her replik ölçülmüş olmalı. Görüntüsü üretilmemiş çekimlerin süresi ve replik
    ofsetleri sesten yeniden hesaplanır; üretilmiş çekimlerde yalnızca fark listelenir (yeniden üretim yok)."""
    scenes = list(s.scalars(select(Scene).where(Scene.sequence_id == seq.id).order_by(Scene.seq_order)))
    per = [(sc, dialogue(s, sc.id)) for sc in scenes]
    unmeasured = sum(1 for _, ts in per for t in ts if not (t.file_key and t.duration_s))
    if unmeasured:
        raise VoiceNotReady(f"{unmeasured} replik henüz seslendirilmedi; önce geçici sesleri üretin ya da kayıtları yükleyin.")
    retimed, drift, clamped = [], [], []
    for sc, ts in per:
        if not ts:
            continue
        offsets, total = retime([float(t.duration_s) for t in ts])
        if has_video(s, sc):
            if abs(total - float(sc.duration_s or 0)) > DRIFT_S:
                drift.append(sc.scene_code)
            continue
        for t, off in zip(ts, offsets):
            t.start_offset_s = off
        if clamp_shot(total) != total:
            clamped.append(sc.scene_code)
        sc.duration_s, sc.duration_source = clamp_shot(total), "voice"
        retimed.append(sc.scene_code)
    seq.voice_locked_at = dt.datetime.now(dt.timezone.utc)
    return {"locked": True, "locked_at": seq.voice_locked_at.timestamp(), "retimed": retimed, "drift": drift,
            "clamped": clamped}


def unlock(s, seq: Sequence) -> dict:
    """Kilidi açar; süreler ve ofsetler olduğu gibi kalır."""
    seq.voice_locked_at = None
    return {"locked": False, "locked_at": None}


def mix_for(s, part: list, out: str | None = None) -> dict:
    """Parçanın diyalog karışımı (ses referansı): her repliğin sesi parçanın zaman çizelgesindeki yerine konur.
    {"key": içerik-adresli anahtar, "lines": [...], "missing": seslendirilmemiş replik sayısı, "duration_s"}.
    out verilirse dosya üretilir ve depoya yazılır (yoksa yalnızca plan)."""
    from app.post.mux import AudioInput, mix_audio
    at, lines, missing = 0.0, [], 0
    for x in part:
        for t in dialogue(s, x.id):
            if not t.file_key:
                missing += 1
                continue
            lines.append((t.file_key, round(at + float(t.start_offset_s), 3), round(float(t.gain_db), 2)))
        at += float(x.duration_s or 5.0)
    total = round(at, 3)
    key = f"voice/refs/{stable_hash([lines, total])}.wav" if lines else None
    if out and key and not storage.exists(key):
        work = os.path.dirname(out)
        inputs = [AudioInput(storage.download(k, os.path.join(work, f"l{i}{os.path.splitext(k)[1]}")), off, gain)
                  for i, (k, off, gain) in enumerate(lines)]
        storage.put_file(mix_audio(inputs, out, total), key, "audio/wav")
    return {"key": key, "lines": len(lines), "missing": missing, "duration_s": total}


def lipsync_state(cfg: dict | None) -> dict:
    """Dudak senkronu bu projede ne yapar (Kurgu denetçisi › Ses): ücretsiz modda atlanır, ücretli modda fal.ai."""
    from app.post.pipeline import paid_audio, paid_lipsync
    if settings.dry_run:
        return {"mode": "free", "text": "Ücretsiz mod: dudak senkronu atlanır, video olduğu gibi kullanılır."}
    if not paid_audio(cfg):
        return {"mode": "off", "text": "Projede ücretli ses kapalı: dudak senkronu çağrılmaz."}
    if not paid_lipsync(cfg):
        return {"mode": "no_key", "text": "fal.ai anahtarı tanımlı değil: video senkronsuz kullanılır."}
    return {"mode": "paid", "text": "fal.ai sync-lipsync (ücretli; bütçe rezervasyonundan geçer)."}
