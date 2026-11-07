"use client";
/* Storyboard sayfası (storyboard yazılımları gibi): sekans sekans kare ızgarası; her karede kamera, süre, diyalog,
   yönetmen notu ve onay. Üst araç çubuğu kareleri hazırlar, PDF verir, taslak ve iyileştirme geçişlerini başlatır.
   "Animatik" sekmesi çekimleri geçici ya da final sesle, sesin süresine göre dizer: görseli olan çekim (ilk kare, onaylı
   kare, kadro ve mekân referansları) sahnelenir (kesik kâğıt, duyguya göre hareket, kamera), olmayan kart kalır. Bir
   çekim seçilince sahneleme planı düzenlenir, önizlenir; kukla sürümü video modeline hareket rehberidir. Ücretsizdir. */
import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { BOARD_TR, MOVE_TR, SHOT_TR } from "@/lib/i18n";
import { pickMedia, posterOf } from "@/lib/media";
import { usePanel } from "@/lib/panel";
import { passText, stripText, usdTr } from "@/lib/text";
import { timecode } from "@/lib/timeline";
import { secTr } from "@/lib/voice";
import type { DispatchAll, Film, Scene, StagingGuide, StagingPreview, StagingView, StagingWritten, StoryboardPlan, StripApproval } from "@/lib/types";
import { SafeAreas } from "./Broadcast";
import { Icon } from "./icons";
import { NoteBox, ToolMenu, useSynced } from "./ui";

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

const STILL: Record<string, { color: string; tr: string }> = {
  motion: { color: "#2f7d63", tr: "hareketli sahneleme" }, board: { color: "#3a5f9e", tr: "onaylı kare" },
  card: { color: "#3a3c42", tr: "çekim kartı" },
};

/** Seçili çekimin sahneleme planı: kaynak (kurallar / yönetmen), JSON düzenleme, önizleme ve kukla rehber videosu. */
const SOURCE_TR: Record<string, string> = { director: "yönetmen planı", claude: "Claude planı", rules: "kurallardan" };

