"use client";
/* Storyboard sayfası (storyboard yazılımları gibi): sekans sekans kare ızgarası; her karede kamera, süre, diyalog,
   yönetmen notu ve onay. Üst araç çubuğu kareleri hazırlar, PDF verir, taslak ve iyileştirme geçişlerini başlatır.
   "Animatik" sekmesi onaylı kareleri (yoksa çekim kartını) geçici ya da final sesle, sesin süresine göre dizer; ücretsiz
   modda yerel olarak birleştirilir. Toplam süre ve sekans süreleri görünür. */
import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { BOARD_TR, MOVE_TR, SHOT_TR } from "@/lib/i18n";
import { pickMedia, posterOf } from "@/lib/media";
import { usePanel } from "@/lib/panel";
import { passText, stripText } from "@/lib/text";
import { timecode } from "@/lib/timeline";
import { secTr } from "@/lib/voice";
import type { DispatchAll, Film, Scene, StoryboardPlan, StripApproval } from "@/lib/types";
import { SafeAreas, SafeToggle } from "./Broadcast";
import { FlowBridge } from "./FlowBridge";
import { NoteBox, useSynced } from "./ui";

const TONE: Record<string, string> = { approved: "ok", changes_requested: "bad", pending: "" };
const SEAM: Record<string, { mark: string; title: string }> = {
  continuous: { mark: "→", title: "Devam: video sonraki çekimin karesinde biter" },
  cut: { mark: "✂", title: "Kesme: video kendi son karesinde biter" },
  new_scene: { mark: "‖", title: "Yeni sahne: zincir kırılır" },
};

/* Storyboard önce: zincirin bütün kareleri (ilk kareler ve son kareler) tek şeritte; geçişler arada. */
function Strip({ scenes }: { scenes: Scene[] }) {
  const { selectScene, setPage } = usePanel();
  return (
    <div className="strip" role="list" aria-label="Zincir şeridi">
      {scenes.map((sc) => (
        <div key={sc.id} className="strip-shot" role="listitem">
          {sc.transition && <span className="strip-seam" title={`${SEAM[sc.transition]?.title ?? sc.transition}${sc.transition_reason ? ` (${sc.transition_reason})` : ""}`}>{SEAM[sc.transition]?.mark ?? "·"}</span>}
          <button type="button" className={`strip-frame ${sc.board_status}`} onClick={() => { selectScene(sc.id); setPage("edit"); }}
            title={`${sc.scene_code}: ilk kare · ${BOARD_TR[sc.board_status] || sc.board_status}${sc.board_note ? ` · ${sc.board_note}` : ""}`}>
            {sc.board_url ? <img src={sc.board_url} alt={`${sc.scene_code} ilk kare`} loading="lazy" /> : <span className="ph">Kare yok</span>}
            <span className="code">{sc.scene_code.replace(/^S\d+_/, "")}</span>
          </button>
          {sc.end_board_url && (
            <button type="button" className={`strip-frame end ${sc.end_board_note && !sc.end_board_note.startsWith("İnceleme") ? "changes_requested" : ""}`}
              onClick={() => { selectScene(sc.id); setPage("edit"); }}
              title={`${sc.scene_code}: son kare${sc.end_board_note ? ` · ${sc.end_board_note}` : ""}`}>
              <img src={sc.end_board_url} alt={`${sc.scene_code} son kare`} loading="lazy" style={{ opacity: sc.end_board_current ? 1 : 0.5 }} />
              <span className="code">son</span>
            </button>
          )}
        </div>
      ))}
    </div>
  );
}

/** Referans modunda kare yerine kazanan klip: küçük resim, üzerine gelince sessiz oynar. */
function HoverClip({ sc }: { sc: Scene }) {
  const [on, setOn] = useState(false);
  const poster = posterOf(sc);
  const media = pickMedia(sc, null);
  if (!poster && !media?.src) return <div className="ph">Henüz video yok</div>;
  return (
    <span className="clipbox" onMouseEnter={() => setOn(true)} onMouseLeave={() => setOn(false)}>
      {on && media?.src ? <video src={media.src} poster={poster ?? undefined} muted autoPlay loop playsInline />
        : poster ? <img src={poster} alt={`${sc.scene_code} kazanan klip`} loading="lazy" /> : <div className="ph">Henüz video yok</div>}
      <span className="tag ok corner">{sc.status === "done" ? "filmdeki klip" : "son klip"}</span>
    </span>
  );
}

