"use client";
/* 2D yayın güvenliği görünümleri: güvenli alan katmanı (action safe %93, title safe %90; izleyicide ve storyboard
   karelerinde, G ile açılıp kapanır), karakter boy sıralaması (Varlıklar) ve renk betiği şeridi (Renk). */
import { useEffect, useRef, useState, type RefObject } from "react";
import useSWR from "swr";
import { fetcher } from "@/lib/api";
import { usePanel } from "@/lib/panel";
import { fitRect, insetPct, lineup, meters, SAFE } from "@/lib/safe";
import type { Asset, ColorScript } from "@/lib/types";

/** Güvenli alanlar. `box` verilirse (izleyici) görüntünün "contain" dikdörtgeni ölçülür; verilmezse katman kapsayıcının
    tamamıdır (16:9 storyboard karesi). */
export function SafeAreas({ box, aspect = 16 / 9 }: { box?: RefObject<HTMLElement | null>; aspect?: number }) {
  const { safeAreas } = usePanel();
  const [rect, setRect] = useState<{ left: number; top: number; width: number; height: number } | null>(null);
  useEffect(() => {
    const el = box?.current;
    if (!el || !safeAreas) return;
    const measure = () => setRect(fitRect(el.clientWidth, el.clientHeight, aspect));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [box, aspect, safeAreas]);
  if (!safeAreas) return null;
  const style = box ? (rect ? { left: rect.left, top: rect.top, width: rect.width, height: rect.height } : { display: "none" }) : { inset: 0 };
  return (
    <div className="safe" style={style} aria-hidden="true">
      <div className="safe-a" style={{ inset: insetPct(SAFE.action) }}><span>action safe %93</span></div>
      <div className="safe-t" style={{ inset: insetPct(SAFE.title) }}><span>title safe %90</span></div>
      <i className="safe-c" />
    </div>
  );
}

/** Güvenli alan anahtarı (G kısayolu). */
export function SafeToggle() {
  const { safeAreas, setSafeAreas } = usePanel();
  return (
    <button type="button" className="btn ghost" aria-pressed={safeAreas} data-tour="safe-areas" onClick={() => setSafeAreas(!safeAreas)}
      title="Güvenli alanlar: action safe %93, title safe %90 (G)">Güvenli alanlar</button>
  );
}

/** Karakterlerin boy sıralaması: gerçek boy ölçek metninden okunur (sayı ve birim, yoksa sözcük, yoksa varsayılan). */
export function LineUp({ assets, onPick }: { assets: Asset[]; onPick: (id: string) => void }) {
  const chars = assets.filter((a) => a.type === "character");
  const wrap = useRef<HTMLDivElement>(null);
  const tall = 260, label = 40;                          // figürlerin altındaki ad ve boy alanı (ızgara buna göre)
  const { items, maxM, grid } = lineup(chars.map((a) => ({ id: a.id, name: a.name, scale: a.scale, human: a.character?.kind !== "non_human" })), tall);
  if (!chars.length) return <p className="empty">Karakter yok.</p>;
  const thumb = (a: Asset) => a.references.still?.thumb_url ?? a.references.still?.url ?? a.references.turnaround?.frame_urls?.[0] ?? null;
  return (
    <div className="lineup" ref={wrap} data-tour="lineup">
      <div className="lu-stage" style={{ height: tall + label + 8 }}>
        {grid.map((m) => (
          <div key={m} className="lu-grid" style={{ bottom: label + (m / maxM) * tall }}><span className="mono">{meters(m)}</span></div>
        ))}
        <div className="lu-row">
          {items.map((x) => {
            const a = chars.find((c) => c.id === x.id)!;
            const src = thumb(a);
            return (
              <button key={x.id} type="button" className="lu-item" onClick={() => onPick(x.id)} title={`${a.name}: ${meters(x.height.m)}${x.height.source === "measure" ? "" : " (tahmini)"}${a.scale ? ` · ${a.scale}` : ""}`}>
                <span className="lu-fig" style={{ height: x.px, width: Math.max(18, Math.round(x.px * 0.42)) }}>
                  {src ? <img src={src} alt="" loading="lazy" /> : <span className="lu-sil" />}
                </span>
                <span className="lu-lab" style={{ height: label }}>
                  <span className="lu-name">{a.name}</span>
                  <span className={`mono small${x.height.source === "measure" ? "" : " faint"}`}>{meters(x.height.m)}{x.height.source === "measure" ? "" : " ~"}</span>
                </span>
              </button>
            );
          })}
        </div>
      </div>
      <p className="faint small">Boy, varlığın Gerçek boyut alanından okunur (ör. &quot;1,70 m&quot;, &quot;30 cm&quot;; yoksa &quot;palm-sized&quot;, &quot;çocuk&quot; gibi sözcükler). &quot;~&quot; tahmini boydur: kesin karşılaştırma için Gerçek boyut alanına ölçü yazın.</p>
    </div>
  );
}

/** Renk betiği: her sekansın baskın paleti yan yana (genişlik = payı); altında çekimlerin paleti; sağda hedef palet. */
export function ColorScriptStrip() {
  const { projectId, ov } = usePanel();
  const key = projectId ? `/projects/${projectId}/color-script?v=${encodeURIComponent((ov?.scenes ?? []).map((s) => s.grade_rev ?? "").join("."))}` : null;
  const { data } = useSWR<ColorScript>(key, fetcher, { revalidateOnFocus: false, keepPreviousData: true });
  if (!data) return <p className="faint small pad">Renk betiği hesaplanıyor…</p>;
  return (
    <div className="cscript" data-tour="color-script">
      <div className="cs-seqs">
        {data.sequences.map((q) => (
          <div key={q.id} className="cs-seq" title={`${q.code} · ${q.title}${q.distance != null ? ` · hedefe uzaklık ΔE ${q.distance}` : ""}`}>
            <span className="cs-l"><b className="mono">{q.code}</b> {q.title}{q.distance != null && <span className={`mono small ${q.distance > 25 ? "warn" : "faint"}`}> ΔE {q.distance}</span>}</span>
            <span className="cs-bar">{q.palette.length ? q.palette.map((c) => <i key={c.hex} style={{ background: c.hex, flex: c.weight }} title={`${c.hex} · %${Math.round(c.weight * 100)}`} />) : <em className="faint small">klip yok</em>}</span>
            <span className="cs-shots">{q.shots.map((s) => (
              <span key={s.code} className="cs-shot" title={s.code}>{s.palette.map((c) => <i key={c.hex} style={{ background: c.hex, flex: c.weight }} />)}</span>
            ))}</span>
          </div>
        ))}
      </div>
      <div className="cs-target" title="Hedef palet: etkin stil kılavuzundan">
        <span className="small faint">Hedef{data.style_version ? ` (stil s${data.style_version})` : ""}</span>
        <span className="cs-bar">{data.target.length ? data.target.map((h, i) => <i key={`${h}-${i}`} style={{ background: h, flex: 1 }} title={h} />) : <em className="faint small">stil kılavuzunda palet yok</em>}</span>
      </div>
    </div>
  );
}
