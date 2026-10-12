"""Ayrıştırılmış senaryoyu veritabanına yazar: sekanslar, karakterler, varlıklar, sahneler, diyalog ve müzik kanalları.

Müzik sekans düzeyindedir (Faz 11): her sekans, çekimlerinin baskın duygusuna göre bir prosedürel müzik kanalı alır ve
müzik çekimler arasında kesintisiz akar. Sahneye müzik eklenmez; sahne düzeyindeki fon kanalları isteğe bağlıdır.

Kadrodaki her karakter, başlıktaki her mekân ve [NESNE: ...] ile işaretlenen her nesne için bir varlık yoksa `draft`
durumunda oluşturulur ve çekime bağlanır. Taslak varlıkla üretim başlamaz (hermes/lint.py); yönetmen referansları
onaylayıp kilitleyince başlar.

Aynı senaryoyu yeniden yüklemek hiçbir şeyi değiştirmemeli: sahne alanları aynı değerlerle yazılır ve içeriği
değişmeyen ses kanalları korunur; böylece Stateless Re-render planı boş çıkar ve para harcanmaz.
"""
import logging
import uuid

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.config import project_settings, settings
from app.core.chain import sync_seams
from app.core.hashing import stable_hash
from app.db import session_scope
from app.engine.providers import route_engine
from app.hermes import rules
from app.models import Asset, AudioTrack, Character, Project, Scene, SceneAsset, SceneCharacter, Sequence, TimelineTrack

log = logging.getLogger("hellgrind.hermes")

DIALOGUE_START_S = 0.4      # ilk replik klibin başından bu kadar sonra başlar
MUSIC_GAIN_DB = -8.0
MUSIC_FADE_IN_S, MUSIC_FADE_OUT_S = 1.0, 1.5
DEFAULT_SEQUENCE = {"code": "SEQ01", "title": "Sekans 1"}


def parse_script(text: str, project_id: str, known=()) -> dict:
    """Ücretsiz modda her zaman kural tabanlı ayrıştırıcı; LLM yalnızca DRY_RUN kapalıyken ve açıkça istenirse.
    known: projenin mevcut karakterleri (konuşmasalar da adları geçen çekimlere bağlanırlar)."""
    if settings.hermes_mode == "llm" and not settings.dry_run:
        from app.live.hermes_llm import parse_with_llm     # ücretli; LLM çağrıları proje bütçesinden rezerve edilir
        return parse_with_llm(text, project_id)
    return rules.parse(text, known=known)


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


class AssetBook:
    """Projenin varlıkları (tür, ad) -> satır; olmayanı `draft` olarak oluşturur."""

    def __init__(self, s, project_id: str):
        self.s, self.project_id, self.created = s, project_id, []
        self.rows = {(a.type, a.name): a for a in s.scalars(select(Asset).where(Asset.project_id == project_id))}

    def get(self, type_: str, name: str, description: str | None = None) -> Asset:
        row = self.rows.get((type_, name))
        if row is None:
            # render_style boş: varlık stil kılavuzunun tür kuralını izler (style/bible.py)
            row = Asset(id=uuid.uuid4(), project_id=self.project_id, type=type_, name=name, description=description,
                        render_style=None, status="draft", version=1, references={})
            self.s.add(row)
            self.rows[(type_, name)] = row
            self.created.append(f"{name} ({type_})")
        return row


def ensure_character(s, book: AssetBook, project_id: str, name: str, chars: dict, **fields) -> Character:
    """Karakteri ve ona bağlı karakter varlığını hazırlar (ses alanları karakterde kalır)."""
    ch = chars.get(name)
    if ch is None:
        cid = uuid.uuid4()
        ch = Character(id=cid, project_id=project_id, name=name, pinecone_namespace=str(cid), **fields)
        s.add(ch)
        chars[name] = ch
    if ch.asset_id is None:
        ch.asset_id = book.get("character", name, ch.description).id
    return ch


def shot_assets(shot: dict) -> tuple[str | None, list[str]]:
    """(mekân, nesneler). LLM modu bu alanları vermeyebilir: başlıktan ve açıklamadaki işaretlerden çıkarılır."""
    location = shot.get("location") or rules.location_name(shot.get("heading", ""))
    props = shot.get("props") or rules.take_props(shot.get("description", ""))[1]
    return location, list(props)


def transition_guess(shots: list[dict], i: int) -> str:
    """Geçişi olmayan çekim (LLM ayrıştırıcısı): sahne başlığı değiştiyse new_scene, değilse continuous."""
    return "new_scene" if i == 0 or shots[i].get("heading") != shots[i - 1].get("heading") else "continuous"


