"""Müşteri onay döngüsü: onay kapıları, film sürümleri (v001, v002…), süreli salt okunur inceleme bağlantıları ve zaman
kodlu müşteri notları.

Kapılar: Senaryo, Tasarım (varlıklar), Animatik, Taslak kurgu, Final, Kabul. Her kapının durumu bekliyor, onaylandı ya
da değişiklik istendi. Video kapıları (animatik, taslak kurgu, final, kabul) müşteriye bir sürümle gider: sürüm
gönderildiği anki birleştirilmiş filmi gösterir. Senaryo ve tasarım kapıları panelden işaretlenir (müşterinin başka
kanaldan gelen kararı). Revizyon turu, kapıdaki değişiklik isteği sayısıdır; tur sınırı proje ayarıdır (`revision_rounds`,
varsayılan 2). Turun maliyeti, önceki gönderimden bu yana yapılan (sanal) harcamadır.

İnceleme bağlantısı belirteçlidir ve süreli; açılan sayfa yalnızca oynatıcıyı, sürüm adını, zaman kodlu not alanını ve
"Onayla" / "Değişiklik iste" düğmelerini gösterir. Belirteçli uçlar (`/api/review/{token}…`) bütçe, motor, maliyet ya da
iç ayrıntı döndürmez. Müşterinin her kararı kürasyon kaydına düşer (client_note, client_approve, client_reject).
"""
import datetime as dt
import json
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import storage
from app.api.deps import ProjectId, project_or_404
from app.budget import killswitch
from app.config import project_settings
from app.curation import curated, record
from app.db import get_session
from app.models import ClientGate, ClientNote, FilmVersion, Project, ReviewLink, Scene
from app.schemas import ClientDecisionIn, ClientNoteIn, GateIn, NoteResolveIn, ReviewLinkIn, VersionIn

router = APIRouter(tags=["review"])

GATES = ("script", "design", "animatic", "draft", "final", "acceptance")
GATE_TR = {"script": "Senaryo", "design": "Tasarım", "animatic": "Animatik", "draft": "Taslak kurgu", "final": "Final",
           "acceptance": "Kabul"}
# Video kapılarının filmi: sürüm gönderildiği anki birleştirilmiş filmi taşır.
GATE_FILM = {"animatic": ("animatic", "animatic_key"), "draft": ("draft", "draft_timeline_key"),
             "final": ("final", "timeline_key"), "acceptance": ("final", "timeline_key")}
STATUS_TR = {"pending": "bekliyor", "approved": "onaylandı", "changes_requested": "değişiklik istendi"}
LINK_DAYS = 7


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _cut(key: str | None) -> dict | None:
    if not key:
        return None
    try:
        return json.loads(storage.read_bytes(key.rsplit(".", 1)[0] + ".json"))
    except Exception:                                   # kesme listesi okunamıyor: sürüm yine gönderilir
        return None


def gate_row(s: Session, project_id: str, gate: str, lock: bool = False) -> ClientGate:
    q = select(ClientGate).where(ClientGate.project_id == project_id, ClientGate.gate == gate)
    row = s.scalar(q.with_for_update() if lock else q)
    if row is None:
        row = ClientGate(project_id=project_id, gate=gate, status="pending", rounds=0)
        s.add(row)
        s.flush()
    return row


def clip_at(cut: dict | None, at_s: float | None) -> dict | None:
    """Filmdeki zamanın düştüğü kesme (kesme listesinden); filmin sonundan sonrası son kesmeye düşer."""
    if not cut or at_s is None:
        return None
    clips = cut.get("clips") or []
    for c in clips:
        if c["in_s"] <= at_s < c["out_s"]:
            return c
    return clips[-1] if clips and at_s >= clips[-1]["out_s"] else None


def version_out(v: FilmVersion, cost: float | None = None) -> dict:
    return {"id": str(v.id), "number": v.number, "label": v.label, "gate": v.gate, "gate_tr": GATE_TR[v.gate],
            "kind": v.kind, "url": f"/api/media?key={v.video_key}", "status": v.status, "status_tr": STATUS_TR.get(
                "changes_requested" if v.status == "changes_requested" else v.status, v.status),
            "created_at": v.created_at.timestamp() if v.created_at else None,
            "decided_at": v.decided_at.timestamp() if v.decided_at else None, "cost_usd": cost,
            "duration_s": (v.cut or {}).get("duration_s")}