function BoardCard({ sc, refMode }: { sc: Scene; refMode: boolean }) {
  const { busy, act, note, selectScene, setPage, admin } = usePanel();
  const [text, setText] = useSynced(sc.board_note || "", `${sc.id}:${sc.board_note ?? ""}`);   // yazılan not yoklamayla ezilmez
  const cam = sc.camera_effective || sc.camera || {};
  const lines = sc.tracks.filter((t) => t.kind === "dialogue");
  const decide = (status: string) => act(async () => {
    const r = await api.post<{ board_status: string; board_note: string | null }>(`/scenes/${sc.id}/board`, { status, note: text });
    note("board", `${sc.scene_code}: ${BOARD_TR[r.board_status]}${r.board_note ? ": " + r.board_note : ""}${r.board_status === "changes_requested" ? ". \"Kareleri hazırla\" bu kareyi notunuzla yeniden çizer (zincirde sonrakiler de)." : ""}`, r.board_status === "changes_requested" ? "err" : "ok");
  }, "board");
  return (
    <article className={`board ${sc.board_status}`} aria-label={`${sc.scene_code} storyboard karesi`}>
      <div className="frame">
        {sc.board_url ? <img src={sc.board_url} alt={`${sc.scene_code} storyboard karesi`} loading="lazy" />
          : refMode ? <HoverClip sc={sc} /> : <div className="ph">Kare yok · "Kareleri hazırla"</div>}
        <SafeAreas />
        <span className="code">{sc.scene_code}</span>
        <button className="open" type="button" onClick={() => { selectScene(sc.id); setPage("edit"); }}>Kurguda aç</button>
      </div>
      <div className="row">
        <span className="dim small grow">{SHOT_TR[cam.shot ?? ""] || cam.shot || ""}, {MOVE_TR[cam.movement ?? ""] || cam.movement || ""} · <span className="mono">{Number(sc.duration_s).toFixed(1)} sn</span>{sc.duration_source === "voice" && <span className="tag info" style={{ marginLeft: 4 }}>sesten</span>}</span>
        {refMode ? (sc.board_status === "changes_requested" ? <span className="st bad">not kaydedildi</span> : sc.upgrade_requested ? <span className="st info">iyileştirilecek</span> : null)
          : <span className={`st ${TONE[sc.board_status] ?? ""}`}>{BOARD_TR[sc.board_status] || sc.board_status}</span>}
      </div>
      <p className="small">{sc.description}</p>
      {admin && (sc.transition_reason || (sc.chain_cast?.length ?? 0) > 0) && (
        <p className="faint small">
          {sc.transition_reason && <span>{SEAM[sc.transition ?? ""]?.mark} {sc.transition_reason}</span>}
          {(sc.chain_cast?.length ?? 0) > 0 && <span className="tag info" style={{ marginLeft: 6 }}>zincirden gelen: {sc.chain_cast!.join(", ")}</span>}
        </p>
      )}
      <p className="dim small">
        {lines.length ? lines.map((t, i) => <span key={t.id}>{i > 0 && <br />}<b>{t.character || "Anlatıcı"}:</b> {t.text}</span>) : "Diyalog yok."}
      </p>
      <textarea aria-label={`${sc.scene_code} yönetmen notu`} placeholder={refMode ? "Yönetmen notu: yeniden üretimde isteme girer" : "Yönetmen notu"} maxLength={2000} value={text} onChange={(e) => setText(e.target.value)} />
      <div className="row">
        {refMode ? (
          <>
            <button className="btn" type="button" disabled={busy || !text.trim()} title="Notu kaydeder ve çekimi değişiklik istendi olarak işaretler; bir sonraki üretimde istem bu notu taşır"
              onClick={() => decide("changes_requested")}>Notu kaydet</button>
            <button className={`btn${sc.upgrade_requested ? " good" : ""}`} type="button" disabled={busy} aria-pressed={sc.upgrade_requested}
              title="İyileştirme geçişinde yalnızca işaretli çekimler tam turla üretilir"
              onClick={() => act(async () => {
                const r = await api.post<{ upgrade_requested: boolean }>(`/scenes/${sc.id}/upgrade`, { requested: !sc.upgrade_requested });
                note("board", r.upgrade_requested ? `${sc.scene_code} iyileştirme geçişine alınacak.` : `${sc.scene_code} işareti kaldırıldı.`, "ok");
              }, "board")}>{sc.upgrade_requested ? "İyileştirilecek" : "İyileştir"}</button>
          </>
        ) : (
          <>
            <button className="btn good" type="button" disabled={busy || !sc.board_url} onClick={() => decide("approved")}>Onayla</button>
            <button className="btn danger" type="button" disabled={busy} onClick={() => decide("changes_requested")}>Değişiklik iste</button>
          </>
        )}
        {sc.board_status !== "pending" && <button className="btn ghost" type="button" disabled={busy} onClick={() => decide("pending")}>Geri al</button>}
      </div>
    </article>
  );
}