function Staging({ sceneId, onSaved }: { sceneId: string; onSaved: () => void }) {
  const { busy, act, note, meta } = usePanel();
  const [view, setView] = useState<StagingView | null>(null);
  const [text, setText] = useState("");
  const [preview, setPreview] = useState<StagingPreview | null>(null);
  const [working, setWorking] = useState<"" | "preview" | "guide" | "write">("");
  useEffect(() => {
    let live = true;
    setView(null); setPreview(null);
    api.get<StagingView>(`/scenes/${sceneId}/staging`).then((v) => {
      if (!live) return;
      setView(v); setText(v.plan ? JSON.stringify(v.plan, null, 1) : "");
    }).catch(() => {});
    return () => { live = false; };
  }, [sceneId]);
  if (!view) return <p className="faint small">Sahneleme okunuyor…</p>;
  const show = (v: StagingView) => { setView(v); setText(v.plan ? JSON.stringify(v.plan, null, 1) : ""); setPreview(null); };
  const save = () => act(async () => {
    let plan: unknown;
    try { plan = JSON.parse(text); } catch { note("board", "Plan geçerli bir JSON değil.", "err"); return; }
    show(await api.put<StagingView>(`/scenes/${sceneId}/staging`, { plan }));
    note("board", `${view.scene_code} sahnelemesi kaydedildi; animatiği yeniden birleştirince görünür.`, "ok");
    onSaved();
  }, "board");
  const reset = () => act(async () => {
    show(await api.del<StagingView>(`/scenes/${sceneId}/staging`));
    note("board", `${view.scene_code} sahnelemesi kurallara döndü.`, "ok");
    onSaved();
  }, "board");
  const write = () => act(async () => {
    setWorking("write");
    try {
      const v = await api.post<StagingWritten>(`/scenes/${sceneId}/staging/write`);
      show(v);
      note("board", `${view.scene_code} sahnelemesini Claude yazdı (${usdTr(v.usd)}). Önizleyip beğenmezsen düzenleyebilir ya da kurallara dönebilirsin.`, "ok");
      onSaved();
    } finally { setWorking(""); }
  }, "board");
  const toggleGuide = (on: boolean) => act(async () => {
    show(await api.put<StagingView>(`/scenes/${sceneId}/staging/guide`, { on }));
    note("board", on ? `${view.scene_code}: kukla rehberi video isteğine eklendi; plan ve bedel yenilendi (onay yeniden istenir).`
      : `${view.scene_code}: kukla rehberi video isteğinden çıkarıldı.`, "ok");
    onSaved();
  }, "board");
  const render = (dummy: boolean) => act(async () => {
    setWorking(dummy ? "guide" : "preview");
    try { setPreview(await api.post<StagingPreview>(`/scenes/${sceneId}/staging/preview?dummy=${dummy}`)); }
    finally { setWorking(""); }
  }, "board");
  return (
    <div className="staging" data-tour="board-staging">
      <div className="row"><b className="mono">{view.scene_code}</b>
        <span className="faint">{view.source ? SOURCE_TR[view.source] ?? view.source : "görsel yok"} · {secTr(view.duration_s)}</span></div>
      {view.note ? <p className="faint small">{view.note}</p> : <>
        {preview && <video key={preview.url} src={preview.url} controls autoPlay muted playsInline />}
        <div className="row wrap">
          <button className="btn" type="button" disabled={busy || Boolean(working)} onClick={() => void render(false)}>{working === "preview" ? "Çiziliyor…" : "Önizle"}</button>
          <button className="btn" type="button" disabled={busy || Boolean(working)} onClick={() => void render(true)}
            title="Görünüş taşımayan, adı yazılı siluetler: video modeline hareket ve kamera rehberi">{working === "guide" ? "Çiziliyor…" : "Kukla rehberi"}</button>
          {preview && <a className="btn ghost" href={preview.url} download>İndir</a>}
          <button className="btn" type="button" disabled={busy || Boolean(working) || meta?.dry_run !== false} onClick={() => void write()}
            title={meta?.dry_run !== false ? "Ücretsiz modda Claude çağrılmaz." : "Claude kareye bakıp planı yazar: grubu karakterlere böler, hareketleri anlatıma oturtur, kamerayı gerekçelendirir. Ücretli (çekim başına yaklaşık 0,05 $), önce izin sorulur."}>
            {working === "write" ? "Claude yazıyor…" : "Claude ile yaz"}</button>
        </div>
        {typeof view.plan?.note === "string" && view.plan.note && <p className="small staging-note">{view.plan.note}</p>}
        {view.guide && <GuideToggle guide={view.guide} busy={busy} onChange={(on) => void toggleGuide(on)} />}
        <label className="faint small" htmlFor={`staging-${sceneId}`}>Plan (JSON): konum x/y, boy h (kare oranı), grubu bölmek için crop, hareketler, kamera</label>
        <textarea id={`staging-${sceneId}`} className="mono staging-plan" spellCheck={false} value={text} onChange={(e) => setText(e.target.value)} rows={12} />
        <p className="faint small">Hareketler: {view.actions.join(", ")} · görseller: {view.images.join(", ") || "yok"}</p>
        <div className="row wrap">
          <button className="btn primary" type="button" disabled={busy} onClick={() => void save()}>Planı kaydet</button>
          {(view.source === "director" || view.source === "claude") && <button className="btn ghost" type="button" disabled={busy} onClick={() => void reset()}>Kurallara dön</button>}
        </div>
      </>}
    </div>
  );
}

