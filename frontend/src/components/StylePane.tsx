"use client";
import { useEffect, useRef, useState, type FormEvent } from "react";
import useSWR from "swr";
import { api, fetcher } from "@/lib/api";
import { POLL_MS, usePanel } from "@/lib/panel";
import { clock, dispatchText, impactText, isHex, paletteOf } from "@/lib/text";
import type { DispatchAll, StyleImpact, StyleState, StyleVersion } from "@/lib/types";
import { NoteBox, reducedMotion } from "./ui";

const RULE_TYPES = [["character", "Karakterler"], ["location", "Mekânlar"], ["prop", "Nesneler"]] as const;

interface Form { lighting: string; motion: string; rules: Record<string, string>; negative: string; palette: string; notes: string }

const toForm = (src: Partial<StyleVersion>): Form => ({
  lighting: src.lighting_prefix || "", motion: src.motion_style || "",
  rules: Object.fromEntries(RULE_TYPES.map(([t]) => [t, src.rules?.[t] || ""])),
  negative: src.negative || "", palette: (src.palette || []).join(", "), notes: src.notes || "",
});

const Swatches = ({ colors }: { colors: string[] }) => (
  <div className="swatches" aria-hidden="true">
    {colors.map((c, i) => <span key={`${c}-${i}`} className="swatch" style={{ background: c }} title={c} />)}
  </div>
);

