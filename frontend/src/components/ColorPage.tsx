"use client";
/* Renk sayfası (renk düzenleme yazılımları gibi): üstte seçili çekimin önce/sonra görüntüsü, sağda kaydırıcılı
   renk ayarı, film LUT'u ve süreklilik denetimi, altta bütün çekimlerin klip şeridi (renk sıçramaları işaretli).
   "Kareler" görünümü (kare tutma açıkken ya da yönetici modunda) art arda altı kareyi önce/sonra gösterir: on twos'ta
   her çizim iki karede tekrar eder. */
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { GRADE_FIELDS } from "@/lib/i18n";
import { posterOf } from "@/lib/media";
import { usePanel } from "@/lib/panel";
import { jumpText, resultText } from "@/lib/text";
import type { ContinuityPair, Grade, Scene, StageResult } from "@/lib/types";
import { ColorScriptStrip } from "./Broadcast";
import { NoteBox, Panel, Section, UploadForm, useSynced } from "./ui";

const FIELDS: Record<string, { label: string; min: number; max: number; step: number; neutral: number; unit?: string }> = {
  exposure: { label: "Pozlama", min: -3, max: 3, step: 0.05, neutral: 0, unit: "EV" },
  contrast: { label: "Kontrast", min: 0.5, max: 2, step: 0.05, neutral: 1 },
  saturation: { label: "Doygunluk", min: 0, max: 3, step: 0.05, neutral: 1 },
  gamma: { label: "Gama", min: 0.3, max: 3, step: 0.05, neutral: 1 },
  temperature: { label: "Sıcaklık", min: 2000, max: 12000, step: 100, neutral: 6500, unit: "K" },
};

function Controls({ sc, values, setValues, numeric }: {
  sc: Scene; values: Record<string, string>; setValues: (v: Record<string, string>) => void; numeric: () => Record<string, number>;
}) {
  const { busy, frozen, act, note } = usePanel();
  const apply = (body: Record<string, number>, prefix: string) => act(async () => {
    const r = await api.post<StageResult>(`/scenes/${sc.id}/grade`, body);
    note("color", prefix + resultText(r), r.blocked ? "err" : "ok");
  }, "color");
  if (!sc.lipsync_ready) return <p className="faint">Renk ayarı, çekimin dudak senkronlu videosu hazır olunca yapılır.</p>;
  return (
    <form className="stack" onSubmit={(e) => { e.preventDefault(); void apply(numeric(), "Renk uygulandı. "); }}>
      <p className="faint small">Kaydırıcılar yalnızca sağdaki önizlemeyi değiştirir; <b>Rengi uygula</b> çekimi yeniden renklendirip montajlar (görüntü motoru çağrılmaz).</p>
      {GRADE_FIELDS.map((k) => {
        const f = FIELDS[k];
        return (
          <label key={k} className="slider">
            <span>{f.label}</span>
            <output className="mono">{values[k]}{f.unit ? ` ${f.unit}` : ""}</output>
            <input type="range" min={f.min} max={f.max} step={f.step} value={values[k]} aria-label={f.label}
              onChange={(e) => setValues({ ...values, [k]: e.target.value })}
              onDoubleClick={() => setValues({ ...values, [k]: String(f.neutral) })} />
          </label>
        );
      })}
      <p className="faint tiny">Kaydırıcıya çift tıklamak o ayarı nötre döndürür.</p>
      {frozen && <div className="note err">Üretim durduruldu (Kill-Switch): renk uygulanamaz. Önce üst banttaki <b>Üretime devam et</b> düğmesine basın.</div>}
      <div className="row">
        <button className="btn primary" type="submit" disabled={busy || frozen}>Rengi uygula</button>
        <button className="btn" type="button" disabled={busy || frozen} onClick={() => apply({}, "Nötr renk. ")}>Nötre dön</button>
      </div>
    </form>
  );
}

/** Art arda altı kare: önce ham, sonra renk ayarı ve kare tutmayla. Altındaki numaralar karede görünen çizimdir. */
function HoldStrips({ sc, query, rev, on }: { sc: Scene; query: string; rev: string; on: boolean }) {
  const drawings = (twos: boolean) => Array.from({ length: 6 }, (_, i) => (twos ? Math.floor(i / 2) : i) + 1);
  const Numbers = ({ twos }: { twos: boolean }) => (
    <span className="hold-n mono" aria-hidden="true">{drawings(twos).map((n, i) => <span key={i}>{n}</span>)}</span>
  );
  return (
    <div className="stack" style={{ padding: 10, gap: 10 }}>
      <figure className="hold">
        <img src={`/api/scenes/${sc.id}/grade/strip.jpg?before=true${rev}`} alt="Art arda altı kare, renk ve kare tutmadan önce" />
        <Numbers twos={false} />
        <figcaption>Önce · art arda altı kare, her karede yeni çizim (24 çizim/sn)</figcaption>
      </figure>
      <figure className="hold">
        <img src={`/api/scenes/${sc.id}/grade/strip.jpg?twos=true&${query}${rev}`} alt="Aynı altı kare, renk ayarı ve kare tutmayla" />
        <Numbers twos />
        <figcaption>Sonra · renk ayarı + kare tutma: her çizim iki kare (12 çizim/sn){on ? "" : " · şu an kapalı, yalnızca önizleme"}</figcaption>
      </figure>
    </div>
  );
}