def _board_approval_default(s, project) -> None:
    """Zincirli projede (ücretli modda kare zinciri Qwen-Image-Edit ile çiziliyorsa) storyboard önce: yönetmen ayarı
    seçmediyse kare onayı zorunlu olur; videolar şerit onaylanınca başlar. Ücretsiz mod (yerel kareler) değişmez."""
    from app.engine.storyboard import chain_enabled
    raw = dict(project.settings or {})
    if "require_board_approval" in raw or settings.dry_run or not chain_enabled():
        return
    from app.engine import fal_plan, scene_parts
    if scene_parts.reference_mode() and fal_plan.is_fal(fal_plan.draft_engine(project_settings(raw))):
        return                                  # referans modu: kare yok; onay planın kendisidir (require_plan_approval)
    chained = s.scalar(select(Scene.id).where(Scene.project_id == project.id,
                                              Scene.transition_auto.in_(("continuous", "cut"))).limit(1))
    if chained is not None:
        project.settings = {**raw, "require_board_approval": True}


PRESERVED_ON_SAME_TEXT = ("duration_s", "camera", "emotion", "action_complexity")


def ingest(project_id: str, text: str, auto_music: bool = True) -> dict:
    with session_scope() as s:
        known = list(s.scalars(select(Character.name).where(Character.project_id == project_id)))
    parsed = parse_script(text, project_id, known)     # DB işleminin dışında: LLM modunda uzun sürebilir
    lang = "tr" if rules.is_turkish_script(text) else "en"     # prompt çevirisi bunu esas alır (hermes/translate.py)
    created, updated, unchanged = [], [], []
    with session_scope() as s:
        # Aynı projeye eşzamanlı iki senaryo yüklemesi (project_id, scene_code) benzersizliğine çarpmasın.
        project = s.execute(select(Project).where(Project.id == project_id).with_for_update()).scalar_one()
        project.script_source = text

        seqs = _write_sequences(s, project_id, parsed)
        book = AssetBook(s, project_id)
        chars = {c.name: c for c in s.scalars(select(Character).where(Character.project_id == project_id))}
        for name in parsed["cast"]:
            ensure_character(s, book, project_id, name, chars)
        s.flush()

        existing = {sc.scene_code: sc for sc in s.scalars(select(Scene).where(Scene.project_id == project_id))}
        from app.config import project_settings
        engine_cfg = project_settings(s.get(Project, project_id).settings)    # projenin taslak motoru
        for order, shot in enumerate(parsed["shots"], start=1):
            sc = existing.get(shot["scene_id"])
            fields = dict(seq_order=order, sequence_id=seqs[shot.get("sequence", DEFAULT_SEQUENCE["code"])].id,
                          script_text=script_text(shot), description=shot["description"],
                          emotion=shot["emotion"], camera=shot["camera"], duration_s=shot["duration_s"],
                          action_complexity=shot["action_complexity"],
                          transition_auto=shot.get("transition") or transition_guess(parsed["shots"], order - 1),
                          transition_hint=shot.get("transition_hint"))
            prompt = {"prompt": shot["visual_prompt"], "lang": lang}
            if sc is None:
                sc = Scene(project_id=project_id, scene_code=shot["scene_id"], prompt_spec=prompt, **fields)
                sc.engine = route_engine(sc, engine_cfg)     # görsel özet motoru içerir: baştan yazılır, sonradan değişip render tetiklemez
                s.add(sc)
                created.append(shot["scene_id"])
            else:
                if sc.script_text == fields["script_text"]:
                    # Metni değişmeyen çekim: yönetmenin panelde verdiği süre, kamera ve duygu korunur (motor zaten
                    # route_engine ile korunur); senaryoyu yeniden kaydetmek planı sıfırlamaz.
                    for key in PRESERVED_ON_SAME_TEXT:
                        fields.pop(key)
                    unchanged.append(shot["scene_id"])
                for key, value in fields.items():
                    setattr(sc, key, value)
                sc.prompt_spec = {**(sc.prompt_spec or {}), **prompt}    # düzeltme turlarının eklediği anahtarlar kalır
                sc.engine = route_engine(sc, engine_cfg)
                updated.append(shot["scene_id"])
            s.flush()
            _write_cast(s, sc, shot, chars)
            _write_assets(s, sc, shot, book)
            _write_tracks(s, sc, shot, chars)
        music = _write_sequence_music(s, project_id, seqs, parsed["shots"]) if auto_music else []
        sync_seams(s, project_id)                          # geçişler ve zincirden gelen karakterler (core/chain.py)
        _board_approval_default(s, project)

    orphaned = sorted(set(existing) - {shot["scene_id"] for shot in parsed["shots"]})
    if orphaned:
        log.warning("senaryoda artık olmayan sahneler korunuyor (silinmedi): %s", orphaned)
    return {"cast": parsed["cast"], "shots": len(parsed["shots"]), "created": created, "updated": updated,
            "unchanged": unchanged, "orphaned": orphaned, "assets_created": book.created,
            "sequences": [{"code": q["code"], "title": q["title"],
                           "shots": sum(sh.get("sequence", DEFAULT_SEQUENCE["code"]) == q["code"] for sh in parsed["shots"])}
                          for q in parsed.get("sequences") or [DEFAULT_SEQUENCE]],
            "music_created": music}


