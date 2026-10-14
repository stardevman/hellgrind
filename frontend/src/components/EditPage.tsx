"use client";
/* Kurgu sayfası (kurgu yazılımı düzeni): solda çekim havuzu ve olaylar, ortada zaman kodlu izleyici ve çekimin
   adayları, sağda denetçi, altta görüntü/diyalog/müzik izleriyle zaman çizelgesi. İzleyici ve oynatma kafası
   birbirine bağlıdır: filmde oynarken kafa çekimler üzerinde ilerler, cetvele tıklamak filmi oraya sarar. */
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type DragEvent, type MouseEvent } from "react";
import { api } from "@/lib/api";
import { parseIds, playable, toggle } from "@/lib/compare";
import { promoteText } from "@/lib/mixing";
import { EMOTION_COLOR, ENGINE_TR, WORKING } from "@/lib/i18n";
import { resolutionBadge } from "@/lib/finishing";
import { pickMedia, posterOf } from "@/lib/media";
import { readFlag, usePanel, writeFlag } from "@/lib/panel";
import { clock, dispatchText, eventText, tileSpec, usd, usdTr } from "@/lib/text";
import { noteAt } from "@/lib/client";
import { FPS, filmToTimeline, layout, rulerStep, timecode, timelineToFilm, type Layout } from "@/lib/timeline";
import type { DispatchAll, PromoteResult, RenderPlan, Scene, Variation } from "@/lib/types";
import { Icon } from "./icons";
import { UpscaleConfirm } from "./Finishing";
import { Inspector } from "./Inspector";
import { StatusDot, shortcutTarget } from "./ui";

type Mode = "shot" | "film";
const INSP_KEY = "hg-inspector";     // denetçi açık mı (tarayıcıda hatırlanır; varsayılan kapalı)

/* ---------- oynatıcı durumu (izleyici ile zaman çizelgesi paylaşır) ---------- */

function usePlayer() {
  const ref = useRef<HTMLVideoElement | null>(null);
  const pending = useRef<number | null>(null);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [loop, setLoop] = useState(false);

  const bind = useCallback((el: HTMLVideoElement | null) => { ref.current = el; }, []);
  const seek = useCallback((t: number) => {
    const v = ref.current;
    if (v && v.readyState >= 1) { v.currentTime = Math.max(0, Math.min(t, (v.duration || t) - 0.001)); setTime(v.currentTime); }
    else pending.current = t;
  }, []);
  const play = useCallback(() => { ref.current?.play().catch(() => {}); }, []);
  const pause = useCallback(() => { ref.current?.pause(); }, []);
  const toggle = useCallback(() => { const v = ref.current; if (v) { if (v.paused) play(); else pause(); } }, [play, pause]);
  const step = useCallback((dt: number) => { const v = ref.current; if (v) { v.pause(); seek(v.currentTime + dt); } }, [seek]);

  useEffect(() => {                                       // oynarken zaman kodu kare hızında güncellenir
    if (!playing) return;
    let raf = 0;
    const tick = () => { if (ref.current) setTime(ref.current.currentTime); raf = requestAnimationFrame(tick); };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing]);

  const events = {
    onLoadedMetadata: (e: React.SyntheticEvent<HTMLVideoElement>) => {
      const v = e.currentTarget;
      setDuration(v.duration || 0);
      if (pending.current != null) { v.currentTime = Math.min(pending.current, (v.duration || 0) - 0.001); pending.current = null; }
      setTime(v.currentTime);
    },
    onPlay: () => setPlaying(true),
    onPause: () => setPlaying(false),
    onEnded: (e: React.SyntheticEvent<HTMLVideoElement>) => { if (loop) { e.currentTarget.currentTime = 0; void e.currentTarget.play(); } else setPlaying(false); },
    onTimeUpdate: (e: React.SyntheticEvent<HTMLVideoElement>) => { if (!playing) setTime(e.currentTarget.currentTime); },
    onSeeked: (e: React.SyntheticEvent<HTMLVideoElement>) => setTime(e.currentTarget.currentTime),
  };
  return { ref, bind, seek, play, pause, toggle, step, time, duration, playing, loop, setLoop, events, pending };
}
type Player = ReturnType<typeof usePlayer>;

/* ---------- çekim havuzu ---------- */

/** Projenin kuru çalıştırma toplamı (GET render-plan?dry_run=true): taslak motoruyla bütün çekimler ve final motoruna
    yükseltme. Yüklenecek dosyaların SHA-256'sını hesapladığı için yoklanmaz; "Hesapla" ile elle okunur. */