export function ColorPage() {
  const { ov, scene: sc, selectScene, projectId, busy, frozen, act, note, admin } = usePanel();
  const sig = sc ? sc.id + ":" + JSON.stringify(sc.grade) : "";
  const [values, setValues] = useSynced<Record<string, string>>(
    Object.fromEntries(GRADE_FIELDS.map((k) => [k, String(sc ? sc.grade[k as keyof Grade] : FIELDS[k].neutral)])), sig);
  const numeric = () => {
    const out: Record<string, number> = {};
    for (const k of GRADE_FIELDS) { const v = Number(values[k]); if (values[k] !== "" && Number.isFinite(v)) out[k] = k === "temperature" ? Math.round(v) : v; }
    return out;
  };
  const query = new URLSearchParams(Object.entries(numeric()).map(([k, v]) => [k, String(v)])).toString();
  const [preview, setPreview] = useState(query);
  useEffect(() => { const t = setTimeout(() => setPreview(query), 350); return () => clearTimeout(t); }, [query]);   // sürüklemeyi bitirince
  const [view, setView] = useState<"shot" | "seam" | "hold">("shot");
  const [tried, setTried] = useState<string | null>(null);        // kaydırıcılara yüklenmiş öneri (çekim kimliği)
  // Başka çekimin önerisi: önce çekim seçilir, seçim gelince kaydırıcılar yüklenir (useSynced seçimde sunucu değerine döner).
  const [pending, setPending] = useState<{ id: string; values: Record<string, string> } | null>(null);
  useEffect(() => {
    if (pending && sc?.id === pending.id) { setValues(pending.values); setPending(null); }
  }, [pending, sc?.id, setValues]);

  if (!ov || !sc) return null;
  const p = ov.project, report = p.continuity, jumps = report ? report.pairs.filter((x) => x.jump) : [];
  const twos = Boolean(p.settings.on_twos);
  const holdView = twos || admin;
  const jumpTo = new Set(jumps.map((j) => j.to));
  const incoming = jumps.find((j) => j.to_id === sc.id);          // bu çekime gelen geçişteki sıçrama
  const order = ov.scenes.filter((x) => x.sequence_id === sc.sequence_id && x.lipsync_ready);
  const prev = order[order.findIndex((x) => x.id === sc.id) - 1];
  const rev = (x: Scene) => `&r=${x.grade_rev ?? ""}`;
  const current = (x: Scene) => new URLSearchParams(Object.entries(x.grade).map(([k, v]) => [k, String(v)])).toString();
  /** Öneriyi kaydırıcılara yükler (uygulamaz): önizlemede ve geçiş görünümünde denenir. */
  const trySuggestion = (j: ContinuityPair) => {
    if (!j.suggestion) return;
    const vals = Object.fromEntries(GRADE_FIELDS.map((k) => [k, String(j.suggestion![k] ?? FIELDS[k].neutral)]));
    if (j.to_id !== sc.id) { selectScene(j.to_id); setPending({ id: j.to_id, values: vals }); }
    else setValues(vals);
    setTried(j.to_id);
    setView("seam");
    note("color", `${j.to} için önerilen renk kaydırıcılara yüklendi; sağdaki önizleme ve Geçiş görünümü bu ayarı gösterir. Beğenirseniz "Rengi uygula".`, "ok");
  };
  return (
    <div className="page color">
      <section className="pnl cv">
        <header className="pnl-h">
          <h2 className="mono">{sc.scene_code}</h2><span className="meta">{sc.heading}</span>
          <div className="seg" role="group" aria-label="Önizleme görünümü">
            <button type="button" aria-pressed={view === "shot"} onClick={() => setView("shot")} title="Bu çekimin renklendirilmemiş hâli ve kaydırıcılarla önizlemesi">Bu çekim</button>
            <button type="button" aria-pressed={view === "seam"} disabled={!prev} onClick={() => setView("seam")}
              title={prev ? `Önceki çekim ${prev.scene_code} (şu anki rengi) ile bu çekimin önizlemesi yan yana` : "Bu sekansta önceki hazır çekim yok"}>Geçiş</button>
            {holdView && <button type="button" aria-pressed={view === "hold"} onClick={() => setView("hold")}
              title="Art arda altı kare önce ve sonra: kare tutmanın (on twos) etkisi">Kareler</button>}
          </div>
          {twos && <span className="tag info" title="Kare tutma (on twos) açık: renk aşamasında her çizim iki kare">on twos</span>}
          <span className="grow" />
          {sc.graded && <span className="tag warn">renk uygulanmış</span>}
          {sc.grade_auto?.applied && <span className="tag info" title={`Referans ${sc.grade_auto.reference}; ΔE ${sc.grade_auto.delta_e_before?.toFixed(1)} → ${sc.grade_auto.delta_e_after?.toFixed(1)}`}>otomatik eşleştirildi ({sc.grade_auto.reference})</span>}
          {sc.grade_auto?.applied && <button className="btn ghost" type="button" disabled={busy || frozen} onClick={() => act(async () => {
            const r = await api.post<StageResult>(`/scenes/${sc.id}/grade/auto-undo`);
            note("color", "Otomatik eşleştirme geri alındı; bu çekim bir daha otomatik eşleştirilmez. " + resultText(r), "ok");
          }, "color")}>Geri al</button>}
        </header>
        {!sc.lipsync_ready ? <p className="empty">Bu çekimin dudak senkronlu videosu henüz hazır değil; renk önizlemesi o zaman açılır.</p>
          : view === "hold" && holdView ? <HoldStrips sc={sc} query={preview} rev={rev(sc)} on={twos} />
          : view === "seam" && prev ? (
            <div className="split">
              <figure><img src={`/api/scenes/${prev.id}/grade/preview.jpg?${current(prev)}${rev(prev)}`} alt={`Önceki çekim ${prev.scene_code}`} /><figcaption>Önceki çekim <span className="mono">{prev.scene_code}</span> · şu anki rengi</figcaption></figure>
              <figure><img src={`/api/scenes/${sc.id}/grade/preview.jpg?${preview}${rev(sc)}`} alt={`Bu çekim ${sc.scene_code}, önizleme`} /><figcaption>Bu çekim <span className="mono">{sc.scene_code}</span> · kaydırıcılarla önizleme</figcaption></figure>
            </div>
          ) : (
            <div className="split">
              <figure><img src={`/api/scenes/${sc.id}/grade/preview.jpg?before=true${rev(sc)}`} alt="Renk ayarından önce" /><figcaption>Önce · renklendirilmemiş</figcaption></figure>
              <figure><img src={`/api/scenes/${sc.id}/grade/preview.jpg?${preview}${rev(sc)}`} alt="Renk ayarından sonra" /><figcaption>Sonra · kaydırıcılarla önizleme</figcaption></figure>
            </div>
          )}
        {incoming && view !== "seam" && (
          <div className="hint" style={{ margin: 8 }}>
            Bu çekime gelen geçişte renk sıçraması var ({incoming.from} → {incoming.to}). <button type="button" className="btn ghost" onClick={() => setView("seam")}>Geçişi göster</button>
          </div>
        )}
      </section>

      <Panel className="cc" title="Renk ayarı" meta={p.lut ? "film LUT'u etkin" : ""} bodyClass="">
        <div className="pad"><Controls key={sc.id} sc={sc} values={values} setValues={setValues} numeric={numeric} /></div>
        <Section title="Film LUT'u" meta={p.lut ? "etkin" : "yok"}>
          <UploadForm label="Film LUT'u (.cube)" accept=".cube" button="LUT yükle"
            onUpload={(file) => act(async () => {
              const r = await api.upload<{ lut: { size: number }; regraded: string[] }>(`/projects/${projectId}/lut?filename=${encodeURIComponent(file.name)}`, file);
              note("color", `LUT yüklendi (${r.lut.size}³); yeniden renklendirilen: ${r.regraded.join(", ") || "yok"}.`, "ok");
            }, "color")} />
          {p.lut && (
            <div className="row">
              <button className="btn" type="button" disabled={busy} onClick={() => act(async () => {
                const r = await api.del<{ regraded: string[] }>(`/projects/${projectId}/lut`);
                note("color", `LUT kaldırıldı; yeniden renklendirilen: ${r.regraded.join(", ") || "yok"}.`, "ok");
              }, "color")}>LUT&apos;u kaldır</button>
            </div>
          )}
        </Section>
        <Section title="Süreklilik" meta={report ? (jumps.length ? `${jumps.length} sıçrama` : "temiz") : "denetlenmedi"}>
          <div className="row">
            <button className="btn" type="button" disabled={busy || frozen}
              onClick={() => act(async () => { await api.post(`/projects/${projectId}/continuity`); note("color", "Süreklilik denetimi kuyruğa alındı.", "ok"); }, "color")}>
              Sürekliliği denetle
            </button>
            <button className="btn" type="button" disabled={busy || frozen} title="Sekansta medyana en yakın çekim referans alınır; elle ayarlanmamış çekimlerin rengi karakter dışı bölgenin LAB ölçüsüne göre eşleştirilir (GRADE + MUX)"
              onClick={() => act(async () => { await api.post(`/projects/${projectId}/color/auto-match`); note("color", "Otomatik çekim eşleştirme kuyruğa alındı.", "ok"); }, "color")}>
              Çekimleri otomatik eşleştir
            </button>
          </div>
          <p className="faint small">
            Aynı sekansta art arda gelen iki çekimin orta kareleri karşılaştırılır (karakter dışı bölgede parlaklık ve renk).
            Fark eşiği aşarsa izleyici kesmede bir "atlama" hisseder; öneri, sonraki çekimi öncekine yaklaştıran renk ayarıdır.
            Yeni sahnede ve kamera değişiminde eşik gevşektir.
          </p>
          {!report ? <p className="faint small">Süreklilik henüz denetlenmedi; film birleşince kendiliğinden denetlenir.</p>
            : jumps.length ? jumps.map((j) => (
              <div className="jump" key={`${j.from}-${j.to}`}>
                <b className="mono">{j.from} → {j.to}{j.seam === "new_scene" ? " · yeni sahne" : ""}</b>
                <span>{jumpText(j)}</span>
                <ol className="howto">
                  <li><b>Gör:</b> çekimi açıp <b>Geçiş</b> görünümünde iki kareyi yan yana izle.</li>
                  <li><b>Dene:</b> öneri kaydırıcılara yüklenir, önizlemede görürsün; istersen elle oynat.</li>
                  <li><b>Uygula:</b> beğenirsen <b>Rengi uygula</b>; görüntü motoru çağrılmaz, yalnızca renk ve montaj çalışır.</li>
                </ol>
                <div className="row">
                  <button className="btn" type="button" onClick={() => { if (j.to_id !== sc.id) selectScene(j.to_id); setView("seam"); }}>Geçişi göster</button>
                  {j.suggestion && <button className="btn" type="button" disabled={tried === j.to_id && sc.id === j.to_id} onClick={() => trySuggestion(j)}>Öneriyi dene</button>}
                  {j.suggestion && (
                    <button className="btn primary" type="button" disabled={busy || frozen} title={frozen ? "Üretim durduruldu: önce Üretime devam et" : "Öneriyi doğrudan uygular (renk + montaj)"}
                      onClick={() => act(async () => {
                        const r = await api.post<StageResult>(`/scenes/${j.to_id}/grade`, j.suggestion);
                        note("color", "Önerilen renk uygulandı: " + resultText(r) + " Süreklilik, montaj bitince yeniden ölçülür.", "ok");
                      }, "color")}>Öneriyi uygula</button>
                  )}
                </div>
                {!j.suggestion && <span className="faint small">Öneri hesaplanmadı: sonraki çekimin renklendirilecek videosu henüz yok.</span>}
              </div>
            ))
            : <p className="faint small">Renk sıçraması yok ({report.pairs.length} geçiş denetlendi).</p>}
        </Section>
        <div className="pad"><NoteBox id="color" /></div>
      </Panel>

      <Panel className="cs" title="Klip şeridi" meta="üstte renk betiği: sekansların baskın paleti ve stil hedefi · kırmızı çentik: renk sıçraması · sarı nokta: renk uygulanmış">
        <ColorScriptStrip />
        <div className="clipstrip">
          {ov.scenes.map((x) => {
            const th = posterOf(x);
            return (
              <button key={x.id} type="button" className="cs-item" aria-current={x.id === sc.id} onClick={() => selectScene(x.id)}
                aria-label={`${x.scene_code} çekimini renk için aç`}>
                {jumpTo.has(x.scene_code) && <span className="cs-jump" title="Renk sıçraması" />}
                {th ? <img src={th} alt="" loading="lazy" /> : <span className="ph" />}
                {x.graded && <span className="graded" title="Renk uygulanmış" />}
                <span className="mono">{x.scene_code}</span>
              </button>
            );
          })}
        </div>
      </Panel>
    </div>
  );
}
