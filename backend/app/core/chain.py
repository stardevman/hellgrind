"""Sahneler arası süreklilik: anahtar kare zinciri (ağ yok; planlayıcı, storyboard, üretim ve panel aynı kuralı kullanır).

Bir sekansta çekimlerin ilk kareleri sırayla çizilir: K1, K2, … Kn (Kn = n. çekimin ilk karesi).
- Çekimin geçişi (transition_in): continuous (aynı sahnede devam; varsayılan), cut ("CUT TO:" sonrası: kamera açısı
  değişebilir, mekân/nesne/karakter durumu korunur), new_scene (yeni sahne başlığı: zincir kırılır). Yönetmen Kurgu
  sayfasından değiştirir (`transition_in`), boşsa senaryodan gelen (`transition_auto`).
- Kn (n>1, continuous ya da cut): Qwen-Image-Edit ile image 1 = K(n-1), image 2/3 = sahnedeki karakterlerin durağan
  referansları. new_scene: K referanslardan çizilir.
- Kn çizilmeden K(n+1) çizilmez. K(n-1) değişirse (yeniden çizilir), Kn "eski" olur (`board_parent` önceki karenin
  `board_sha`'sıyla tutmaz) ve videodan önce yeniden çizilir.
- Video (FL2VA): sonraki çekimin geçişi continuous ise last_frame = K(n+1); çekimin son karesi sonrakinin ilk karesi olur.
  Değilse (kesme, yeni sahne, sekansın son çekimi) video sonraki kareye "yolculuk" yapmaz: last_frame = çekim sonu karesi
  (Kn_end; Kn'den çekimin kendi eylemi tamamlanmış hâli), proje ayarı chain_end_frame kapalıysa last_frame yok.

Otomatik sınıflama (classify_shots; ayrıştırıcı, ingest ve panel aynı kuralı kullanır):
- Sahne başlığının ilk çekimi new_scene.
- Zincirden gelen karakterler: önceki çekimin (etkin) kadrosunda olan, bu çekimde adı geçmeyen ve senaryoda çıktığı
  yazılmayan karakterler, bu çekimin yazılı kadrosu önceki kadronun alt kümesiyse kadroya eklenir (Kıvılcım SH05: Bolt
  yazılı, Mila K4'ten karede kalır -> Mila "zincirden gelen", referansı, değişmezleri ve kimlik kapısıyla). Yazılı kadro
  alt küme değilse (ör. Mila'dan Bolt'a geçiş) önceki kadrodan kalanlar kareden çıkarılır (edit istemi açıkça ister).
- Aynı mekân + aynı kadro (önceki çekimin etkin kadrosu = bu çekimin yazılı kadrosu) + aynı kamera ölçeği -> continuous;
  kadro ya da kadraj değişirse -> cut. Kıvılcım: SH04 (Mila+Bolt) -> SH05 (yazılı Bolt; Mila zincirden gelir) kesme,
  SH05 (etkin Bolt+Mila) -> SH06 (Bolt+Mila) devam.
- Senaryodaki "CUT TO:" / "KESME:" kesmeyi, "CONTINUOUS:" / "DEVAM:" devamı zorlar (transition_hint); yönetmen Kurgu
  sayfasından ayrıca seçebilir (transition_in). Kare zinciri (Kn, K(n-1)'den) her iki durumda da sürer: dünya tutarlılığı.
"""
import hashlib
import re

from sqlalchemy import select

TRANSITIONS = ("continuous", "cut", "new_scene")
CHAINED = ("continuous", "cut")
HINTS = ("continuous", "cut")
SCALE_TR = {"extreme_wide": "çok geniş", "wide": "geniş", "pov": "öznel", "medium": "orta", "over_shoulder": "omuz üstü",
            "close_up": "yakın", "extreme_close_up": "çok yakın"}
# Karakterin çekimden çıktığını söyleyen ifadeler (adıyla aynı cümlede)
LEAVE = re.compile(
    r"\b(?:leaves|left\s+the|exits?|exited|walks?\s+(?:out|away|off)|goes\s+out|steps?\s+out|runs?\s+(?:off|out|away)|"
    r"disappears?|vanish(?:es)?|is\s+gone|has\s+(?:left|gone)|departs?)\b|"
    r"(?:ayrıl|uzaklaş|gözden\s+kaybol|kaybol|çıkıp\s+gid|dışarı\s+çık|odadan\s+çık|terk\s+ed)", re.IGNORECASE)
SENTENCE = re.compile(r"(?<=[.!?…])\s+")


def leaves(text: str | None, name: str) -> bool:
    """Senaryo, karakterin bu çekimde çıktığını söylüyor mu (adı ve çıkış ifadesi aynı cümlede)."""
    low_name = name.lower()
    return any(low_name in sentence.lower() and LEAVE.search(sentence) for sentence in SENTENCE.split(text or ""))


