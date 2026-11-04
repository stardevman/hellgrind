"use client";
/* Rehberler: hedef öğenin üstüne spot ışığı, yanına açıklama kartı. Ok tuşları ileri/geri (yazı alanındayken değil),
   Escape kapatır; pencere boyutu değişince yeniden konumlanır. Panel turu ilk açılışta kendiliğinden başlar, bitince
   tarayıcıda "görüldü" kalır. Kitaptan çizgi film rehberi etkileşimlidir: ekranı kapatmaz, uygulama kullanılırken açık
   kalır, kaldığı adımı hatırlar ve her adımın gerçek durumunu (yapıldı / bekliyor) gösterir. */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { readFlag, TOUR_DONE_KEY, usePanel, writeFlag } from "@/lib/panel";
import { tourStepsFor } from "@/lib/tour";

interface Box { top: number; left: number; width: number; height: number }
const PAD = 6;
const BOOK_STEP_KEY = "hg-tour-book-step";

function place(target: Box | null, card: { w: number; h: number }): { top: number; left: number } {
  const vw = window.innerWidth, vh = window.innerHeight, m = 12;
  if (!target) return { top: Math.max(m, (vh - card.h) / 2), left: Math.max(m, (vw - card.w) / 2) };
  const below = target.top + target.height + PAD + m;
  const above = target.top - PAD - m - card.h;
  let top = below + card.h <= vh ? below : above >= m ? above : NaN;
  let left = Math.max(m, Math.min(vw - card.w - m, target.left + target.width / 2 - card.w / 2));
  if (Number.isNaN(top)) {                                     // üstte de altta da yer yok: hedefin yanına
    top = Math.max(m, Math.min(vh - card.h - m, target.top));
    const right = target.left + target.width + PAD + m;
    left = right + card.w <= vw - m ? right : Math.max(m, target.left - PAD - m - card.w);
  }
  return { top, left };
}

const readStep = () => { try { return Number(localStorage.getItem(BOOK_STEP_KEY)) || 0; } catch { return 0; } };
const writeStep = (i: number) => { try { localStorage.setItem(BOOK_STEP_KEY, String(i)); } catch { /* depolama kapalı */ } };
const find = (name: string) => document.querySelector<HTMLElement>(`[data-tour="${name}"]`);
/** Görünür hedef: kapalı bir <details> bölümünün içindeki öğe yerleşimde kalır ama görünmez; o zaman yok sayılır. */
const visible = (name: string) => {
  const el = find(name);
  const shut = el?.closest("details:not([open])");
  return el && !(shut && !shut.querySelector(":scope > summary")?.contains(el)) ? el : null;
};