def note_out(n: ClientNote) -> dict:
    return {"id": str(n.id), "version_id": str(n.version_id) if n.version_id else None, "gate": n.gate,
            "gate_tr": GATE_TR.get(n.gate, n.gate), "at_s": n.at_s, "shot_offset_s": n.shot_offset_s,
            "scene_id": str(n.scene_id) if n.scene_id else None,
            "scene_code": n.scene_code, "text": n.text, "resolved": n.resolved,
            "created_at": n.created_at.timestamp() if n.created_at else None}


def state(s: Session, project_id: str) -> dict:
    """Teslim sayfasının müşteri bölümü: kapılar (durum, tur, tur maliyetleri), sürümler, bağlantılar, notlar."""
    p = s.get(Project, project_id)
    limit = int(project_settings(p.settings).get("revision_rounds", 2))
    versions = list(s.scalars(select(FilmVersion).where(FilmVersion.project_id == project_id).order_by(FilmVersion.number)))
    costs, previous = {}, 0.0
    for v in versions:                                  # turun maliyeti: önceki gönderimden bu yana yapılan harcama
        costs[v.id] = round(max(0.0, float(v.spent_usd) - previous), 4)
        previous = float(v.spent_usd)
    rows = {g.gate: g for g in s.scalars(select(ClientGate).where(ClientGate.project_id == project_id))}
    gates = []
    for g in GATES:
        row = rows.get(g)
        mine = [v for v in versions if v.gate == g]
        gates.append({"gate": g, "label": GATE_TR[g], "status": row.status if row else "pending",
                      "rounds": row.rounds if row else 0, "limit": limit,
                      "over_limit": bool(row and row.rounds > limit), "note": row.note if row else None,
                      "video": g in GATE_FILM, "versions": [version_out(v, costs[v.id]) for v in mine],
                      "cost_usd": round(sum(costs[v.id] for v in mine), 4)})
    links = s.scalars(select(ReviewLink).where(ReviewLink.project_id == project_id).order_by(ReviewLink.created_at.desc()))
    labels = {v.id: v.label for v in versions}
    notes = s.scalars(select(ClientNote).where(ClientNote.project_id == project_id).order_by(ClientNote.created_at))
    return {"gates": gates, "versions": [version_out(v, costs[v.id]) for v in versions], "round_limit": limit,
            "links": [{"id": str(x.id), "token": x.token, "url": f"/panel/review/?token={x.token}",
                       "version_id": str(x.version_id), "label": labels.get(x.version_id),
                       "expires_at": x.expires_at.timestamp(), "revoked": x.revoked_at is not None,
                       "expired": x.expires_at <= now()} for x in links],
            "notes": [note_out(n) for n in notes]}


def summary(s: Session, project_id: str) -> dict:
    """Overview için kısa özet: kapıların durumu ve gönderilip gönderilmediği, notlar (Kurgu işaretçileri)."""
    p = s.get(Project, project_id)
    limit = int(project_settings(p.settings).get("revision_rounds", 2))
    rows = {g.gate: g for g in s.scalars(select(ClientGate).where(ClientGate.project_id == project_id))}
    sent = set(s.scalars(select(FilmVersion.gate).where(FilmVersion.project_id == project_id)))
    labels = dict(s.execute(select(FilmVersion.id, FilmVersion.label).where(FilmVersion.project_id == project_id)).all())
    notes = s.scalars(select(ClientNote).where(ClientNote.project_id == project_id).order_by(ClientNote.created_at))
    return {"gates": [{"gate": g, "status": rows[g].status if g in rows else "pending",
                       "rounds": rows[g].rounds if g in rows else 0, "limit": limit, "sent": g in sent} for g in GATES],
            "notes": [{**note_out(n), "label": labels.get(n.version_id)} for n in notes]}


# ---------- yönetmen (panel) ----------

@router.get("/projects/{project_id}/client")
def get_client(project_id: ProjectId, s: Session = Depends(get_session)):
    project_or_404(s, project_id)
    return state(s, project_id)


