"use client";
/* Teslim › Teslim sekmesi: teslim şartnamesi (hazır ayarlar: TV (Türkiye, 25 fps), Web, Platform (yüksek)), QC raporu
   (siyah kare, donma, loudness, fotosensitif, çizgi titremesi; PDF), paket içeriği (dosya listesi) ve hak zinciri (madde
   başına belge; yapay zekâ kullanım beyanı servis geçmişinden otomatik). Eksik hak zinciri maddesi paketi engellemez,
   uyarır. Hepsi yerel ve ücretsizdir. */
import { useState, type FormEvent } from "react";
import useSWR from "swr";
import { api, fetcher } from "@/lib/api";
import { applyPreset, FPS_TR, matchesPreset, specLine } from "@/lib/delivery";
import { POLL_MS, usePanel } from "@/lib/panel";
import type { DeliverySpec, DeliveryState } from "@/lib/types";
import { Icon } from "./icons";
import { NoteBox, Section, UploadForm, useSynced } from "./ui";

function SpecForm({ st, save }: { st: DeliveryState; save: (s: DeliverySpec) => Promise<boolean> }) {
  const { busy } = usePanel();
  const [form, setForm] = useSynced<DeliverySpec>(st.spec, JSON.stringify(st.spec));
  const set = (patch: Partial<DeliverySpec>) => setForm({ ...form, ...patch, preset: null });
  const preset = matchesPreset(st, form);
  const submit = (e: FormEvent) => { e.preventDefault(); void save({ ...form, preset }); };
  return (
    <form className="stack" onSubmit={submit} data-tour="delivery-spec">
      <div className="seg" role="group" aria-label="Hazır ayarlar">
        {Object.entries(st.presets).map(([id, p]) => (
          <button key={id} type="button" aria-pressed={preset === id} onClick={() => setForm(applyPreset(st, id, form))}>{p.label}</button>
        ))}
      </div>
      <div className="form-row">
        <label className="field">Çözünürlük
          <select value={form.resolution} onChange={(e) => set({ resolution: e.target.value })}>
            {st.options.resolution.map((r) => <option key={r} value={r}>{r.replace("x", "×")}</option>)}
          </select>
        </label>
        <label className="field">Kare hızı
          <select value={form.fps} onChange={(e) => set({ fps: e.target.value })}>
            {st.options.fps.map((f) => <option key={f} value={f}>{FPS_TR[f] ?? f}</option>)}
          </select>
        </label>
        <label className="field">Master kodeği
          <select value={form.codec} onChange={(e) => set({ codec: e.target.value })}>
            {Object.entries(st.options.codec).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <label className="field">Loudness (LUFS)
          <input type="number" min={-40} max={-10} step={0.5} value={form.loudness_lufs} onChange={(e) => set({ loudness_lufs: Number(e.target.value) })} style={{ width: 80 }} />
        </label>
      </div>
      <div className="row">
        <label className="check"><input type="checkbox" checked={form.srt} onChange={(e) => set({ srt: e.target.checked })} /> Altyazı (SRT)</label>
        <label className="check"><input type="checkbox" checked={form.me_mix} onChange={(e) => set({ me_mix: e.target.checked })} /> M&amp;E mix</label>
        <label className="check"><input type="checkbox" checked={form.textless} onChange={(e) => set({ textless: e.target.checked })} /> Yazısız sürüm</label>
      </div>
      <div className="row">
        <button className="btn primary" type="submit" disabled={busy}>Şartnameyi kaydet</button>
        <span className="faint small">{specLine(form)}</span>
      </div>
      <p className="faint small">Final film bir sonraki birleştirmede bu biçimle çıkar; görüntü motoru çağrılmaz.{st.custom ? "" : " Şartname kaydedilmedi: film eski varsayılanla (1280×720, 24 fps) birleşir."}</p>
    </form>
  );
}

export function DeliveryPanel() {
  const { projectId, busy, act, note } = usePanel();
  const { data: st, mutate } = useSWR<DeliveryState>(projectId ? `/projects/${projectId}/delivery` : null, fetcher, { refreshInterval: POLL_MS * 2 });
  const [qcWaiting, setQcWaiting] = useState(false);
  if (!st) return <p className="loading">Yükleniyor…</p>;
  const save = (spec: DeliverySpec) => act(async () => {
    await mutate(await api.put<DeliveryState>(`/projects/${projectId}/delivery`, spec), false);
    note("delivery", `Şartname kaydedildi: ${specLine(spec)}. Filmi yeniden birleştirince bu biçimle çıkar.`, "ok");
  }, "delivery");
  const runQc = () => act(async () => {
    await api.post(`/projects/${projectId}/qc`);
    setQcWaiting(true);
    note("delivery", "QC kuyruğa alındı: final film taranıyor.", "ok");
  }, "delivery");
  const makePackage = () => {
    const missing = st.rights.missing;
    if (missing.length && !window.confirm(`Hak zincirinde eksik madde var: ${missing.join(", ")}.\nPaket yine de oluşturulsun mu? (Eksikler BENIOKU dosyasında işaretlenir.)`)) return;
    void act(async () => {
      await api.post(`/projects/${projectId}/delivery/package`);
      note("delivery", "Teslim paketi hazırlanıyor (QC güncel değilse önce QC çalışır).", "ok");
    }, "delivery");
  };
  const qc = st.qc;
  const film = Boolean(st.film.key);
  return (
    <div className="delivery">
      <div className="pad"><NoteBox id="delivery" /></div>
      <Section title="Teslim şartnamesi" meta={st.custom ? (matchesPreset(st, st.spec) ? st.presets[matchesPreset(st, st.spec)!].label : "özel") : "varsayılan"}>
        <SpecForm key={projectId} st={st} save={save} />
      </Section>
      <Section title="QC raporu" meta={qc ? (qc.stale ? "eski film" : qc.passed ? "geçti" : "kaldı") : "çalışmadı"}>
        <div className="stack" data-tour="qc">
          <div className="row">
            <button className="btn" type="button" disabled={busy || !film} onClick={() => void runQc()} title={film ? "Final film taranır" : "Önce filmi birleştir"}>
              {qcWaiting && (!qc || qc.stale) ? "Taranıyor…" : "QC çalıştır"}
            </button>
            {qc && <a className="btn ghost" href={`/api/projects/${projectId}/qc.pdf`} download="qc_raporu.pdf"><Icon.download />PDF indir</a>}
            {qc?.stale && <span className="tag warn" title="Rapor daha önceki bir filme ait">film değişti</span>}
          </div>
          {qc && (
            <ul className="qc">
              {qc.checks.map((c) => (
                <li key={c.id} className={c.passed ? "ok" : "bad"}>
                  <span className="qc-h">{c.passed ? <Icon.check /> : <span className="x" aria-hidden="true">✕</span>}<b>{c.name}</b><span className="faint small">{c.summary}</span></span>
                  {c.issues.map((x, i) => (
                    <span key={i} className="qc-i mono small">{x.tc}{x.scene_code ? ` · ${x.scene_code}` : ""} · <span>{x.detail}</span></span>
                  ))}
                </li>
              ))}
            </ul>
          )}
          {qc && <p className="faint small">Hedef {String(qc.target_lufs).replace(".", ",")} LUFS · {String(qc.fps).replace(".", ",")} fps · {qc.checked_at}. Kalan kontroller Kurgu zaman çizelgesinde işaretlidir.</p>}
        </div>
      </Section>
      <Section title="Paket içeriği" meta={`${st.package.files.filter((f) => f.ready).length}/${st.package.files.length}`}>
        <ul className="pkg">
          {st.package.files.map((f) => (
            <li key={f.path} className={f.ready ? "" : "missing"}><span className="mono small">{f.path}</span><span className="faint small">{f.label}</span></li>
          ))}
        </ul>
        <div className="row">
          <button className="btn primary" type="button" disabled={busy || !film} data-tour="delivery-package" onClick={makePackage}
            title={film ? "Zip: master, izleme kopyası, stem'ler, M&E, SRT, EDL/OTIO, QC raporu, hak zinciri" : "Önce filmi birleştir"}>Paketi oluştur</button>
          {st.package.url && <a className="btn" href={st.package.url} download="teslim_paketi.zip"><Icon.zip />Paketi indir</a>}
        </div>
      </Section>
      <Section title="Hak zinciri" meta={st.rights.missing.length ? `${st.rights.missing.length} eksik` : "tam"}>
        <ul className="rights">
          {st.rights.items.map((r) => (
            <li key={r.kind} className={r.auto || r.files.length ? "ok" : "missing"}>
              <div className="row"><b>{r.label}</b><span className="grow" />
                {r.auto ? <span className="tag info">otomatik · {r.rows} kayıt</span> : r.files.length ? <span className="tag ok">{r.files.length} belge</span> : <span className="tag warn">eksik</span>}
              </div>
              {r.auto ? (
                <div className="row">
                  <span className="faint small">Servis geçmişinden: çekim, motor, işlem, tarih.</span><span className="grow" />
                  <a className="btn ghost" href={`/api/projects/${projectId}/rights/ai-declaration?format=csv`} download>CSV indir</a>
                </div>
              ) : (
                <>
                  {r.files.map((f) => (
                    <div key={f.id} className="row small"><a href={f.url} target="_blank" rel="noreferrer">{f.filename}</a><span className="grow" />
                      <button type="button" className="btn ghost" disabled={busy} onClick={() => act(async () => {
                        await api.del(`/rights/${f.id}`);
                        await mutate();
                        note("delivery", `${f.filename} kaldırıldı.`, "ok");
                      }, "delivery")}>Kaldır</button></div>
                  ))}
                  <UploadForm label="Belge" accept=".pdf,.png,.jpg,.jpeg,.txt,.doc,.docx,.odt,.md" button="Yükle"
                    onUpload={(file) => act(async () => {
                      await api.upload(`/projects/${projectId}/rights/${r.kind}?filename=${encodeURIComponent(file.name)}`, file);
                      await mutate();
                      note("delivery", `${r.label}: ${file.name} yüklendi.`, "ok");
                    }, "delivery")} />
                </>
              )}
            </li>
          ))}
        </ul>
        <p className="faint small">Eksik madde varken paket oluşturulabilir; eksikler paketteki BENIOKU dosyasında işaretlenir.</p>
      </Section>
    </div>
  );
}