function PlanTotal({ projectId }: { projectId: string }) {
  const { busy, frozen } = usePanel();
  const [plan, setPlan] = useState<RenderPlan | null>(null);
  const [up, setUp] = useState(false);
  const [state, setState] = useState<"" | "busy" | string>("");
  const run = () => {
    setState("busy");
    api.get<RenderPlan>(`/projects/${projectId}/render-plan?dry_run=true`)
      .then((r) => { setPlan(r); setState(""); })
      .catch((e: Error) => setState(e.message));
  };
  const off = plan ? plan.scenes.filter((r) => r.disabled).length : 0;
  const requests = plan ? plan.requests ?? plan.scenes.filter((r) => r.cost).length : null;
  const over = plan?.remaining_usd != null && plan.total_usd > plan.remaining_usd;
  return (
    <div className="pad stack" style={{ borderBottom: "1px solid var(--line)", gap: 4 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span className="small">Tahmini toplam (taslak{requests != null ? `, ${requests} istek` : ""}) <b className="mono">{plan ? usdTr(plan.total_usd) : "—"}</b></span>
        <button className="btn ghost" type="button" disabled={state === "busy"} onClick={run}
          title={plan?.input_mode === "reference"
            ? "Her sahnenin fal.ai isteği gönderilmeden hesaplanır (kuru çalıştırma); bir parça (aynı sahnenin çekimleri) tek istektir"
            : "Her çekimin fal.ai isteği gönderilmeden hesaplanır (kuru çalıştırma)"}>{state === "busy" ? "Hesaplanıyor…" : plan ? "Yeniden hesapla" : "Hesapla"}</button>
      </div>
      {plan?.remaining_usd != null && (
        <span className={`small ${over ? "warn" : "dim"}`}>Kalan bütçe <b className="mono">{usdTr(plan.remaining_usd)}</b>
          {over && " · taslak tahmini kalan bütçeyi aşıyor"}</span>
      )}
      {plan && (
        <span className="small dim">Final'e yükseltme <b className="mono">{usdTr(plan.upgrade_total_usd)}</b>
          <span className="faint"> · {ENGINE_TR[plan.engines.draft] ?? plan.engines.draft} → {ENGINE_TR[plan.engines.final] ?? plan.engines.final}</span></span>
      )}
      {off > 0 && <span className="warn small">{off} çekim kapalı motorda (RunPod): denetçiden H3 Max'e geçin.</span>}
      <button className="btn" type="button" aria-expanded={up} disabled={busy || frozen}
        title={frozen ? "Üretim durduruldu, önce Üretime devam et" : "1080p altındaki bütün kazanan klipler; uç nokta, klip başına tahmin, toplam ve kalan bütçe gösterilir"}
        onClick={() => setUp(!up)}>Bütün filmi 1080p'ye upscale et (son montajdan önce)</button>
      {up && <UpscaleConfirm scope="project" id={projectId} onClose={() => setUp(false)} />}
      {state && state !== "busy" && <span className="bad small">{state}</span>}
    </div>
  );
}

/** Müşteri notları: sürüm, zaman kodu, çekim ve metin; çözüldü / çözülmedi. Nota tıklamak çekimi seçer. */
function ClientNotes() {
  const { ov, selectScene, busy, act, note } = usePanel();
  const notes = ov?.client?.notes ?? [];
  const resolve = (id: string, resolved: boolean) => act(async () => {
    await api.patch(`/client-notes/${id}`, { resolved });
    note("inspector", resolved ? "Müşteri notu çözüldü olarak işaretlendi." : "Müşteri notu yeniden açıldı.", "ok");
  }, "inspector");
  return (
    <ol className="cnotes">
      {notes.map((n) => (
        <li key={n.id} className={n.resolved ? "done" : ""}>
          <label className="check" title={n.resolved ? "Çözüldü" : "Çözülmedi"}>
            <input type="checkbox" checked={n.resolved} disabled={busy} onChange={(e) => void resolve(n.id, e.target.checked)} aria-label={`${n.text} çözüldü`} />
          </label>
          <button type="button" className="cn-body" disabled={!n.scene_id} onClick={() => n.scene_id && selectScene(n.scene_id)}>
            <span className="mono small faint">{n.label ?? n.gate_tr}{n.at_s != null ? ` · ${timecode(n.at_s)}` : ""}{n.scene_code ? ` · ${n.scene_code}` : ""}</span>
            <span>{n.text}</span>
          </button>
        </li>
      ))}
    </ol>
  );
}

function ShotBin({ onPick }: { onPick: () => void }) {
  const { ov, scene, selectScene, projectId, busy, frozen, act, note } = usePanel();
  const [tab, setTab] = useState<"shots" | "takes" | "events" | "notes">("takes");
  if (!ov) return null;
  const codes = Object.fromEntries(ov.scenes.map((s) => [s.id, s.scene_code]));
  const groups = ov.sequences.map((q) => ({ q, scenes: ov.scenes.filter((s) => s.sequence_id === q.id) }))
    .concat([{ q: null as never, scenes: ov.scenes.filter((s) => !ov.sequences.some((q) => q.id === s.sequence_id)) }])
    .filter((g) => g.scenes.length);
  const blocked = ov.scenes.filter((s) => s.status === "blocked").length;
  // Klibi hazır ama "Sırada"da kalmış çekimler (ör. kare hazırlama ya da donma sonrası): görüntü üretmeden tamamlanır.
  const stuck = ov.scenes.filter((s) => ["pending", "parsed", "storyboard", "frozen"].includes(s.status) && s.final_url).length;
  const finish = () => act(async () => {
    const r = await api.post<DispatchAll>(`/projects/${projectId}/finish-pending`);
    const x = dispatchText(r);
    note("inspector", x.text || "Bekleyen çekimler tamamlandı.", x.bad ? "err" : "ok");
  }, "inspector");
  const retry = () => act(async () => {
    const r = await api.post<DispatchAll>(`/projects/${projectId}/render-all`);
    const x = dispatchText(r);
    note("inspector", x.text, x.bad ? "err" : "ok");
  }, "inspector");
  return (
    <section className="pnl bin" data-tour="edit-bin">
      <div className="pnl-tabs" role="tablist" aria-label="Havuz">
        <button type="button" role="tab" aria-selected={tab === "takes"} onClick={() => setTab("takes")}>Adaylar{scene ? <span className="faint mono"> {scene.variations.filter(playable).length}</span> : null}</button>
        <button type="button" role="tab" aria-selected={tab === "shots"} onClick={() => setTab("shots")}>Çekimler <span className="faint mono">{ov.scenes.length}</span></button>
        <button type="button" role="tab" aria-selected={tab === "events"} onClick={() => setTab("events")}>Olay akışı</button>
        {(ov.client?.notes.length ?? 0) > 0 && (
          <button type="button" role="tab" aria-selected={tab === "notes"} onClick={() => setTab("notes")} data-tour="client-notes">
            Notlar <span className={`mono ${ov.client!.notes.some((x) => !x.resolved) ? "warn" : "faint"}`}>{ov.client!.notes.filter((x) => !x.resolved).length}</span>
          </button>
        )}
      </div>
      <div className={`pnl-b${tab === "takes" ? " takes" : ""}`}>
        {tab === "notes" ? <ClientNotes /> : tab === "takes" ? <Takes onPick={onPick} /> : tab === "events" ? (
          <ol className="ev">
            {ov.events.length ? ov.events.map((e, i) => {
              const x = eventText(e, codes);
              return <li key={`${e.ts}-${i}`}><time>{clock(e.ts)}</time><span className={x.c || ""}>{x.t}</span></li>;
            }) : <li><span /><span className="faint">Henüz olay yok.</span></li>}
          </ol>
        ) : (
          <>
            {projectId && <div className="adv"><PlanTotal key={projectId} projectId={projectId} /></div>}
            {stuck > 0 && (
              <div className="pad stack" style={{ borderBottom: "1px solid var(--line)", gap: 6 }}>
                <span className="small dim">{stuck} çekimin klibi hazır ama durumu "Sırada": görüntü üretilmeden tamamlanabilir.</span>
                <button className="btn primary" type="button" disabled={busy || frozen} onClick={finish}
                  title={frozen ? "Üretim durduruldu, önce Üretime devam et" : "Klip korunur; eksik ses, renk ya da montaj varsa yalnızca onlar çalışır"}>
                  Bekleyen {stuck} çekimi tamamla
                </button>
              </div>
            )}
            {blocked > 0 && (
              <div className="pad" style={{ borderBottom: "1px solid var(--line)" }}>
                <button className="btn" type="button" style={{ width: "100%" }} disabled={busy || frozen} onClick={retry}>Engellenen çekimleri yeniden dene ({blocked})</button>
              </div>
            )}
            {groups.map((g) => (
              <div key={g.q?.id ?? "none"}>
                <div className="bin-seq">
                  <b>{g.q ? `${g.q.code} · ${g.q.title}` : "Sekanssız"}</b>
                  {g.q?.status === "capped" && <span className="tag bad">Tavan doldu</span>}
                  <span className="meta mono">{g.q ? `${g.q.done}/${g.q.shots} · ${usd(g.q.spent_usd)}${g.q.budget_cap_usd != null ? " / " + usd(g.q.budget_cap_usd) : ""}` : `${g.scenes.length} çekim`}</span>
                </div>
                {g.scenes.map((sc) => {
                  const th = posterOf(sc);
                  return (
                    <button key={sc.id} type="button" className="bin-row" aria-current={scene?.id === sc.id} onClick={() => selectScene(sc.id)}
                      aria-label={`${sc.scene_code} çekimini seç`}>
                      {th ? <img className="th" src={th} alt="" loading="lazy" /> : <span className="th" />}
                      <span style={{ minWidth: 0 }}>
                        <span className="code">{sc.scene_code}</span>
                        <span className="desc" style={{ display: "block" }}>{sc.description}</span>
                      </span>
                      <span className="side">
                        <StatusDot status={sc.status} />
                        <span className="mono faint">{usd(sc.spent_usd)}</span>
                        {sc.cut_tier === "draft" && <span className="tag warn">Taslak</span>}
                        {sc.engine_disabled && <span className="tag bad" title="MiniMax H3 (RunPod) kapalı: denetçiden H3 Max'e geçin">Motor kapalı</span>}
                        {sc.upgrade_requested && <span className="tag info">İyileştirilecek</span>}
                        {sc.generation && <span className="tag warn" title={`${sc.generation.message} Farklı üretimden: ${sc.generation.mismatched.join(", ")}.`}>karma üretim</span>}
                      </span>
                    </button>
                  );
                })}
              </div>
            ))}
          </>
        )}
      </div>
    </section>
  );
}

/* ---------- izleyici ---------- */

function Viewer({ mode, setMode, player, src, poster, label, right }: {
  mode: Mode; setMode: (m: Mode) => void; player: Player; src: string | null; poster: string | null; label: string; right: string;
}) {
  const { ov, filmKind, setFilmKind, varId, setVar } = usePanel();
  const p = player;
  return (
    <section className="pnl viewer">
      <header className="pnl-h">
        <h2>İzleyici</h2>
        <div className="seg" role="group" aria-label="Kaynak">
          <button type="button" aria-pressed={mode === "shot"} onClick={() => setMode("shot")}>Çekim</button>
          <button type="button" aria-pressed={mode === "film" && filmKind === "final"} disabled={!ov?.timeline}
            onClick={() => { setFilmKind("final"); setMode("film"); }}>Film</button>
          <button type="button" aria-pressed={mode === "film" && filmKind === "draft"} disabled={!ov?.draft_timeline}
            onClick={() => { setFilmKind("draft"); setMode("film"); }}>Kaba kurgu</button>
        </div>
        <span className="grow" />
        {mode === "shot" && varId && <button className="btn ghost" type="button" onClick={() => setVar(null)}>Sonuca dön</button>}
      </header>
      <div className="screen">
        {src
          ? <video key={src} ref={p.bind} src={src} poster={poster ?? undefined} preload="metadata" playsInline {...p.events}
              onClick={p.toggle} />
          : <p className="none">{mode === "film" ? "Henüz film yok. Teslim sayfasından birleştirin." : "Henüz video yok."}</p>}
        <div className="osd"><span>{label}</span>{right && <span className="mono">{right}</span>}</div>
      </div>
      <div className="transport">
        <div className="btns">
          <button className="btn ghost icon-btn" type="button" aria-label="Başa sar" onClick={() => p.seek(0)} disabled={!src}><Icon.toStart /></button>
          <button className="btn ghost icon-btn" type="button" aria-label="Bir kare geri" onClick={() => p.step(-1 / FPS)} disabled={!src}><Icon.stepBack /></button>
          <button className="btn icon-btn" type="button" aria-label={p.playing ? "Duraklat" : "Oynat"} onClick={p.toggle} disabled={!src}>
            {p.playing ? <Icon.pause /> : <Icon.play />}
          </button>
          <button className="btn ghost icon-btn" type="button" aria-label="Bir kare ileri" onClick={() => p.step(1 / FPS)} disabled={!src}><Icon.stepFwd /></button>
          <button className="btn ghost icon-btn" type="button" aria-label="Döngü" aria-pressed={p.loop} onClick={() => p.setLoop(!p.loop)}><Icon.loop /></button>
        </div>
        <span className="tc" aria-label="Zaman kodu">{timecode(p.time)}<small>/ {timecode(p.duration)}</small></span>
        <input className="scrub" type="range" min={0} max={p.duration || 0} step={1 / FPS} value={Math.min(p.time, p.duration || 0)}
          aria-label="Konum" disabled={!src} onChange={(e) => { p.pause(); p.seek(Number(e.target.value)); }} />
        <span className="keys faint tiny"><kbd>Boşluk</kbd> <kbd>J</kbd><kbd>K</kbd><kbd>L</kbd> <kbd>←</kbd><kbd>→</kbd> <kbd>↑</kbd><kbd>↓</kbd></span>
      </div>
    </section>
  );
}

/* ---------- çekimin adayları ---------- */

/** Aday filme konabilir mi: havuzdaysa doğrudan, kapıdan geçememişse yönetmen kararıyla (override). */
const usable = (v: Variation) => playable(v) && v.rank !== 1 && Boolean(v.rank || v.overridable);

const Tile = memo(function Tile({ v, prev, selected, picked, onPick, sceneId, onHover }: {
  v: Variation; prev?: Variation; selected: boolean; picked: boolean; onPick: (v: Variation, e: MouseEvent) => void; sceneId: string;
  onHover: (v: Variation | null, el: HTMLElement | null) => void;
}) {
  const t = tileSpec(v, prev);
  const badge = resolutionBadge(v);
  const cls = ["tile", t.state === "passed" ? "" : t.state, selected ? "selected" : "", picked ? "compare-pick" : ""].filter(Boolean).join(" ");
  const label = t.label + (badge ? ` · ${badge.long}` : "") + (picked ? " · karşılaştırmada" : "") + (usable(v) ? " · zaman çizelgesine sürükleyerek filme koy" : "");
  const drag = (e: DragEvent<HTMLButtonElement>) => {
    e.dataTransfer.setData("text/x-hg-variation", JSON.stringify({ scene: sceneId, variation: v.id }));
    e.dataTransfer.effectAllowed = "move";
  };
  return (
    <>
      {t.tierStart && <span className="gap" aria-hidden="true" title="Tam tur" />}
      <button type="button" className={cls} aria-pressed={selected} aria-label={label} onClick={(e) => onPick(v, e)}
        draggable={usable(v)} onDragStart={usable(v) ? drag : undefined}
        onMouseEnter={(e) => onHover(v, e.currentTarget)} onMouseLeave={() => onHover(null, null)}
        onFocus={(e) => onHover(v, e.currentTarget)} onBlur={() => onHover(null, null)}>
        {v.thumb_url && <img src={v.thumb_url} alt="" loading="lazy" decoding="async" />}
        {v.score != null && <span className="sc adv">{v.score.toFixed(3)}</span>}
        {v.revision ? <span className="rev">R{v.revision}</span> : null}
        {badge ? <span className="res" aria-hidden="true">{badge.short}</span> : null}
        {picked ? <span className="cmp">K</span> : null}
        {v.rank ? <span className="rank">{v.rank === 1 ? "filmde" : v.rank}</span> : null}
      </button>
    </>
  );
});

/** Üzerine gelinen adayın büyük önizlemesi ve ayrıntısı: sütunun dışında, izleyicinin üstünde açılır (kırpılmaz); video varsa
    sessiz ve döngülü oynar, yoksa küçük resim. */
function TakePop({ v, prev, top, left }: { v: Variation; prev?: Variation; top: number; left: number }) {
  const t = tileSpec(v, prev);
  const badge = resolutionBadge(v);
  return (
    <div className="take-pop" style={{ top, left }} role="tooltip">
      {v.video_url ? <video src={v.video_url} poster={v.thumb_url ?? undefined} muted autoPlay loop playsInline preload="auto" />
        : v.thumb_url ? <img src={v.thumb_url} alt="" /> : <span className="ph" />}
      <div className="tp-body">
        <b>{v.rank === 1 ? "Filmde bu klip oynuyor" : t.label}</b>
        {!t.label.includes(t.fate) && <span className="dim">{t.fate}</span>}
        <span className="mono tiny dim">{v.score != null ? `skor ${v.score.toFixed(3)}` : "skor yok"}{badge ? ` · ${badge.long}` : v.resolution ? ` · ${v.resolution}` : ""}{v.seed != null ? ` · seed ${v.seed}` : ""}</span>
        {v.anomalies?.length ? <span className="warn tiny">{v.anomalies.join(", ")}</span> : null}
      </div>
    </div>
  );
}

/** Adayı filme koyar: havuzdaysa promote, kapıyı geçememişse yönetmen kararıyla (override). Yalnızca ses ve montaj yeniden çalışır. */
function usePromote(sc: Scene) {
  const { act, note, setVar } = usePanel();
  return useCallback((v: Variation) => act(async () => {
    const r = await api.post<PromoteResult>(`/scenes/${sc.id}/promote/${v.id}${v.rank ? "" : "?override=true"}`);
    setVar(null);
    const x = promoteText(r, v.rank ? "" : "Kapının kararı geçersiz kılındı. ");
    note("inspector", x.text, x.kind);
  }, "inspector"), [act, note, setVar, sc.id]);
}

function Takes({ onPick }: { onPick: () => void }) {
  const { scene: sc, varId, setVar, compareRaw, setCompare, openCompare, admin, busy } = usePanel();
  const [showAll, setShowAll] = useState(false);
  const [hover, setHover] = useState<{ v: Variation; top: number; left: number } | null>(null);
  const onHover = useCallback((v: Variation | null, el: HTMLElement | null) => {
    if (!v || !el) { setHover(null); return; }
    const r = el.getBoundingClientRect();
    setHover({ v, top: Math.max(8, Math.min(window.innerHeight - 220, r.top - 20)), left: r.right + 8 });
  }, []);
  const promote = usePromote(sc ?? ({ id: "" } as Scene));
  if (!sc) return <p className="empty">Çekim seçin.</p>;
  const picked = parseIds(compareRaw, sc);
  const pick = (v: Variation, e: MouseEvent) => {
    if (e.shiftKey && playable(v)) { setCompare(toggle(picked, v.id)); return; }
    setVar(varId === v.id ? null : v.id);
    onPick();
  };
  const probe = sc.variations.filter((v) => v.tier === "probe").length;
  const winner = sc.variations.find((v) => v.rank === 1);
  const chosen = varId ? sc.variations.find((v) => v.id === varId) : undefined;
  const hidden = admin || showAll ? [] : sc.variations.filter((v) => !playable(v) && !["queued", "running", "generated"].includes(v.status));
  const shown = hidden.length ? sc.variations.filter((v) => !hidden.includes(v)) : sc.variations;
  return (
    <>
      <div className="takes-bar">
        <span className="faint tiny mono">{sc.scene_code} · {winner ? `${sc.variations.filter(playable).length} izlenebilir` : `${probe} test + ${sc.variations.length - probe} tam`}</span>
        {chosen ? (
          chosen.rank === 1 ? <span className="tag ok">Filmde bu oynuyor</span>
            : usable(chosen) ? <button className="btn primary" type="button" disabled={busy} onClick={() => promote(chosen)}
              title="Bu adayı çekimin filmdeki klibi yapar; görüntü motoru çağrılmaz, yalnızca ses ve montaj yeniden çalışır">Filmde kullan</button>
              : <span className="faint tiny">{playable(chosen) ? "Filme konamaz: kapıdan geçmedi, karar değiştirilemez." : "İzlenebilir videosu yok."}</span>
        ) : <span className="faint tiny">Aynı çekimin klipleri. Tıkla: izle. Beğenirsen <b>Filmde kullan</b> ya da zaman çizelgesine sürükle.</span>}
      </div>
      <div className="strip" aria-label={`${sc.scene_code} adayları`}>
        {shown.length
          ? shown.map((v, i) => <Tile key={v.id} v={v} prev={shown[i - 1]} selected={varId === v.id} picked={picked.includes(v.id)} onPick={pick} sceneId={sc.id} onHover={onHover} />)
          : <span className="faint">Henüz klip yok</span>}
      </div>
      {hover && <TakePop v={hover.v} prev={sc.variations[sc.variations.indexOf(hover.v) - 1]} top={hover.top} left={hover.left} />}
      <div className="takes-foot">
        {hidden.length > 0 && <button className="btn ghost" type="button" onClick={() => setShowAll(true)}>{hidden.length} elenen adayı göster</button>}
        {!admin && showAll && hidden.length === 0 && sc.variations.some((v) => !playable(v)) && <button className="btn ghost" type="button" onClick={() => setShowAll(false)}>Elenenleri gizle</button>}
        {picked.length >= 2 && <button className="btn" type="button" onClick={() => openCompare(sc.id, picked)}>Seçilen {picked.length} klibi karşılaştır</button>}
        <span className="legend adv" aria-label="Aday göstergeleri">
          <span><i className="refined" />havuzda</span><span><i className="rejected" />elendi</span>
          <span><i className="retired" />havuz dışı</span><span><i className="working" />üretiliyor</span>
          <span><kbd>Shift</kbd>+tık: karşılaştır</span>
        </span>
      </div>
    </>
  );
}

/* ---------- zaman çizelgesi ---------- */

function Timeline({ lay, pos, onRuler, onClip, filmLabel }: {
  lay: Layout; pos: number | null; onRuler: (sec: number) => void; onClip: (sc: Scene) => void; filmLabel: string;
}) {
  const { ov, scene, projectId, busy, act, note, setAssembling, setVar } = usePanel();
  const scroller = useRef<HTMLDivElement>(null);
  const [dropOn, setDropOn] = useState<string | null>(null);
  const readDrag = (e: DragEvent) => { try { return JSON.parse(e.dataTransfer.getData("text/x-hg-variation") || "null") as { scene: string; variation: string } | null; } catch { return null; } };
  const drop = (sc: Scene, e: DragEvent) => {
    e.preventDefault(); setDropOn(null);
    const d = readDrag(e);
    if (!d) return;
    if (d.scene !== sc.id) { note("inspector", `Aday yalnızca kendi çekiminin (${ov?.scenes.find((x) => x.id === d.scene)?.scene_code ?? "?"}) üstüne bırakılabilir.`, "err"); return; }
    const v = sc.variations.find((x) => x.id === d.variation);
    if (!v) return;
    void act(async () => {
      const r = await api.post<PromoteResult>(`/scenes/${sc.id}/promote/${v.id}${v.rank ? "" : "?override=true"}`);
      setVar(null);
      const x = promoteText(r, v.rank ? "" : "Kapının kararı geçersiz kılındı. ");
      note("inspector", x.text, x.kind);
    }, "inspector");
  };
  const [width, setWidth] = useState(800);
  const [zoom, setZoom] = useState(1);
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    setWidth(el.clientWidth);
    return () => ro.disconnect();
  }, []);
  const pps = Math.max(4, ((width - 24) / Math.max(1, lay.duration)) * zoom);        // saniye başına piksel
  const total = Math.max(width, lay.duration * pps + 24);
  const step = rulerStep(pps);
  const ticks = useMemo(() => {
    const out: number[] = [];
    for (let t = 0; t <= lay.duration + 0.001; t += step) out.push(Number(t.toFixed(3)));
    return out;
  }, [lay.duration, step]);
  const x = (t: number) => t * pps;
  useEffect(() => {                                       // oynatma kafası görünür alanda kalsın
    const el = scroller.current;
    if (!el || pos == null) return;
    const px = x(pos);
    if (px < el.scrollLeft || px > el.scrollLeft + el.clientWidth - 40) el.scrollLeft = Math.max(0, px - 60);
  }, [pos]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!ov) return null;
  const jumps = new Set((ov.project.continuity?.pairs ?? []).filter((p) => p.jump).map((p) => p.to));
  const done = ov.scenes.filter((s) => s.status === "done").length;
  const assemble = () => act(async () => {
    await api.post(`/projects/${projectId}/assemble`);
    setAssembling({ before: ov.timeline?.url ?? null, at: Date.now() });
    note("inspector", "Film birleştirmeye alındı.", "ok");
  }, "inspector");
  // Cetvel: tıkla ya da basılı tutup sürükle (kafa izler); metin seçimi oluşmaz.
  const rulerAt = (el: HTMLElement, clientX: number) => {
    const r = el.getBoundingClientRect();
    onRuler(Math.max(0, Math.min(lay.duration, (clientX - r.left) / pps)));
  };
  const rulerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    rulerAt(e.currentTarget, e.clientX);
  };
  const rulerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.buttons & 1) rulerAt(e.currentTarget, e.clientX);
  };
  const hasFx = lay.effects.length > 0;
  const marks = (ov.client?.notes ?? []).map((n) => ({ n, t: noteAt(n, lay.shots) })).filter((m): m is { n: typeof m.n; t: number } => m.t != null);
  return (
    <section className="pnl tl" aria-label="Zaman çizelgesi">
      <header className="pnl-h">
        <h2>Zaman çizelgesi</h2>
        <span className="meta" title="Her çekim filmde kullanılan klibiyle, süresi kadar yer tutar. Bir adayı çekimin üstüne sürükleyerek filmdeki klibi değiştirebilirsin.">{ov.sequences.length} sekans · {ov.scenes.length} çekim · {filmLabel}</span>
        <span className="grow" />
        <div className="tl-tools">
          <span className="tc" title="Oynatma kafası">{timecode(pos ?? 0)}</span>
          <label className="check" title="Yakınlaştır">Ölçek
            <input type="range" min={1} max={8} step={0.25} value={zoom} onChange={(e) => setZoom(Number(e.target.value))} aria-label="Zaman çizelgesi ölçeği" />
          </label>
          <button className="btn ghost" type="button" onClick={() => setZoom(1)}>Sığdır</button>
          <button className="btn" type="button" disabled={!done || busy} onClick={assemble}>Tamamlanan {done} sahneyi birleştir</button>
        </div>
      </header>
      <div className="tl-grid">
        <div className="tl-heads" aria-hidden="true">
          <div className="h-ruler" />
          <div className="h-seq"><b>SQ</b>Sekans</div>
          <div className="h-vid" title="Her çekimin filmde kullanılan klibi"><b>V1</b>Görüntü</div>
          <div className="h-aud"><b>A1</b>Diyalog</div>
          <div className="h-aud"><b>A2</b>Müzik</div>
          {hasFx && <div className="h-aud"><b>A3</b>Efekt</div>}
          {marks.length > 0 && <div className="h-aud" title="Müşteri notları: ilgili çekimin üstünde"><b>MN</b>Müşteri</div>}
        </div>
        <div className="tl-scroll" ref={scroller}>
          <div className="tl-canvas" style={{ width: total }}>
            <div className="ruler" onPointerDown={rulerDown} onPointerMove={rulerMove} title="Tıkla ya da sürükle: oynatma kafasını taşı">
              {ticks.map((t) => <span key={t} style={{ left: x(t) }}>{timecode(t).slice(3, 8)}</span>)}
            </div>
            <div className="trk seq">
              {lay.bands.map((b) => <span key={b.id} className="band" style={{ left: x(b.start), width: Math.max(2, x(b.end - b.start) - 2) }} title={`${b.code} · ${b.title}`}>{b.code} · {b.title}</span>)}
            </div>
            <div className="trk vid">
              {lay.shots.map(({ scene: sc, start, end }) => {
                const th = posterOf(sc);
                const cls = ["clip", "vid", sc.status === "blocked" ? "blocked" : "", WORKING.has(sc.status) ? "work" : "", jumps.has(sc.scene_code) ? "jump" : "", sc.generation ? "mixed" : "", dropOn === sc.id ? "drop" : ""].filter(Boolean).join(" ");
                const extra = [sc.generation ? "karma üretim" : "", sc.split?.warning ?? ""].filter(Boolean).join(" · ");
                return (
                  <button key={sc.id} type="button" className={cls} aria-current={scene?.id === sc.id}
                    style={{ left: x(start), width: Math.max(4, x(end - start) - 2), ["--clip" as string]: EMOTION_COLOR[sc.emotion] || "#6b6f78" }}
                    title={`${sc.scene_code} · ${sc.description ?? ""}${extra ? ` · ${extra}` : ""}`} aria-label={`${sc.scene_code} çekimine git${sc.generation ? " (karma üretim)" : ""}`} onClick={() => onClip(sc)}
                    onDragOver={(e) => { if (e.dataTransfer.types.includes("text/x-hg-variation")) { e.preventDefault(); e.dataTransfer.dropEffect = "move"; if (dropOn !== sc.id) setDropOn(sc.id); } }}
                    onDragLeave={() => { if (dropOn === sc.id) setDropOn(null); }} onDrop={(e) => drop(sc, e)}>
                    {th && <img src={th} alt="" loading="lazy" />}
                    <span className="lbl"><b>{sc.scene_code}</b><span>{sc.description}</span></span>
                  </button>
                );
              })}
            </div>
            <div className="trk aud">
              {lay.dialogue.map((c) => <span key={c.id} className="clip dlg" style={{ left: x(c.start), width: Math.max(3, x(c.end - c.start) - 1) }} title={c.title}>{c.label}</span>)}
            </div>
            <div className="trk aud">
              {lay.music.map((c) => <span key={c.id} className="clip mus" style={{ left: x(c.start), width: Math.max(3, x(c.end - c.start) - 1) }} title={c.title}>{c.label}</span>)}
            </div>
            {hasFx && (
              <div className="trk aud">
                {lay.effects.map((c) => <span key={c.id} className="clip sfx" style={{ left: x(c.start), width: Math.max(3, x(c.end - c.start) - 1) }} title={c.title}>{c.label}</span>)}
              </div>
            )}
            {marks.length > 0 && (
              <div className="trk aud notes">
                {marks.map(({ n, t }) => (
                  <button key={n.id} type="button" className={`cmark${n.resolved ? " done" : ""}`} style={{ left: x(t) }}
                    title={`${n.label ?? ""} ${n.scene_code ?? ""} · ${n.text}${n.resolved ? " (çözüldü)" : ""}`} aria-label={`Müşteri notu: ${n.text}`}
                    onClick={() => onRuler(t)} />
                ))}
              </div>
            )}
            {pos != null && <div className="playhead" style={{ left: x(pos) }} />}
          </div>
        </div>
      </div>
    </section>
  );
}