@router.post("/projects/{project_id}/versions", status_code=201)
@curated("client_send")
def send_version(project_id: ProjectId, body: VersionIn, s: Session = Depends(get_session)):
    """Kapının filmini müşteriye gönderilecek sürüm olarak etiketler (v001, v002…). Sürüm o anki filmi gösterir."""
    s.execute(select(Project.id).where(Project.id == project_id).with_for_update())     # numara yarışı olmasın
    p = project_or_404(s, project_id)
    kind, attr = GATE_FILM[body.gate]
    key = getattr(p, attr)
    if not key:
        what = {"animatic": "animatik", "draft": "kaba kurgu", "final": "film"}[kind]
        raise HTTPException(409, f"Önce {what} birleştirilmeli; gönderilecek film yok.")
    number = (s.scalar(select(func.max(FilmVersion.number)).where(FilmVersion.project_id == project_id)) or 0) + 1
    v = FilmVersion(project_id=project_id, number=number, label=f"v{number:03d}", gate=body.gate, kind=kind,
                    video_key=key, cut=_cut(key), spent_usd=p.spent_usd, status="sent")
    s.add(v)
    gate = gate_row(s, project_id, body.gate, lock=True)
    gate.status, gate.updated_at = "pending", now()
    s.commit()
    killswitch.event(project_id, "version_sent", label=v.label, gate=body.gate)
    return version_out(v)


@router.post("/projects/{project_id}/review-links", status_code=201)
@curated("review_link")
def create_link(project_id: ProjectId, body: ReviewLinkIn, s: Session = Depends(get_session)):
    """Sürüm için süreli, salt okunur, belirteçli inceleme bağlantısı (/panel/review/?token=…)."""
    project_or_404(s, project_id)
    v = s.get(FilmVersion, body.version_id)
    if v is None or str(v.project_id) != project_id:
        raise HTTPException(404, "Sürüm bu projede yok")
    link = ReviewLink(project_id=project_id, version_id=v.id, token=secrets.token_urlsafe(24),
                      expires_at=now() + dt.timedelta(days=body.days))
    s.add(link)
    s.commit()
    return {"id": str(link.id), "token": link.token, "url": f"/panel/review/?token={link.token}", "label": v.label,
            "expires_at": link.expires_at.timestamp()}


@router.delete("/review-links/{link_id}")
@curated("review_link_revoke")
def revoke_link(link_id: str, s: Session = Depends(get_session)):
    link = s.get(ReviewLink, link_id)
    if link is None:
        raise HTTPException(404, "Bağlantı yok")
    link.revoked_at = link.revoked_at or now()
    s.commit()
    return {"id": link_id, "revoked": True, "project_id": str(link.project_id)}


@router.post("/projects/{project_id}/gates/{gate}")
@curated(lambda kw, r: "client_approve" if kw["body"].status == "approved" else "client_reject"
         if kw["body"].status == "changes_requested" else "client_gate_reset")
def set_gate(project_id: ProjectId, gate: str, body: GateIn, s: Session = Depends(get_session)):
    """Müşterinin başka kanaldan gelen kararını işler (ör. senaryo ve tasarım kapıları e-postayla). Değişiklik isteği
    yeni bir revizyon turudur."""
    project_or_404(s, project_id)
    if gate not in GATES:
        raise HTTPException(404, "Böyle bir onay kapısı yok")
    row = gate_row(s, project_id, gate, lock=True)
    if body.status == "changes_requested":
        row.rounds += 1
    row.status, row.note, row.updated_at = body.status, body.note, now()
    s.commit()
    killswitch.event(project_id, "gate_changed", gate=gate, status=body.status, source="panel")
    return {"gate": gate, "status": row.status, "rounds": row.rounds}


@router.patch("/client-notes/{note_id}")
@curated("client_note_resolve")
def resolve_note(note_id: str, body: NoteResolveIn, s: Session = Depends(get_session)):
    n = s.get(ClientNote, note_id)
    if n is None:
        raise HTTPException(404, "Not yok")
    n.resolved, n.resolved_at = body.resolved, now() if body.resolved else None
    s.commit()
    return {**note_out(n), "project_id": str(n.project_id)}


# ---------- müşteri (belirteçli, salt okunur sayfa) ----------

def link_or_error(s: Session, token: str) -> tuple[ReviewLink, FilmVersion, Project]:
    link = s.scalar(select(ReviewLink).where(ReviewLink.token == token)) if 16 <= len(token) <= 64 else None
    if link is None:
        raise HTTPException(404, "İnceleme bağlantısı bulunamadı.")
    if link.revoked_at is not None or link.expires_at <= now():
        raise HTTPException(410, "Bu inceleme bağlantısının süresi doldu ya da kapatıldı.")
    return link, s.get(FilmVersion, link.version_id), s.get(Project, link.project_id)