/** Animatik: oynatıcı, kesme şeridi ve sekans süreleri. Birleştirme kuyruğa verilir; film adresi değişince biter. */
function Animatic() {
  const { ov, projectId, busy, act, note } = usePanel();
  const video = useRef<HTMLVideoElement | null>(null);
  const [now, setNow] = useState(0);
  const [waiting, setWaiting] = useState<{ before: string | null; at: number } | null>(null);
  const film: Film | null = ov?.animatic ?? null;
  const pending = Boolean(waiting && film?.url === waiting.before && Date.now() - waiting.at < 120000);
  useEffect(() => { if (waiting && !pending) setWaiting(null); }, [waiting, pending]);
  if (!ov) return null;
  const cut = film?.cut ?? null;
  const clips = cut?.clips ?? [];
  const total = cut?.duration_s ?? (clips.length ? clips[clips.length - 1].out_s : 0);
  const build = () => act(async () => {
    setWaiting({ before: film?.url ?? null, at: Date.now() });
    await api.post(`/projects/${projectId}/animatic`);
    note("board", "Animatik birleştirmeye alındı: seslendirilmemiş replikler önce geçici sesle üretilir.", "ok");
  }, "board");
  return (
    <div className="animatic">
      <div className="stack">
        {film ? <video key={film.url} ref={video} src={film.url} controls preload="metadata" playsInline onTimeUpdate={(e) => setNow(e.currentTarget.currentTime)} />
          : <p className="empty">Henüz animatik yok. Onaylı kareler (yoksa çekim kartları) sesle dizilir; birleştirmek ücretsizdir.</p>}
        {clips.length > 0 && (
          <div className="cutbar" aria-label="Animatik çekimleri">
            {clips.map((c) => (
              <button key={c.scene_code} type="button" className={`cut${now >= c.in_s && now < c.out_s ? " active" : ""}`}
                style={{ flex: `${Math.max(0.1, c.out_s - c.in_s)} 1 0`, background: c.still === "board" ? "#3a5f9e" : "#3a3c42" }}
                title={`${c.scene_code} · ${secTr(c.out_s - c.in_s)} · ${c.still === "board" ? "onaylı kare" : "çekim kartı"} · ${c.timing === "voice" ? "süre sesten" : "çekim süresi"}`}
                onClick={() => { const v = video.current; if (v) { v.currentTime = c.in_s + 0.05; v.play().catch(() => {}); } }}>{c.scene_code}</button>
            ))}
          </div>
        )}
      </div>
      <div className="stack anim-seq">
        <button className="btn primary" type="button" disabled={busy || pending} onClick={() => void build()}>{pending ? "Birleştiriliyor…" : film ? "Animatiği yeniden birleştir" : "Animatiği birleştir"}</button>
        <div className="row"><b>Toplam</b><span className="mono">{timecode(total)} · {secTr(total)}</span></div>
        {(cut?.sequences ?? []).map((q) => (
          <div key={q.code} className="row"><span>{q.code} · {q.title}</span><span className="mono">{secTr(q.out_s - q.in_s)}</span></div>
        ))}
        {cut && <p className="faint small">{cut.boards ?? 0} onaylı kare, {clips.length - (cut.boards ?? 0)} çekim kartı · {cut.voiced ?? 0} çekimin süresi sesten.</p>}
        <p className="faint small">Süre: çekimin bütün replikleri seslendirildiyse giriş payı + replikler + çıkış payı, değilse çekim süresi. Ses ve kareler değişince yeniden birleştir.</p>
      </div>
    </div>
  );
}