/** Kukla rehberini video isteğine ekleme anahtarı: ne işe yaradığı ve ek bedeli (referans videosu token'ı) yazılı. */
function GuideToggle({ guide, busy, onChange }: { guide: StagingGuide; busy: boolean; onChange: (on: boolean) => void }) {
  const cost = guide.extra_usd != null
    ? `${guide.engine_label}${guide.resolution ? ` ${guide.resolution}` : ""}: istek başına yaklaşık +${usdTr(guide.extra_usd)}`
    : `${guide.engine_label} rehber almaz (yalnızca H3 Max ve Seedance 2.5)`;
  return (
    <label className="staging-guide">
      <input type="checkbox" checked={guide.on} disabled={busy} onChange={(e) => onChange(e.target.checked)} />
      <span>
        <b>Kukla rehberini video modeline ver</b>
        <span className="faint small">Model hareketi, zamanlamayı ve kamerayı rehberden ({secTr(guide.seconds)}, çekimin video süresi) alır; görünüşü ilk kare ve referanslardan. {cost}.</span>
      </span>
    </label>
  );
}

/** Animatik: oynatıcı, kesme şeridi ve sekans süreleri. Birleştirme kuyruğa verilir; film adresi değişince biter. */
function Animatic() {
  const { ov, projectId, busy, act, note } = usePanel();
  const video = useRef<HTMLVideoElement | null>(null);
  const [now, setNow] = useState(0);
  const [waiting, setWaiting] = useState<{ before: string | null; at: number } | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
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
    setStale(false);
    note("board", "Animatik birleştirmeye alındı: seslendirilmemiş replikler önce geçici sesle üretilir, görseli olan çekimler sahnelenir.", "ok");
  }, "board");
  return (
    <div className="animatic">
      <div className="stack">
        {film ? <video key={film.url} ref={video} src={film.url} controls preload="metadata" playsInline onTimeUpdate={(e) => setNow(e.currentTarget.currentTime)} />
          : <p className="empty">Henüz animatik yok. Görseli olan çekimler sahnelenir, olmayanlar kart olarak sesle dizilir; birleştirmek ücretsizdir.</p>}
        {clips.length > 0 && (
          <div className="cutbar" aria-label="Animatik çekimleri">
            {clips.map((c) => (
              <button key={c.scene_code} type="button" className={`cut${now >= c.in_s && now < c.out_s ? " active" : ""}${c.scene_id && c.scene_id === sel ? " sel" : ""}`}
                style={{ flex: `${Math.max(0.1, c.out_s - c.in_s)} 1 0`, background: (STILL[c.still ?? "card"] ?? STILL.card).color }}
                aria-pressed={Boolean(c.scene_id && c.scene_id === sel)}
                title={`${c.scene_code} · ${secTr(c.out_s - c.in_s)} · ${(STILL[c.still ?? "card"] ?? STILL.card).tr} · ${c.timing === "voice" ? "süre sesten" : "çekim süresi"}${cut?.errors?.[c.scene_code] ? ` · çizilemedi: ${cut.errors[c.scene_code]}` : ""}`}
                onClick={() => { if (c.scene_id) setSel(c.scene_id); const v = video.current; if (v) { v.currentTime = c.in_s + 0.05; v.play().catch(() => {}); } }}>{c.scene_code}</button>
            ))}
          </div>
        )}
      </div>
      <div className="stack anim-seq">
        <button className="btn primary" type="button" disabled={busy || pending} onClick={() => void build()}>{pending ? "Birleştiriliyor…" : film ? "Animatiği yeniden birleştir" : "Animatiği birleştir"}</button>
        {stale && <p className="warn small">Sahneleme değişti: görmek için animatiği yeniden birleştir.</p>}
        <div className="row"><b>Toplam</b><span className="mono">{timecode(total)} · {secTr(total)}</span></div>
        {(cut?.sequences ?? []).map((q) => (
          <div key={q.code} className="row"><span>{q.code} · {q.title}</span><span className="mono">{secTr(q.out_s - q.in_s)}</span></div>
        ))}
        {cut && <p className="faint small">{cut.motion ?? 0} sahnelenmiş çekim, {cut.boards ?? 0} onaylı kare, {clips.length - (cut.boards ?? 0) - (cut.motion ?? 0)} çekim kartı · {cut.voiced ?? 0} çekimin süresi sesten.</p>}
        <p className="faint small">Süre: çekimin bütün replikleri seslendirildiyse giriş payı + replikler + çıkış payı, değilse çekim süresi. Ses ve kareler değişince yeniden birleştir.</p>
        {sel ? <Staging sceneId={sel} onSaved={() => setStale(true)} />
          : clips.some((c) => c.scene_id) && <p className="faint small">Sahnelemesini görmek ve düzenlemek için şeritten bir çekim seç.</p>}
      </div>
    </div>
  );
}