def public(link: ReviewLink, v: FilmVersion, p: Project, notes: list) -> dict:
    """Müşterinin gördüğü her şey: proje adı, sürüm, film, kendi notları ve karar. Bütçe, motor, maliyet yok."""
    clips = [{"scene_code": c["scene_code"], "in_s": c["in_s"], "out_s": c["out_s"]} for c in (v.cut or {}).get("clips") or []]
    return {"title": p.title, "label": v.label, "gate": v.gate, "gate_tr": GATE_TR[v.gate], "status": v.status,
            "video_url": f"/api/review/{link.token}/video", "expires_at": link.expires_at.timestamp(),
            "sent_at": v.created_at.timestamp() if v.created_at else None, "clips": clips,
            "notes": [{"id": str(n.id), "at_s": n.at_s, "scene_code": n.scene_code, "text": n.text,
                       "created_at": n.created_at.timestamp() if n.created_at else None} for n in notes]}


def _notes(s: Session, v: FilmVersion) -> list:
    return list(s.scalars(select(ClientNote).where(ClientNote.version_id == v.id).order_by(ClientNote.created_at)))


@router.get("/review/{token}")
def review(token: str, s: Session = Depends(get_session)):
    link, v, p = link_or_error(s, token)
    return public(link, v, p, _notes(s, v))


@router.api_route("/review/{token}/video", methods=["GET", "HEAD"])
def review_video(token: str, request: Request):
    """Sürümün filmi; medya ucuyla aynı (Range destekli) ama yalnızca geçerli belirteçle."""
    from app.api.panel import media
    from app.db import session_scope
    with session_scope() as s:
        _, v, _ = link_or_error(s, token)
        key = v.video_key
    return media(request, key=key)


@router.post("/review/{token}/notes", status_code=201)
def add_note(token: str, body: ClientNoteIn, s: Session = Depends(get_session)):
    """Müşterinin zaman kodlu notu: filmdeki zamandan çekime bağlanır; kürasyon kaydına client_note olarak düşer."""
    link, v, p = link_or_error(s, token)
    if v.status != "sent":
        raise HTTPException(409, f"{v.label} için karar verildi; yeni not eklenemez.")
    clip = clip_at(v.cut, body.at_s)
    code = clip["scene_code"] if clip else None
    sc = s.scalar(select(Scene).where(Scene.project_id == p.id, Scene.scene_code == code)) if code else None
    offset = round(min(max(0.0, body.at_s - clip["in_s"]), clip["out_s"] - clip["in_s"]), 3) if clip else None
    n = ClientNote(project_id=p.id, version_id=v.id, gate=v.gate, at_s=body.at_s, shot_offset_s=offset,
                   scene_id=sc.id if sc else None, scene_code=code, text=body.text.strip())
    s.add(n)
    s.commit()
    record(str(p.id), "client_note", scene_id=n.scene_id,
           payload={"version": v.label, "gate": v.gate, "at_s": body.at_s, "scene_code": code, "text": n.text})
    killswitch.event(str(p.id), "client_note", label=v.label, scene_id=str(n.scene_id) if n.scene_id else None,
                     code=code, at_s=body.at_s)
    return public(link, v, p, _notes(s, v))


@router.post("/review/{token}/decision")
def decide(token: str, body: ClientDecisionIn, s: Session = Depends(get_session)):
    """Müşterinin kararı: sürüm ve kapı onaylanır ya da değişiklik istenir (yeni revizyon turu). Bir sürüm için bir kez."""
    link, v, p = link_or_error(s, token)
    if v.status != "sent":
        raise HTTPException(409, f"{v.label} için karar zaten verildi ({STATUS_TR[v.status]}).")
    approved = body.decision == "approve"
    v.status, v.decided_at = ("approved" if approved else "changes_requested"), now()
    gate = gate_row(s, str(p.id), v.gate, lock=True)
    gate.status, gate.updated_at = v.status, now()
    if not approved:
        gate.rounds += 1
    if body.note:
        s.add(ClientNote(project_id=p.id, version_id=v.id, gate=v.gate, text=body.note.strip()))
    s.commit()
    record(str(p.id), "client_approve" if approved else "client_reject",
           payload={"version": v.label, "gate": v.gate, "note": body.note, "rounds": gate.rounds})
    killswitch.event(str(p.id), "client_decision", label=v.label, gate=v.gate, status=v.status)
    return public(link, v, p, _notes(s, v))
