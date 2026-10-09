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
"""
import hashlib

from sqlalchemy import select

TRANSITIONS = ("continuous", "cut", "new_scene")
CHAINED = ("continuous", "cut")


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
