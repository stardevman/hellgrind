"use client";
/* Rehber turu: hedef öğenin üstüne spot ışığı, yanına açıklama kartı. Ok tuşları ileri/geri, Escape kapatır; pencere
   boyutu değişince yeniden konumlanır. İlk açılışta kendiliğinden başlar, bitince tarayıcıda "görüldü" kalır. */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { readFlag, TOUR_DONE_KEY, usePanel, writeFlag } from "@/lib/panel";
import { tourStepsFor } from "@/lib/tour";

interface Box { top: number; left: number; width: number; height: number }
const PAD = 6;

function place(target: Box | null, card: { w: number; h: number }): { top: number; left: number } {
  const vw = window.innerWidth, vh = window.innerHeight, m = 12;
  if (!target) return { top: Math.max(m, (vh - card.h) / 2), left: Math.max(m, (vw - card.w) / 2) };
  const below = target.top + target.height + PAD + m;
  const above = target.top - PAD - m - card.h;
  const top = below + card.h <= vh ? below : above >= m ? above : Math.max(m, Math.min(vh - card.h - m, target.top));
  const left = Math.max(m, Math.min(vw - card.w - m, target.left + target.width / 2 - card.w / 2));
  return { top, left };
}

export function Tour() {
  const { tour, setTour, meta, projectId, setPage, ov } = usePanel();
  const hasProject = Boolean(projectId);
  const steps = tourStepsFor(hasProject);
  const [i, setI] = useState(0);
  const [box, setBox] = useState<Box | null>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const card = useRef<HTMLDivElement>(null);
  const step = steps[Math.min(i, steps.length - 1)];
  const hasShots = Boolean(ov?.scenes.length);

  // İlk açılış: veriler gelince ve tur daha önce bitirilmemişse başlat.
  useEffect(() => {
    if (meta && !readFlag(TOUR_DONE_KEY)) setTour(true);
  }, [meta, setTour]);
  useEffect(() => { if (tour) setI(0); }, [tour]);

  const end = useCallback(() => { writeFlag(TOUR_DONE_KEY, true); setTour(false); }, [setTour]);

  // Adımın sayfasına geç (çekimsiz projede çekim gereken sayfalar senaryoya düşer; hedef bulunamazsa kart ortada kalır).
  useEffect(() => {
    if (tour && step?.page) setPage(step.page);
  }, [tour, step?.page, setPage]);

  const measure = useCallback(() => {
    if (!tour || !step) return;
    const el = document.querySelector<HTMLElement>(`[data-tour="${step.target}"]`);
    const r = el?.getBoundingClientRect();
    const b = r && r.width > 0 && r.height > 0 ? { top: r.top, left: r.left, width: r.width, height: r.height } : null;
    setBox(b);
    const c = card.current;
    setPos(place(b, { w: c?.offsetWidth ?? 360, h: c?.offsetHeight ?? 160 }));
  }, [tour, step]);

  useLayoutEffect(() => {
    if (!tour) return;
    measure();
    const t = window.setTimeout(measure, 60);                 // sayfa değişiminden sonra yeniden ölç
    const t2 = window.setTimeout(measure, 300);
    window.addEventListener("resize", measure);
    return () => { window.clearTimeout(t); window.clearTimeout(t2); window.removeEventListener("resize", measure); };
  }, [tour, measure, hasShots]);

  useEffect(() => {
    if (!tour) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") end();
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
  return (
    <div className="tour" role="dialog" aria-modal="true" aria-labelledby="tour-title">
      <div className="tour-dim" onClick={end} />
      {box && <div className="tour-spot" style={{ top: box.top - PAD, left: box.left - PAD, width: box.width + PAD * 2, height: box.height + PAD * 2 }} />}
      <div className="tour-card" ref={card} style={pos ? { top: pos.top, left: pos.left } : { visibility: "hidden" }}>
        <div className="tour-h"><span className="mono tiny dim">{i + 1} / {steps.length}</span><span className="grow" /><button type="button" className="btn ghost" onClick={end}>Atla</button></div>
        <h2 id="tour-title">{step.title}</h2>
        <p>{step.text}</p>
        <div className="tour-f">
          <button type="button" className="btn" disabled={i === 0} onClick={() => setI(i - 1)}>Geri</button>
          <button type="button" className="btn primary" onClick={() => (last ? end() : setI(i + 1))}>{last ? "Bitir" : "İleri"}</button>
        </div>
      </div>
    </div>
  );
}