export function StylePane() {
  const { projectId, busy, frozen, act, note } = usePanel();
  const { data: st } = useSWR<StyleState>(projectId ? `/projects/${projectId}/style` : null, fetcher, { refreshInterval: POLL_MS });
  const [form, setForm] = useState<Form | null>(null);          // null: kullanıcı dokunmadı, etkin sürüm gösterilir
  const [impact, setImpact] = useState<StyleImpact | null>(null);
  const [renderNow, setRenderNow] = useState(true);
  const impactRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (impact) impactRef.current?.scrollIntoView({ block: "nearest", behavior: reducedMotion() ? "auto" : "smooth" });
  }, [impact]);

  if (!st) return <p className="loading">Yükleniyor…</p>;
  const target = st.active || st.versions[0] || null;
  const f = form ?? toForm(target || { rules: st.defaults });
  const set = (patch: Partial<Form>) => setForm({ ...f, ...patch });

  const preview = (version: number) => act(async () => {
    setImpact(await api.post<StyleImpact>(`/projects/${projectId}/style/preview`, { version }));
    note("style", "");
  }, "style");

  const save = (e: FormEvent) => {
    e.preventDefault();
    const rules: Record<string, string> = {};
    for (const [t] of RULE_TYPES) { const v = f.rules[t].trim(); if (v) rules[t] = v; }
    const body = { lighting_prefix: f.lighting, motion_style: f.motion, rules, negative: f.negative, palette: paletteOf(f.palette), notes: f.notes || null };
    void act(async () => {
      const v = await api.post<StyleVersion>(`/projects/${projectId}/style`, body);
      setImpact(await api.post<StyleImpact>(`/projects/${projectId}/style/preview`, { version: v.version }));
      note("style", `Sürüm ${v.version} kaydedildi (etkin değil). Etkisi sağda; onaylarsan etkinleştir.`, "ok");
    }, "style");
  };

  const activate = (version: number) => act(async () => {
    const r = await api.post<DispatchAll>(`/projects/${projectId}/style/activate`, { version, render: renderNow });
    setImpact(null);
    if (!renderNow) { note("style", `Sürüm ${version} etkin. Etkilenen çekimler bir sonraki üretimde yeniden üretilecek.`, "ok"); return; }
    const x = dispatchText(r);
    note("style", `Sürüm ${version} etkin. ${x.text}`, x.bad ? "err" : "ok");
  }, "style");

  return (
    <div className="style-grid">
      <section className="panel" aria-labelledby="style-edit-title">
        <header>
          <h2 id="style-edit-title">Yeni sürüm</h2><span className="spacer" />
          <button className="btn small" type="button" onClick={() => setForm(toForm(st.example))}>Örnekle doldur</button>
        </header>
        <div className="body">
          <p className="muted">
            Işık dili, hareket tarzı ve render kuralları her çekime aynı ifadeyle eklenir; çekim bazında stil yazılmaz. Kaydetmek hiçbir
            şeyi değiştirmez: önce etkisini önizle, sonra etkinleştir.
          </p>
          <form className="style-form mt12" onSubmit={save}>
            <label>Işık öneki<textarea rows={3} maxLength={2000} value={f.lighting} onChange={(e) => set({ lighting: e.target.value })}
              placeholder="ör. Late-afternoon golden-hour lighting, low warm sun, long dramatic shadows" /></label>
            <label>Hareket tarzı<textarea rows={2} maxLength={2000} value={f.motion} onChange={(e) => set({ motion: e.target.value })}
              placeholder="ör. Cartoony motion, squash and stretch" /></label>
            <div className="rules">
              {RULE_TYPES.map(([t, label]) => (
                <label key={t}>{label}<input type="text" list="st-styles" maxLength={200} value={f.rules[t]}
                  onChange={(e) => set({ rules: { ...f.rules, [t]: e.target.value } })} /></label>
              ))}
            </div>
            <datalist id="st-styles">{Object.keys(st.render_styles).map((k) => <option key={k} value={k} />)}</datalist>
            <label>Negatifler<input type="text" maxLength={2000} value={f.negative} onChange={(e) => set({ negative: e.target.value })} /></label>
            <label>Palet (virgülle, #RRGGBB)<input type="text" placeholder="#E4A23E, #2A2D31" value={f.palette} onChange={(e) => set({ palette: e.target.value })} /></label>
            <Swatches colors={paletteOf(f.palette).filter(isHex)} />
            <label>Notlar<textarea rows={2} maxLength={4000} value={f.notes} onChange={(e) => set({ notes: e.target.value })} /></label>
            <div><button className="btn primary" type="submit" disabled={busy}>Yeni sürüm olarak kaydet</button></div>
          </form>
          <NoteBox id="style" className="mt12" />
        </div>
      </section>
      <section className="panel" aria-labelledby="style-versions-title">
        <header><h2 id="style-versions-title">Sürümler</h2></header>
        <ol className="versions">
          {st.versions.length ? st.versions.map((v) => (
            <li key={v.version} className={v.is_active ? "active" : ""}>
              <div className="card-head">
                <h3>Sürüm {v.version}</h3><span className="muted">{v.created_at ? clock(v.created_at) : ""}</span>
                <span className="spacer" />{v.is_active && <span className="astatus locked">Etkin</span>}
              </div>
              <p className="prefix">{v.prefix || "Işık öneki ve hareket tarzı yok."}</p>
              <p className="muted small">Karakter {v.rules.character} · mekân {v.rules.location} · nesne {v.rules.prop}</p>
              {v.palette.length > 0 && <Swatches colors={v.palette} />}
              <div className="actions-row">
                {!v.is_active && <button className="btn small" type="button" disabled={busy} onClick={() => preview(v.version)}>Etkisini önizle</button>}
                <button className="btn small" type="button" onClick={() => {
                  setForm(toForm(v));
                  note("style", `Sürüm ${v.version} forma alındı; değiştirip yeni sürüm olarak kaydet.`, "ok");
                }}>Düzenlemeye al</button>
              </div>
            </li>
          )) : <li className="muted">Henüz stil kılavuzu yok. Soldan ilk sürümü kaydet.</li>}
        </ol>
        {impact && (
          <div className="impact" ref={impactRef}>
            <h3>Sürüm {impact.version} etkinleşirse</h3>
            <p>{impactText(impact)}</p>
            {impact.affected.some((a) => !a.style_only) && (
              <p className="muted">Bazı çekimlerin başka girdileri de değişmiş; onlar zaten yeniden üretilecekti.</p>
            )}
            <label className="check"><input type="checkbox" checked={renderNow} onChange={(e) => setRenderNow(e.target.checked)} /> Etkilenen çekimleri hemen yeniden üret</label>
            <div className="actions-row">
              <button className="btn primary" type="button" disabled={busy || (frozen && renderNow)} onClick={() => activate(impact.version)}>Etkinleştir</button>
              <button className="btn small" type="button" onClick={() => setImpact(null)}>Vazgeç</button>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