export function BoardPage() {
  const { ov, projectId, busy, frozen, act, note, meta, admin, safeAreas, setSafeAreas } = usePanel();
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
  const draftPass = () => act(async () => {
    const r = await api.post<DispatchAll>(`/projects/${projectId}/draft-pass`);
    note("board", passText(r, "Taslak geçiş") + " Son çekim bitince kaba kurgu kendiliğinden birleşir.", "ok");
  }, "board");
  const upgradePass = () => act(async () => {
    const r = await api.post<DispatchAll>(`/projects/${projectId}/upgrade-pass`);
    note("board", passText(r, "Tam tura alınan"), "ok");
  }, "board");
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
          {!refMode && <button className="btn primary" type="button" disabled={busy || !scenes.some((x) => x.board_url)}
            title="Şeritteki bütün kareleri (ilk ve son kareler) onaylar, sonra videoları başlatır. İnceleme bekleyen kare onaylanmaz."
            onClick={() => act(async () => {
              const r = await api.post<StripApproval>(`/projects/${projectId}/boards/approve`);
              const t = stripText(r);
              note("board", t.text, t.bad ? "err" : "ok");
            }, "board")}>Şeridi onayla ve videoları başlat</button>}
          {refMode && flagged > 0 && <button className="btn primary" type="button" disabled={busy || frozen} onClick={upgradePass}>İyileştirme geçişi ({flagged} çekim)</button>}
          <ToolMenu title="Geçişler, PDF, güvenli alanlar ve kare onayı ayarı" tour="board-more">{(close) => <>
            <button type="button" role="menuitem" disabled={busy || frozen} onClick={() => { close(); void draftPass(); }}>
              <span className="mi-ck" /><span className="mi-t">Taslak geçişi başlat</span><span className="mi-s">test turu</span>
            </button>
            {!(refMode && flagged > 0) && <button type="button" role="menuitem" disabled={busy || frozen || !flagged} onClick={() => { close(); void upgradePass(); }}>
              <span className="mi-ck" /><span className="mi-t">{flagged ? `İyileştirme geçişi (${flagged} çekim)` : "İyileştirme geçişi"}</span>{!flagged && <span className="mi-s">işaretli çekim yok</span>}
            </button>}
            <div className="menu-sep" />
            <a role="menuitem" href={`/api/projects/${projectId}/storyboard.pdf`} download="storyboard.pdf" onClick={close}>
              <span className="mi-ck"><Icon.download /></span><span className="mi-t">PDF indir</span>
            </a>
            <button type="button" role="menuitemcheckbox" aria-checked={safeAreas} onClick={() => setSafeAreas(!safeAreas)}
              title="Güvenli alanlar: action safe %93, title safe %90 (G)">
              <span className="mi-ck">{safeAreas && <Icon.check />}</span><span className="mi-t">Güvenli alanlar</span><span className="mi-s">G</span>
            </button>
            {!refMode && <button type="button" role="menuitemcheckbox" aria-checked={required} disabled={busy} onClick={() => {
              const on = !required;
              void act(async () => {
                await api.patch(`/projects/${projectId}`, { settings: { require_board_approval: on } });
                note("board", on ? "Onaylanmamış karesi olan çekimde görüntü üretilmez." : "Kare onayı artık zorunlu değil.", "ok");
              }, "board");
            }}>
              <span className="mi-ck">{required && <Icon.check />}</span><span className="mi-t">Görüntü üretimi için kare onayı zorunlu</span>
            </button>}
          </>}</ToolMenu>
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
