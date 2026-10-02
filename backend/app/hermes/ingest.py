"""Ayrıştırılmış senaryoyu veritabanına yazar: karakterler, sahneler, diyalog ve müzik kanalları.

Aynı senaryoyu yeniden yüklemek hiçbir şeyi değiştirmemeli: sahne alanları aynı değerlerle yazılır ve içeriği
değişmeyen ses kanalları korunur; böylece Stateless Re-render planı boş çıkar ve para harcanmaz.
"""
import logging
import uuid

from sqlalchemy import delete, select

from app.config import settings
from app.core.hashing import stable_hash
from app.db import session_scope
from app.engine.providers import route_engine
from app.hermes import rules
from app.models import AudioTrack, Character, Project, Scene, SceneCharacter

log = logging.getLogger("hellgrind.hermes")

DIALOGUE_START_S = 0.4      # ilk replik klibin başından bu kadar sonra başlar
MUSIC_GAIN_DB = -8.0


def parse_script(text: str, project_id: str) -> dict:
    """Ücretsiz modda her zaman kural tabanlı ayrıştırıcı; LLM yalnızca DRY_RUN kapalıyken ve açıkça istenirse."""
    if settings.hermes_mode == "llm" and not settings.dry_run:
        from app.live.hermes_llm import parse_with_llm     # ücretli; LLM çağrıları proje bütçesinden rezerve edilir
        return parse_with_llm(text, project_id)
    return rules.parse(text)


def dialogue_hash(character_id, line: dict) -> str:
    """Yalnızca duyulan şeyi belirler (kim, ne, hangi duyguyla). Ofset ayrıca özetlenir; önceki replik uzayıp
    sonrakinin ofseti kayınca, sonraki replik yeniden seslendirilmez."""
    return stable_hash({"kind": "dialogue", "character": str(character_id), "text": line["line"],
                        "emotion": line["emotion"]})


def music_hash(mood: str) -> str:
    return stable_hash({"kind": "music", "source": "procedural", "mood": mood})


def script_text(shot: dict) -> str:
    said = "".join(f"\n{d['character'].upper()}: {d['line']}" for d in shot["dialogue"])
    return f"{shot['heading']}\n{shot['description']}{said}"


def ingest(project_id: str, text: str, auto_music: bool = True) -> dict:
    parsed = parse_script(text, project_id)     # DB işleminin dışında: LLM modunda uzun sürebilir
    created, updated = [], []
    with session_scope() as s:
        # Aynı projeye eşzamanlı iki senaryo yüklemesi (project_id, scene_code) benzersizliğine çarpmasın.
        project = s.execute(select(Project).where(Project.id == project_id).with_for_update()).scalar_one()
        project.script_source = text

        chars = {c.name: c for c in s.scalars(select(Character).where(Character.project_id == project_id))}
        for name in parsed["cast"]:
            if name not in chars:
                cid = uuid.uuid4()
                chars[name] = Character(id=cid, project_id=project_id, name=name, pinecone_namespace=str(cid))
                s.add(chars[name])
        s.flush()

        existing = {sc.scene_code: sc for sc in s.scalars(select(Scene).where(Scene.project_id == project_id))}
        for order, shot in enumerate(parsed["shots"], start=1):
            sc = existing.get(shot["scene_id"])
            fields = dict(seq_order=order, script_text=script_text(shot), description=shot["description"],
                          emotion=shot["emotion"], camera=shot["camera"], duration_s=shot["duration_s"],
                          action_complexity=shot["action_complexity"])
            prompt = {"prompt": shot["visual_prompt"], "lang": "tr"}
            if sc is None:
                sc = Scene(project_id=project_id, scene_code=shot["scene_id"], prompt_spec=prompt, **fields)
                sc.engine = route_engine(sc)     # görsel özet motoru içerir: baştan yazılır, sonradan değişip render tetiklemez
                s.add(sc)
                created.append(shot["scene_id"])
            else:
                for key, value in fields.items():
                    setattr(sc, key, value)
                sc.prompt_spec = {**(sc.prompt_spec or {}), **prompt}    # düzeltme turlarının eklediği anahtarlar kalır
                sc.engine = route_engine(sc)
                updated.append(shot["scene_id"])
            s.flush()
            _write_cast(s, sc, shot, chars)
            _write_tracks(s, sc, shot, chars, auto_music)

    orphaned = sorted(set(existing) - {shot["scene_id"] for shot in parsed["shots"]})
    if orphaned:
        log.warning("senaryoda artık olmayan sahneler korunuyor (silinmedi): %s", orphaned)
    return {"cast": parsed["cast"], "shots": len(parsed["shots"]), "created": created, "updated": updated,
            "orphaned": orphaned}


def _write_cast(s, sc: Scene, shot: dict, chars: dict) -> None:
    s.execute(delete(SceneCharacter).where(SceneCharacter.scene_id == sc.id))
    for name in shot["characters"]:
        if name in chars:
            s.add(SceneCharacter(scene_id=sc.id, character_id=chars[name].id, role="primary",
                                 dialogue=[d for d in shot["dialogue"] if d["character"] == name]))


def _write_tracks(s, sc: Scene, shot: dict, chars: dict, auto_music: bool) -> None:
    tracks = s.scalars(select(AudioTrack).where(AudioTrack.scene_id == sc.id)).all()
    active = [t for t in tracks if t.is_active]
    next_version = max((t.version for t in tracks if t.kind == "dialogue"), default=0) + 1

    # İstenen replikler ardışık ofsetlerle; her biri mevcut bir kanalla içerik özetiyle eşleşir.
    # Aynı replik iki kez geçebilir, bu yüzden özet başına bir liste tutulur.
    keep: dict[str, list[AudioTrack]] = {}
    for t in active:
        if t.kind == "dialogue":
            keep.setdefault(t.content_hash, []).append(t)
    offset = DIALOGUE_START_S
    for line in shot["dialogue"]:
        cid = chars[line["character"]].id if line["character"] in chars else None
        h = dialogue_hash(cid, line)
        if keep.get(h):
            track = keep[h].pop(0)
            track.start_offset_s = round(offset, 2)             # değişmeyen replik: dosyası ve sesi korunur
        else:
            s.add(AudioTrack(scene_id=sc.id, character_id=cid, kind="dialogue", source="tts", text=line["line"],
                             emotion=line["emotion"], params={}, content_hash=h, start_offset_s=round(offset, 2),
                             gain_db=0.0, is_active=True, version=next_version))
        offset += len(line["line"].split()) / rules.WPS + rules.LINE_GAP_S
    for leftovers in keep.values():                             # artık senaryoda olmayan replikler
        for track in leftovers:
            track.is_active = False

    if auto_music and not any(t.kind == "music" for t in active):
        s.add(AudioTrack(scene_id=sc.id, kind="music", source="procedural", params={"mood": sc.emotion},
                         content_hash=music_hash(sc.emotion), start_offset_s=0.0, gain_db=MUSIC_GAIN_DB,
                         is_active=True, version=1))
