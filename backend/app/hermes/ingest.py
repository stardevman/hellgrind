"""Senaryoyu veritabanına yazar: karakterler, sahneler, diyalog ve müzik kanalları."""
from sqlalchemy import select, update

from app.config import settings
from app.core.hashing import stable_hash
from app.db import session_scope
from app.hermes import rules
from app.models import AudioTrack, Character, Project, Scene, SceneCharacter

GAP_S = 0.35


def parse_script(text: str) -> dict:
    if settings.hermes_mode == "llm" and not settings.dry_run:
        from app.live.hermes_llm import parse_with_llm          # ücretli; yalnızca açıkça istenirse
        return parse_with_llm(text)
    return rules.parse(text)


def ingest(project_id: str, text: str, auto_music: bool = True) -> dict:
    parsed = parse_script(text)
    created, updated = [], []
    with session_scope() as s:
        project = s.get(Project, project_id)
        project.script_source = text

        chars = {c.name: c for c in s.scalars(select(Character).where(Character.project_id == project_id))}
        for name in parsed["cast"]:
            if name not in chars:
                c = Character(project_id=project_id, name=name, pinecone_namespace=project_id,
                              reference_images=[], style_constraints={}, embedding_version=1, lora_scale=1.0)
                s.add(c)
                chars[name] = c
        s.flush()

        existing = {sc.scene_code: sc for sc in s.scalars(select(Scene).where(Scene.project_id == project_id))}
        for order, shot in enumerate(parsed["shots"], start=1):
            fields = dict(seq_order=order, script_text=shot["heading"], description=shot["description"],
                          emotion=shot["emotion"], camera=shot["camera"], duration_s=shot["duration_s"],
                          action_complexity=shot["action_complexity"],
                          prompt_spec={"prompt": shot["visual_prompt"], "negative": "", "lang": "tr"})
            sc = existing.get(shot["scene_id"])
            if sc is None:
                sc = Scene(project_id=project_id, scene_code=shot["scene_id"], status="pending", prompt_revision=0,
                           correction_count=0, spent_usd=0, **fields)
                s.add(sc)
                created.append(shot["scene_id"])
            else:
                for k, v in fields.items():
                    setattr(sc, k, v)
                updated.append(shot["scene_id"])
            s.flush()

            # karakter bağları
            s.query(SceneCharacter).filter(SceneCharacter.scene_id == sc.id).delete()
            for name in shot["characters"]:
                if name in chars:
                    lines = [d for d in shot["dialogue"] if d["character"] == name]
                    s.add(SceneCharacter(scene_id=sc.id, character_id=chars[name].id, role="primary",
                                         dialogue=lines))

            # diyalog kanalları: içerik aynıysa korunur (gereksiz yeniden render olmaz)
            wanted, t = [], 0.4
            for d in shot["dialogue"]:
                cid = chars[d["character"]].id if d["character"] in chars else None
                body = {"kind": "dialogue", "text": d["line"], "emotion": d["emotion"],
                        "character_id": str(cid) if cid else None, "start_offset_s": round(t, 2)}
                wanted.append((body, cid))
                t += len(d["line"].split()) / rules.WPS + GAP_S
            active = s.scalars(select(AudioTrack).where(AudioTrack.scene_id == sc.id, AudioTrack.is_active)).all()
            act_dlg = {a.content_hash for a in active if a.kind == "dialogue"}
            if {stable_hash(b) for b, _ in wanted} != act_dlg:
                s.execute(update(AudioTrack).where(AudioTrack.scene_id == sc.id, AudioTrack.kind == "dialogue")
                          .values(is_active=False))
                for body, cid in wanted:
                    s.add(AudioTrack(scene_id=sc.id, character_id=cid, kind="dialogue", source="local_tts",
                                     text=body["text"], emotion=body["emotion"], params={},
                                     content_hash=stable_hash(body), start_offset_s=body["start_offset_s"],
                                     gain_db=0.0, is_active=True, version=1))
            if auto_music and not any(a.kind == "music" for a in active):
                body = {"kind": "music", "source": "procedural", "mood": shot["emotion"]}
                s.add(AudioTrack(scene_id=sc.id, kind="music", source="procedural", params={"mood": shot["emotion"]},
                                 content_hash=stable_hash(body), start_offset_s=0.0, gain_db=-8.0,
                                 is_active=True, version=1))
    return {"cast": parsed["cast"], "shots": len(parsed["shots"]), "created": created, "updated": updated}