def scale_of(camera: dict | None) -> str | None:
    return (camera or {}).get("shot")


def _cast_text(names: list[str]) -> str:
    return "+".join(names) if names else "karaktersiz"


def classify_shots(shots: list[dict]) -> list[dict]:
    """Bir sekansın çekimleri (sırayla) -> her çekim için {transition, cast, carried, removed, reason}.
    Girdi: {"new_scene": bool, "location", "cast": [yazılı kadro], "camera": {...}, "hint": cut|continuous|None,
    "description"}. cast: etkin kadro (yazılı + zincirden gelen); removed: önceki karede olup bu çekimde olmayanlar."""
    out, prev, prev_cast = [], None, []
    for i, sh in enumerate(shots):
        stated = list(dict.fromkeys(sh.get("cast") or []))
        if prev is None or sh.get("new_scene"):
            out.append({"transition": "new_scene", "cast": stated, "carried": [], "removed": [],
                        "reason": "yeni sahne"})
            prev, prev_cast = sh, stated
            continue
        gone = [n for n in prev_cast if leaves(prev.get("description"), n)]
        carried = [n for n in prev_cast if n not in stated and n not in gone] \
            if stated and set(stated) < set(prev_cast) else []
        cast = stated + carried
        removed = [n for n in prev_cast if n not in cast]
        differences = []
        if (sh.get("location") or None) != (prev.get("location") or None):
            differences.append("mekân değişti")
        if set(stated) != set(prev_cast):               # kadraj yazılı kadroya göre: zincirden gelen karede kalır
            carry = f" ({_cast_text(carried)} zincirden gelir)" if carried else ""
            differences.append(f"kadro değişti: {_cast_text(prev_cast)} → {_cast_text(stated)}{carry}")
        if scale_of(sh.get("camera")) != scale_of(prev.get("camera")):
            a, b = scale_of(prev.get("camera")), scale_of(sh.get("camera"))
            differences.append(f"kadraj değişti: {SCALE_TR.get(a, a)} → {SCALE_TR.get(b, b)}")
        hint = sh.get("hint") if sh.get("hint") in HINTS else None
        if hint:
            transition = hint
            reason = "senaryoda " + ("CONTINUOUS" if hint == "continuous" else "CUT TO")
        elif differences:
            transition, reason = "cut", "; ".join(differences)
        else:
            transition, reason = "continuous", f"kadro aynı: {_cast_text(cast)}"
        out.append({"transition": transition, "cast": cast, "carried": carried, "removed": removed, "reason": reason})
        prev, prev_cast = sh, cast
    return out


def transition_of(scene) -> str:
    explicit = getattr(scene, "transition_in", None)
    if explicit in TRANSITIONS:
        return explicit
    auto = getattr(scene, "transition_auto", None)
    return auto if auto in TRANSITIONS else "continuous"


def neighbours(s, scene) -> tuple:
    """(önceki, sonraki) çekim: aynı proje ve aynı sekans, seq_order sırasıyla."""
    from app.models import Scene
    q = select(Scene).where(Scene.project_id == scene.project_id)
    if getattr(scene, "sequence_id", None) is not None:
        q = q.where(Scene.sequence_id == scene.sequence_id)
    shots = list(s.scalars(q.order_by(Scene.seq_order, Scene.scene_code)))
    ids = [sc.id for sc in shots]
    if scene.id not in ids:
        return None, None
    i = ids.index(scene.id)
    return (shots[i - 1] if i > 0 else None), (shots[i + 1] if i + 1 < len(shots) else None)


def parent_of(s, scene):
    """Zincirde bu çekimin karesinin çizildiği önceki çekim (geçiş continuous ya da cut ise), yoksa None."""
    if transition_of(scene) not in CHAINED:
        return None
    previous, _ = neighbours(s, scene)
    return previous


def ancestors(s, scene) -> list:
    """Zincirin başından bu çekimin hemen öncesine kadar çekimler (en eskisi önce)."""
    out, cur, seen = [], scene, set()
    while (parent := parent_of(s, cur)) is not None and parent.id not in seen:
        seen.add(parent.id)
        out.append(parent)
        cur = parent
    return list(reversed(out))


def successors(s, scene) -> list:
    """Bu çekimin karesinden zincirle türeyen sonraki çekimler (ilk new_scene'e kadar)."""
    out, cur = [], scene
    while True:
        _, nxt = neighbours(s, cur)
        if nxt is None or transition_of(nxt) not in CHAINED:
            return out
        out.append(nxt)
        cur = nxt