def _write_sequences(s, project_id: str, parsed: dict) -> dict:
    """Senaryodaki sekansları koduna göre yazar (başlık ve sıra güncellenir; tavan ve harcama korunur). {kod: satır}.
    Senaryoda artık olmayan sekanslar silinmez."""
    wanted = parsed.get("sequences") or [DEFAULT_SEQUENCE]
    rows = {q.code: q for q in s.scalars(select(Sequence).where(Sequence.project_id == project_id))}
    for order, item in enumerate(wanted, start=1):
        row = rows.get(item["code"])
        if row is None:
            row = rows[item["code"]] = Sequence(id=uuid.uuid4(), project_id=project_id, code=item["code"],
                                                title=item["title"], seq_order=order, status="active", settings={})
            s.add(row)
        else:
            row.title, row.seq_order = item["title"], order
    s.flush()
    return rows


def dominant_mood(shots: list[dict]) -> str:
    """Sekans müziğinin duygusu: çekim sürelerine göre en ağır basan duygu (eşitlikte önce geçen)."""
    weight: dict[str, float] = {}
    for shot in shots:
        weight[shot["emotion"]] = weight.get(shot["emotion"], 0.0) + float(shot["duration_s"])
    return max(weight, key=lambda e: weight[e]) if weight else "neutral"


def _write_sequence_music(s, project_id: str, seqs: dict, shots: list[dict]) -> list[str]:
    """Müziği olmayan her sekansa prosedürel müzik ekler. Yönetmenin değiştirdiği müzik ezilmez; sahne düzeyinde müziği
    olan (Faz 11 öncesi) sekansa eklenmez: film çift müzik çalmasın."""
    created = []
    for code, seq in seqs.items():
        mine = [sh for sh in shots if sh.get("sequence", DEFAULT_SEQUENCE["code"]) == code]
        if not mine:
            continue
        has_music = s.scalar(select(TimelineTrack.id).where(TimelineTrack.sequence_id == seq.id, TimelineTrack.is_active,
                                                            TimelineTrack.kind == "music").limit(1))
        scene_music = s.scalar(select(AudioTrack.id).join(Scene, Scene.id == AudioTrack.scene_id)
                               .where(Scene.sequence_id == seq.id, AudioTrack.is_active, AudioTrack.kind == "music").limit(1))
        if has_music or scene_music:
            continue
        mood = dominant_mood(mine)
        s.add(TimelineTrack(project_id=project_id, sequence_id=seq.id, kind="music", source="procedural",
                            params={"mood": mood}, start_s=0.0, end_s=None, gain_db=MUSIC_GAIN_DB,
                            fade_in_s=MUSIC_FADE_IN_S, fade_out_s=MUSIC_FADE_OUT_S,
                            content_hash=music_hash(mood), is_active=True))
        created.append(code)
    return created


def _write_cast(s, sc: Scene, shot: dict, chars: dict) -> None:
    s.execute(delete(SceneCharacter).where(SceneCharacter.scene_id == sc.id))
    for name in shot["characters"]:
        if name in chars:
            s.add(SceneCharacter(scene_id=sc.id, character_id=chars[name].id, role="primary",
                                 dialogue=[d for d in shot["dialogue"] if d["character"] == name]))


def _write_assets(s, sc: Scene, shot: dict, book: AssetBook) -> None:
    """Senaryodan gelen mekân ve nesne bağlarını yeniler; panelden elle eklenen bağlar (source=manual) korunur."""
    s.execute(delete(SceneAsset).where(SceneAsset.scene_id == sc.id, SceneAsset.source == "script"))
    location, props = shot_assets(shot)
    links = ([("location", book.get("location", location))] if location else []) + \
            [("prop", book.get("prop", name)) for name in props]
    s.flush()
    for role, asset in links:
        s.execute(insert(SceneAsset).values(scene_id=sc.id, asset_id=asset.id, role=role, source="script")
                  .on_conflict_do_nothing(index_elements=["scene_id", "asset_id"]))


def _write_tracks(s, sc: Scene, shot: dict, chars: dict) -> None:
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