export function BoardPage() {
  const { ov, projectId, busy, frozen, act, note, meta, admin } = usePanel();
  const [tab, setTab] = useState<"frames" | "animatic">("frames");
  if (!ov) return null;
  const refMode = meta?.video_input_mode === "reference";
  const scenes = ov.scenes;
  const approved = scenes.filter((x) => x.board_status === "approved").length;
  const flagged = scenes.filter((x) => x.upgrade_requested).length;
  const drafts = scenes.filter((x) => x.cut_tier === "draft").length;
  const required = Boolean(ov.project.settings.require_board_approval);
  const groups = ov.sequences.map((q) => ({ id: q.id, title: `${q.code} · ${q.title}`, scenes: scenes.filter((s) => s.sequence_id === q.id) }));
  const loose = scenes.filter((s) => !ov.sequences.some((q) => q.id === s.sequence_id));
  if (loose.length) groups.push({ id: "none", title: "Sekanssız", scenes: loose });
  return (
    <div className="page" data-tour="board">
      <section className="pnl" aria-labelledby="board-title">
        <header className="pnl-h">
          <h2 id="board-title">Storyboard</h2>
          <div className="seg" role="tablist" aria-label="Storyboard görünümü" data-tour="board-animatic">
            <button type="button" role="tab" aria-selected={tab === "frames"} aria-pressed={tab === "frames"} onClick={() => setTab("frames")}>Kareler</button>
            <button type="button" role="tab" aria-selected={tab === "animatic"} aria-pressed={tab === "animatic"} onClick={() => setTab("animatic")}>Animatik</button>
          </div>
          <span className="meta">{refMode ? `${scenes.filter((x) => x.status === "done").length}/${scenes.length} çekim hazır` : `${approved}/${scenes.length} kare onaylı`} · {drafts} taslak çekim · {flagged} iyileştirilecek</span>
          <span className="grow" />
          {!refMode && <button className="btn" type="button" disabled={busy || frozen} onClick={() => act(async () => {
            const plan = await api.get<StoryboardPlan>(`/projects/${projectId}/storyboard/plan`);
            if (plan.requested.length || plan.clips.length) {
              const lines = [
                plan.requested.length ? `Değişiklik istenen kareler yeni seed'lerle ve notunuzla yeniden çizilecek: ${plan.requested.join(", ")}.` : "",
                plan.followers.length ? `Zincirde onlardan türeyen kareler de yeniden çizilecek: ${plan.followers.join(", ")}.` : "",
                plan.clips.length ? `Eski kareden kalacak klipler (yeniden üretilecek): ${plan.clips.join(", ")}.` : "",
                `Toplam ${plan.frames.length} kare${plan.ends.length ? ` + ${plan.ends.length} son kare` : ""}, tahmini $${plan.estimate_usd.toFixed(2)}. Devam edilsin mi?`,
              ].filter(Boolean);
              if (!window.confirm(lines.join("\n"))) return;
            }
            const r = await api.post<{ queued: string[] }>(`/projects/${projectId}/storyboard`);
            note("board", r.queued.length ? `Kareler hazırlanıyor: ${r.queued.join(", ")}.` : "Bütün kareler güncel.", "ok");
          }, "board")}>Kareleri hazırla</button>}
          <SafeToggle />
          <a className="btn" href={`/api/projects/${projectId}/storyboard.pdf`} download="storyboard.pdf">PDF indir</a>
          <span className="vsep" />
          {!refMode && <label className="check">
            <input type="checkbox" checked={required} disabled={busy} onChange={(e) => {
              const on = e.target.checked;
              void act(async () => {
                await api.patch(`/projects/${projectId}`, { settings: { require_board_approval: on } });
                note("board", on ? "Onaylanmamış karesi olan çekimde görüntü üretilmez." : "Kare onayı artık zorunlu değil.", "ok");
              }, "board");
            }} /> Görüntü üretimi için kare onayı zorunlu
          </label>}
          {!refMode && <span className="vsep" />}
          {!refMode && <button className="btn primary" type="button" disabled={busy || !scenes.some((x) => x.board_url)}
            title="Şeritteki bütün kareleri (ilk ve son kareler) onaylar, sonra videoları başlatır. İnceleme bekleyen kare onaylanmaz."
            onClick={() => act(async () => {
              const r = await api.post<StripApproval>(`/projects/${projectId}/boards/approve`);
              const t = stripText(r);
              note("board", t.text, t.bad ? "err" : "ok");
            }, "board")}>Şeridi onayla ve videoları başlat</button>}
          <button className="btn" type="button" disabled={busy || frozen} onClick={() => act(async () => {
            const r = await api.post<DispatchAll>(`/projects/${projectId}/draft-pass`);
            note("board", passText(r, "Taslak geçiş") + " Son çekim bitince kaba kurgu kendiliğinden birleşir.", "ok");
          }, "board")}>Taslak geçişi başlat</button>
          <button className="btn" type="button" disabled={busy || frozen || !flagged} onClick={() => act(async () => {
            const r = await api.post<DispatchAll>(`/projects/${projectId}/upgrade-pass`);
            note("board", passText(r, "Tam tura alınan"), "ok");
          }, "board")}>{flagged ? `İyileştirme geçişi (${flagged} çekim)` : "İyileştirme geçişi"}</button>
        </header>
        {tab === "animatic" ? <div className="pnl-b"><div className="pad"><NoteBox id="board" /></div><Animatic /></div> : <div className="pnl-b">
          <div className="pad stack">
            {refMode ? (
              <p className="faint small">
                Referans modu: storyboard karesi çizilmez; video doğrudan onaylı referans görselleri ve senaryodan üretilir, üretimden önce
                plan Kurgu'daki denetçiden onaylanır. Burada her çekimin filmdeki klibi, tarifi ve diyaloğu bir arada: üzerine gelince oynar,
                zayıf çekimi <b>İyileştir</b> ile işaretle, yeniden üretimde isteme girecek notu <b>Notu kaydet</b> ile bırak.
              </p>
            ) : admin && (
              <p className="faint small">
                Revizyonu en ucuz aşamada yap: her çekimin karesini onayla ya da not bırakıp değişiklik iste. Taslak geçiş yalnızca test
                turuyla kaba kurguyu çıkarır; zayıf çekimleri Kurgu sayfasında &quot;İyileştir&quot; ile işaretleyip yalnızca onları tam turla üretirsin.
              </p>
            )}
            <NoteBox id="board" />
            <FlowBridge scenes={scenes} />
            <NoteBox id="flow" />
          </div>
          {!refMode && groups.filter((g) => g.scenes.length && g.scenes.some((x) => x.board_url)).map((g) => (
            <section key={`strip-${g.id}`} aria-label={`${g.title} zincir şeridi`}>
              <h3 className="board-seq">{g.title} · zincir şeridi</h3>
              <Strip scenes={g.scenes} />
            </section>
          ))}
          {groups.filter((g) => g.scenes.length).map((g) => (
            <section key={g.id}>
              <h3 className="board-seq">{g.title}</h3>
              <div className="boards">{g.scenes.map((sc) => <BoardCard key={sc.id} sc={sc} refMode={refMode} />)}</div>
            </section>
          ))}
        </div>}
      </section>
    </div>
  );
}
