"use client";
/* Stil sayfası: solda sürüm geçmişi, ortada yeni sürüm düzenleyicisi, sağda seçili sürümün prompt öneki ve
   etkinleştirmenin etkisi (hangi çekimler yeniden üretilir, tahmini sanal maliyet). Onay olmadan hiçbir şey üretilmez. */
import { useState, type FormEvent } from "react";
import useSWR from "swr";
import { api, fetcher } from "@/lib/api";
import { POLL_MS, usePanel } from "@/lib/panel";
import { clock, dispatchText, impactText, isHex, paletteOf } from "@/lib/text";
import type { DispatchAll, StyleImpact, StyleState, StyleVersion } from "@/lib/types";
import { NoteBox, Panel } from "./ui";

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

export function StylePage() {
  const { projectId, busy, frozen, act, note } = usePanel();
  const { data: st } = useSWR<StyleState>(projectId ? `/projects/${projectId}/style` : null, fetcher, { refreshInterval: POLL_MS });
  const [form, setForm] = useState<Form | null>(null);          // null: kullanıcı dokunmadı, etkin sürüm gösterilir
  const [picked, setPicked] = useState<number | null>(null);
  const [impact, setImpact] = useState<StyleImpact | null>(null);
  const [renderNow, setRenderNow] = useState(true);

  if (!st) return <p className="loading">Yükleniyor…</p>;
  const target = st.active || st.versions[0] || null;
  const f = form ?? toForm(target || { rules: st.defaults });
  const set = (patch: Partial<Form>) => setForm({ ...f, ...patch });
  const shown = st.versions.find((v) => v.version === picked) ?? target;

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
      setPicked(v.version);
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
    <div className="page three">
      <Panel title="Sürümler" meta={st.active ? `etkin: ${st.active.version}` : "etkin sürüm yok"}>
        <ol className="versions">
          {st.versions.length ? st.versions.map((v) => (
            <li key={v.version}>
              <button type="button" className="v" aria-current={shown?.version === v.version} onClick={() => { setPicked(v.version); setImpact(null); }}>
                <span className="row"><b>Sürüm {v.version}</b><span className="faint mono small">{v.created_at ? clock(v.created_at) : ""}</span>
                  <span className="grow" />{v.is_active && <span className="tag ok">Etkin</span>}</span>
                <span className="prefix">{v.prefix || "Işık öneki ve hareket tarzı yok."}</span>
                {v.palette.length > 0 && <Swatches colors={v.palette} />}
              </button>
            </li>
          )) : <li className="pad faint">Henüz stil kılavuzu yok. Ortadan ilk sürümü kaydet.</li>}
        </ol>
      </Panel>

      <Panel title="Yeni sürüm" tools={<button className="btn" type="button" onClick={() => setForm(toForm(st.example))}>Örnekle doldur</button>}>
        <form className="pad stack" onSubmit={save}>
          <p className="faint small">
            Işık dili, hareket tarzı ve render kuralları her çekime aynı ifadeyle eklenir; çekim bazında stil yazılmaz. Kaydetmek hiçbir
            şeyi değiştirmez: önce etkisini önizle, sonra etkinleştir.
          </p>
          <label className="field">Işık öneki<textarea rows={3} maxLength={2000} value={f.lighting} onChange={(e) => set({ lighting: e.target.value })}
            placeholder="ör. Late-afternoon golden-hour lighting, low warm sun, long dramatic shadows" /></label>
          <label className="field">Hareket tarzı<textarea rows={2} maxLength={2000} value={f.motion} onChange={(e) => set({ motion: e.target.value })}
            placeholder="ör. Cartoony motion, squash and stretch" /></label>
          <div className="row" style={{ alignItems: "end" }}>
            {RULE_TYPES.map(([t, label]) => (
              <label key={t} className="field grow">{label}<input type="text" list="st-styles" maxLength={200} value={f.rules[t]}
                onChange={(e) => set({ rules: { ...f.rules, [t]: e.target.value } })} /></label>
            ))}
          </div>
          <datalist id="st-styles">{Object.keys(st.render_styles).map((k) => <option key={k} value={k} />)}</datalist>
          <label className="field">Negatifler<input type="text" maxLength={2000} value={f.negative} onChange={(e) => set({ negative: e.target.value })} /></label>
          <label className="field">Palet (virgülle, #RRGGBB)<input type="text" placeholder="#E4A23E, #2A2D31" value={f.palette} onChange={(e) => set({ palette: e.target.value })} /></label>
          <Swatches colors={paletteOf(f.palette).filter(isHex)} />
          <label className="field">Notlar<textarea rows={2} maxLength={4000} value={f.notes} onChange={(e) => set({ notes: e.target.value })} /></label>
          <div className="row"><button className="btn primary" type="submit" disabled={busy}>Yeni sürüm olarak kaydet</button></div>
          <NoteBox id="style" />
        </form>
      </Panel>

      <Panel title={shown ? `Sürüm ${shown.version}` : "Sürüm"} meta={shown?.is_active ? "etkin" : shown ? "etkin değil" : ""}>
        {shown ? (
          <div className="pad stack">
            <div className="field">Prompt öneki (her çekimin başına eklenir)<div className="prompt">{shown.prefix || "—"}</div></div>
            <dl className="kv">
              <dt>Karakter</dt><dd className="mono">{shown.rules.character}</dd>
              <dt>Mekân</dt><dd className="mono">{shown.rules.location}</dd>
              <dt>Nesne</dt><dd className="mono">{shown.rules.prop}</dd>
              <dt>Negatifler</dt><dd>{shown.negative || "—"}</dd>
            </dl>
            <div className="row">
              {!shown.is_active && <button className="btn" type="button" disabled={busy} onClick={() => preview(shown.version)}>Etkisini önizle</button>}
              <button className="btn ghost" type="button" onClick={() => { setForm(toForm(shown)); note("style", `Sürüm ${shown.version} forma alındı; değiştirip yeni sürüm olarak kaydet.`, "ok"); }}>Düzenlemeye al</button>
            </div>
            {impact && (
              <div className="stack" style={{ borderTop: "1px solid var(--line)", paddingTop: 10 }}>
                <h3 className="small">Sürüm {impact.version} etkinleşirse</h3>
                <p className="small">{impactText(impact)}</p>
                {impact.affected.length > 0 && (
                  <div className="row">{impact.affected.map((a) => <span key={a.scene_code} className={`tag ${a.style_only ? "info" : ""}`}>{a.scene_code}</span>)}</div>
                )}
                {impact.affected.some((a) => !a.style_only) && <p className="faint small">Bazı çekimlerin başka girdileri de değişmiş; onlar zaten yeniden üretilecekti.</p>}
                <label className="check"><input type="checkbox" checked={renderNow} onChange={(e) => setRenderNow(e.target.checked)} /> Etkilenen çekimleri hemen yeniden üret</label>
                <div className="row">
                  <button className="btn primary" type="button" disabled={busy || (frozen && renderNow)} onClick={() => activate(impact.version)}>Etkinleştir</button>
                  <button className="btn ghost" type="button" onClick={() => setImpact(null)}>Vazgeç</button>
                </div>
              </div>
            )}
          </div>
        ) : <p className="empty">Sürüm yok.</p>}
      </Panel>
    </div>
  );
}