export function Tour() {
  const { tour, setTour, meta, projectId, setPage, ov, admin, scene, selectScene } = usePanel();
  const hasProject = Boolean(projectId);
  const interactive = tour === "book";
  const steps = tourStepsFor(hasProject, tour ?? "panel");
  const [i, setI] = useState(0);
  const [box, setBox] = useState<Box | null>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const card = useRef<HTMLDivElement>(null);
  const step = steps[Math.min(i, steps.length - 1)];
  const hasShots = Boolean(ov?.scenes.length);

  // İlk açılış: veriler gelince ve panel turu daha önce bitirilmemişse başlat.
  useEffect(() => {
    if (meta && !readFlag(TOUR_DONE_KEY)) setTour("panel");
  }, [meta, setTour]);
  useEffect(() => { if (tour) setI(tour === "book" ? readStep() : 0); }, [tour]);
  useEffect(() => { if (interactive) writeStep(i); }, [interactive, i]);

  const end = useCallback(() => {
    if (tour === "panel") writeFlag(TOUR_DONE_KEY, true);
    setTour(null);
  }, [tour, setTour]);
  const finish = useCallback(() => { if (interactive) writeStep(0); end(); }, [interactive, end]);

  // Adımın sayfasına geç, gerekiyorsa çekim seç ve sekmeyi aç (çekimsiz projede çekim gereken sayfalar senaryoya düşer;
  // hedef bulunamazsa yedek hedef, o da yoksa kart ortada kalır). Yalnızca adım değişince: kullanıcı sonra serbestçe gezer.
  useEffect(() => {
    if (!tour || !step) return;
    if (step.page) setPage(step.page);
    if (step.scene && !scene && ov?.scenes[0]) selectScene(ov.scenes[0].id);
    const names = [...(step.scene ? ["inspector-open"] : []), ...(step.click ? [step.click] : [])];
    // Denetçi önce açılır (kapalıysa), sekme sonra; hedef kapalı bir bölümdeyse (<details>) bölüm açılır. Geç gelen
    // sayfa için birkaç kez denenir.
    const prepare = () => {
      names.forEach((n) => find(n)?.click());
      for (let d = find(step.target)?.closest("details"); d; d = d.parentElement?.closest("details")) {
        if (!d.open) d.open = true;
      }
      visible(step.target)?.scrollIntoView({ block: "nearest" });   // kayan panelin dışında kalmasın
    };
    const timers = [80, 300, 600].map((ms) => window.setTimeout(prepare, ms));
    return () => timers.forEach((t) => window.clearTimeout(t));
  }, [tour, step]);

  const measure = useCallback(() => {
    if (!tour || !step) return;
    const el = visible(step.target) ?? (step.fallback ? visible(step.fallback) : null);
    const r = el?.getBoundingClientRect();
    const b = r && r.width > 0 && r.height > 0 ? { top: r.top, left: r.left, width: r.width, height: r.height } : null;
    setBox((old) => (old && b && old.top === b.top && old.left === b.left && old.width === b.width && old.height === b.height ? old : b));
    const c = card.current;
    setPos(place(b, { w: c?.offsetWidth ?? 380, h: c?.offsetHeight ?? 180 }));
  }, [tour, step]);

  useLayoutEffect(() => {
    if (!tour) return;
    measure();
    const timers = [60, 300, 700].map((ms) => window.setTimeout(measure, ms));   // sayfa ve sekme değişiminden sonra
    // Etkileşimli rehberde kullanıcı sayfayı değiştirir, kaydırır, paneller açılır: hedef düzenli yeniden ölçülür.
    const every = interactive ? window.setInterval(measure, 600) : 0;
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true);
    return () => {
      timers.forEach((t) => window.clearTimeout(t));
      if (every) window.clearInterval(every);
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure, true);
    };
  }, [tour, measure, hasShots, interactive]);

  useEffect(() => {
    if (!tour) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") end();
      else if ((e.target as HTMLElement).closest?.("input, select, textarea, [contenteditable]")) return;
      else if (e.key === "ArrowRight") setI((x) => (x < steps.length - 1 ? x + 1 : x));
      else if (e.key === "ArrowLeft") setI((x) => Math.max(0, x - 1));
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [tour, steps.length, end]);

  if (!tour || !step) return null;
  const last = i >= steps.length - 1;
  const state = step.check?.({ ov, meta, admin }) ?? null;
  return (
    <div className={`tour${interactive ? " live" : ""}`} role="dialog" aria-modal={!interactive} aria-labelledby="tour-title">
      {!interactive && <div className="tour-dim" onClick={end} />}
      {box && <div className="tour-spot" style={{ top: box.top - PAD, left: box.left - PAD, width: box.width + PAD * 2, height: box.height + PAD * 2 }} />}
      <div className="tour-card" ref={card} style={pos ? { top: pos.top, left: pos.left } : { visibility: "hidden" }}>
        <div className="tour-h">
          <span className="mono tiny dim">{i + 1} / {steps.length}</span>
          {interactive && <span className="tiny dim">Kitaptan çizgi film</span>}
          <span className="grow" />
          <button type="button" className="btn ghost" onClick={end}>{interactive ? "Kapat" : "Atla"}</button>
        </div>
        {interactive && <div className="tour-bar" aria-hidden><i style={{ width: `${((i + 1) / steps.length) * 100}%` }} /></div>}
        <h2 id="tour-title">{step.title}</h2>
        <p>{step.text}</p>
        {(state !== null || (step.admin && !admin)) && (
          <p className="tour-state">
            {state === true && <span className="tag ok">Yapıldı</span>}
            {state === false && <span className="tag warn">Bekliyor</span>}
            {step.admin && !admin && <span className="tag info">Yönetici modu gerekir</span>}
          </p>
        )}
        <div className="tour-f">
          <button type="button" className="btn" disabled={i === 0} onClick={() => setI(i - 1)}>Geri</button>
          <button type="button" className="btn primary" onClick={() => (last ? finish() : setI(i + 1))}>{last ? "Bitir" : "İleri"}</button>
        </div>
      </div>
    </div>
  );
}