def next_continuous(s, scene):
    """FL2VA last_frame için: sonraki çekim aynı sekansta ve geçişi continuous ise o çekim."""
    _, nxt = neighbours(s, scene)
    return nxt if nxt is not None and transition_of(nxt) == "continuous" else None


def stale(scene, parent) -> bool:
    """Kare var ama zincirdeki yeri değişmiş: önceki kare yeniden çizildi, geçiş değişti ya da zincir yeni kuruldu."""
    if not getattr(scene, "board_key", None):
        return False
    expected = getattr(parent, "board_sha", None) if parent is not None else None
    return (getattr(scene, "board_parent", None) or None) != (expected or None)


def sha_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- veritabanı: sınıflama ve zincirden gelen karakterler ----------

def sequence_shots(s, project_id) -> dict:
    """{sekans (ya da proje) kimliği: [çekim, ...]} seq_order sırasıyla."""
    from app.models import Scene
    groups: dict = {}
    for sc in s.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order, Scene.scene_code)):
        groups.setdefault(sc.sequence_id or sc.project_id, []).append(sc)
    return groups


def shot_inputs(s, scenes: list) -> list[dict]:
    """Sınıflamanın girdileri veritabanından: yazılı kadro (zincirden gelenler hariç), mekân, kamera, senaryo ipucu."""
    from app.models import Asset, Character, SceneAsset, SceneCharacter
    ids = [sc.id for sc in scenes]
    cast: dict = {i: [] for i in ids}
    for scene_id, name in s.execute(select(SceneCharacter.scene_id, Character.name)
                                    .join(Character, Character.id == SceneCharacter.character_id)
                                    .where(SceneCharacter.scene_id.in_(ids), SceneCharacter.role != "chain")
                                    .order_by(Character.name)):
        cast[scene_id].append(name)
    place: dict = {}
    for scene_id, name in s.execute(select(SceneAsset.scene_id, Asset.name).join(Asset, Asset.id == SceneAsset.asset_id)
                                    .where(SceneAsset.scene_id.in_(ids), SceneAsset.role == "location")
                                    .order_by(Asset.name)):
        place.setdefault(scene_id, name)
    # Yazılı kadro sırası: önce sahnenin açıklamasında geçenler (ana karakter), sonra diğerleri
    def ordered(sc):
        text = (sc.description or "").lower()
        return sorted(cast[sc.id], key=lambda n: (text.find(n.lower()) if n.lower() in text else 10 ** 6, n))
    return [{"new_scene": sc.transition_auto == "new_scene", "location": place.get(sc.id), "cast": ordered(sc),
             "camera": dict(sc.camera or {}), "hint": sc.transition_hint, "description": sc.description}
            for sc in scenes]


def plan_of(s, scene) -> dict:
    """Bu çekimin sınıflaması (kayıt yazmadan): {transition, cast, carried, removed, reason}."""
    shots = sequence_shots(s, scene.project_id).get(scene.sequence_id or scene.project_id, [])
    ids = [sc.id for sc in shots]
    if scene.id not in ids:
        return {"transition": "new_scene", "cast": [], "carried": [], "removed": [], "reason": ""}
    return classify_shots(shot_inputs(s, shots))[ids.index(scene.id)]


def sync_seams(s, project_id) -> list[str]:
    """Projenin bütün sekanslarını yeniden sınıflar: transition_auto ve gerekçesi yazılır, zincirden gelen karakterler
    scene_characters'a role='chain' ile eklenir (ya da artık gelmiyorsa çıkarılır). Değişen çekim kodları döner."""
    from sqlalchemy import delete
    from app.models import Character, SceneCharacter
    by_name = {c.name: c.id for c in s.scalars(select(Character).where(Character.project_id == project_id))}
    changed = []
    for shots in sequence_shots(s, project_id).values():
        for sc, plan in zip(shots, classify_shots(shot_inputs(s, shots))):
            current = set(s.scalars(select(Character.name).join(SceneCharacter, SceneCharacter.character_id == Character.id)
                                    .where(SceneCharacter.scene_id == sc.id, SceneCharacter.role == "chain")))
            if sc.transition_auto != plan["transition"] or sc.transition_reason != plan["reason"] \
                    or current != set(plan["carried"]):
                changed.append(sc.scene_code)
            sc.transition_auto, sc.transition_reason = plan["transition"], plan["reason"]
            if current != set(plan["carried"]):
                s.execute(delete(SceneCharacter).where(SceneCharacter.scene_id == sc.id, SceneCharacter.role == "chain"))
                for name in plan["carried"]:
                    if name in by_name:
                        s.add(SceneCharacter(scene_id=sc.id, character_id=by_name[name], role="chain", dialogue=[]))
        s.flush()
    return changed