/* ---------- sayfa ---------- */

export function EditPage() {
  const { ov, scene, selectScene, varId, filmKind, assembling, setAssembling } = usePanel();
  const [mode, setMode] = useState<Mode>("shot");
  const [inspOpen, setInspOpen] = useState(() => readFlag(INSP_KEY));
  const toggleInsp = (on: boolean) => { writeFlag(INSP_KEY, on); setInspOpen(on); };
  const player = usePlayer();
  const lay = useMemo(() => layout(ov?.scenes ?? [], ov?.sequences ?? []), [ov?.scenes, ov?.sequences]);
  const film = (filmKind === "draft" ? ov?.draft_timeline : ov?.timeline) ?? null;
  const cut = film?.cut?.clips ?? [];
  const filmMode = mode === "film" && Boolean(film);
  const media = scene ? pickMedia(scene, varId) : null;
  const shot = scene ? lay.shots.find((s) => s.scene.id === scene.id) : undefined;

  const waiting = Boolean(assembling && !(ov?.timeline && ov.timeline.url !== assembling.before) && Date.now() - assembling.at < 90000);
  useEffect(() => { if (assembling && !waiting) setAssembling(null); }, [assembling, waiting, setAssembling]);

  // Film oynarken oynatma kafasının üstündeki çekim denetçide seçili kalır.
  const filmPos = filmMode ? filmToTimeline(player.time, cut, lay.shots) : null;
  const filmShot = filmPos != null ? lay.shots.find((s) => filmPos >= s.start && filmPos < s.end) : undefined;
  useEffect(() => {
    if (filmShot && filmShot.scene.id !== scene?.id) selectScene(filmShot.scene.id);
  }, [filmShot?.scene.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const pos = filmMode ? filmPos : shot ? Math.min(shot.end, shot.start + player.time) : null;

  const goTo = useCallback((sec: number) => {
    const f = film ? timelineToFilm(sec, cut, lay.shots) : null;
    if (f != null) {
      if (mode !== "film") { player.pending.current = f; setMode("film"); }
      else player.seek(f);
      return;
    }
    const s = lay.shots.find((y) => sec >= y.start && sec < y.end) ?? lay.shots[lay.shots.length - 1];
    if (!s) return;
    setMode("shot");
    if (s.scene.id !== scene?.id) { player.pending.current = sec - s.start; selectScene(s.scene.id); }
    else player.seek(sec - s.start);
  }, [film, cut, lay.shots, mode, player, scene?.id, selectScene]);

  const onClip = useCallback((sc: Scene) => {
    const s = lay.shots.find((y) => y.scene.id === sc.id);
    if (filmMode && s && cut.some((c) => c.scene_code === sc.scene_code)) goTo(s.start);
    else { setMode("shot"); selectScene(sc.id); }
  }, [filmMode, lay.shots, cut, goTo, selectScene]);

  // Klavye: Boşluk/K oynat-duraklat, J/L bir saniye, ←/→ bir kare, ↑/↓ önceki/sonraki çekim, Home başa.
  const keys = useRef({ player, scene, lay, onClip });
  useEffect(() => { keys.current = { player, scene, lay, onClip }; });
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!shortcutTarget(e) || e.shiftKey) return;
      const { player: p, scene: sc, lay: l, onClip: clip } = keys.current;
      const onButton = (e.target as HTMLElement).closest("button, a, [role=tab]");
      if ((e.key === " " && !onButton) || e.key === "k" || e.key === "K") { e.preventDefault(); p.toggle(); }
      else if (e.key === "j" || e.key === "J") p.step(-1);
      else if (e.key === "l" || e.key === "L") p.step(1);
      else if (e.key === "ArrowLeft" && !onButton) { e.preventDefault(); p.step(-1 / FPS); }
      else if (e.key === "ArrowRight" && !onButton) { e.preventDefault(); p.step(1 / FPS); }
      else if (e.key === "Home") { e.preventDefault(); p.seek(0); }
      else if (e.key === "ArrowUp" || e.key === "ArrowDown") {
        const i = l.shots.findIndex((s) => s.scene.id === sc?.id);
        const next = l.shots[i + (e.key === "ArrowDown" ? 1 : -1)];
        if (next) { e.preventDefault(); clip(next.scene); }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (!ov) return null;
  const src = filmMode ? film!.url : media?.src ?? null;
  const label = filmMode ? (filmKind === "draft" ? "Kaba kurgu" : "Film") + (filmShot ? ` · ${filmShot.scene.scene_code}` : "")
    : scene ? `${scene.scene_code} · ${media?.label ?? "video yok"}` : "";
  const filmLabel = waiting ? "film birleştiriliyor…" : ov.timeline ? `film ${timecode(ov.timeline.cut?.clips?.at(-1)?.out_s ?? 0)}` : "film yok";
  return (
    <div className={`page edit${inspOpen ? "" : " insp-closed"}`}>
      <ShotBin onPick={() => setMode("shot")} />
      <Viewer mode={filmMode ? "film" : "shot"} setMode={setMode} player={player} src={src} poster={filmMode ? null : media?.poster ?? null}
        label={label} right={scene && !filmMode ? `${Number(scene.duration_s).toFixed(1)} sn · ${FPS} kare/sn` : ""} />
      {inspOpen ? <Inspector className="insp" onClose={() => toggleInsp(false)} />
        : <aside className="insp-rail" aria-label="Denetçi kapalı">
          <button type="button" className="btn ghost" onClick={() => toggleInsp(true)} title="Denetçiyi aç: seçili çekimin ayrıntıları, ses ve üretim ayarları">
            <Icon.chevron />Denetçi{scene?.status === "blocked" || scene?.status === "plan_review" ? <span className="tag warn">!</span> : null}
          </button>
        </aside>}
      <Timeline lay={lay} pos={pos} onRuler={goTo} onClip={onClip} filmLabel={filmLabel} />
    </div>
  );
}
