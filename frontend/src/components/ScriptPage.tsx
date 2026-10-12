"use client";
/* Senaryo sayfası: yeni proje (ad, sanal bütçe, üretim yoğunluğu) ya da mevcut projenin senaryosu; ortada eş aralıklı
   senaryo düzenleyicisi, sağda biçim kılavuzu. Mevcut projede yalnızca değişen çekimler yeniden üretilir. */
import { useEffect, useState, type FormEvent } from "react";
import useSWR, { useSWRConfig } from "swr";
import { api, fetcher } from "@/lib/api";
import { engineChoices } from "@/lib/engines";
import { ENGINE_TR } from "@/lib/i18n";
import { usePanel } from "@/lib/panel";
import { clipCost } from "@/lib/pricing";
import { awaitingPlanText } from "@/lib/text";
import type { CostEstimate, DispatchAll, PricingTable } from "@/lib/types";
import { NoteBox, Panel } from "./ui";

interface Ingest extends DispatchAll { cast: string[]; shots: number; created: string[]; updated: string[]; orphaned: string[] }

const FROZEN_TITLE = "Üretim durduruldu, önce Üretime devam et";

export function ScriptPage() {
  const { meta, projects, projectId, creating, setCreating, selectProject, setPage, note, ov, frozen, busy, act } = usePanel();
  const { mutate } = useSWRConfig();
  const newProject = creating || !projectId;
  const { data: project } = useSWR<{ script_source: string | null; title: string }>(!newProject ? `/projects/${projectId}` : null, fetcher);
  const [title, setTitle] = useState("Örnek film");
  const [budget, setBudget] = useState("60");
  const [intensity, setIntensity] = useState("preview");
  const [script, setScript] = useState<string | null>(null);      // null: kullanıcı dokunmadı
  const [sending, setSending] = useState(false);
  const source = newProject ? meta?.sample_script : project?.script_source || meta?.sample_script;
  const text = script ?? source ?? "";
  const hasShots = Boolean(ov?.scenes.length);
  const lines = text.split("\n").length, words = text.split(/\s+/).filter(Boolean).length;
  const { data: prices } = useSWR<{ table: PricingTable }>(intensity === "trial" ? "/settings/pricing" : null, fetcher, { revalidateOnFocus: false });
  const trial = meta?.presets.trial;
  // Deneme: çekim başına en çok (test + tam) klip; her klip 480p MiniMax H3 videosu (en az 4 sn GPU'da üretilir).
  // RunPod (varsayılan): en pahalı GPU tipinin saniye ücreti; vast.ai (deneysel): makinenin saatlik ücreti.
  const runpod = meta?.h3_platform !== "vast" ? prices?.table.serverless?.runpod : undefined;
  const h3 = prices?.table.self_hosted?.h3;
  const probeRes = String(trial?.probe_resolution ?? "480p"), fullRes = String(trial?.full_resolution ?? "720p");
  const perShot = !trial ? null : runpod
    ? Math.max(...["fl2va", "ref2va"].map((m) => Number(trial.probe_n) * clipCost(runpod, m, probeRes) + Number(trial.full_n) * clipCost(runpod, m, fullRes)))
    : h3 ? (Number(trial.probe_n) + Number(trial.full_n)) * (h3.gpu_hourly_usd / 3600)
      * (h3.render_seconds[probeRes] ?? Math.max(...Object.values(h3.render_seconds))) * 4 : null;
  const pickIntensity = (x: string) => {
    setIntensity(x);
    if (x === "trial") {
      if (budget === "60") setBudget("3");
      if (script == null && meta?.trial_script) setScript(meta.trial_script);
    } else if (budget === "3") setBudget("60");
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!meta) return;
    setSending(true);
    note("start", "İşleniyor…");
    try {
      let id = projectId;
      if (newProject) {
        const limit = Number(budget);
        const preset = meta.presets[intensity] || {};
        // Hız bekçisi (10 dakikada izin verilen harcama) bütçenin yarısı olarak yazılır: örnek film saniyeler içinde
        // ~22 sanal USD harcar ve varsayılan 20 USD sınırı filmin ortasında projeyi dondururdu.
        const created = await api.post<{ id: string }>("/projects", {
          title, budget_limit_usd: limit, settings: { ...preset, max_spend_per_10min: Math.max(0.5, limit / 2) },
        });
        id = created.id;
      }
      const r = await api.post<Ingest>(`/projects/${id}/script`, { text });
      if (newProject || !hasShots) {
        note("start", "");
        await mutate("/projects");                     // yeni proje listede olmadan seçilirse en yeniye düşülürdü
        selectProject(id);
        setPage("edit");
        if (r.busy?.length) note("inspector", `${r.busy.join(", ")} şu an işlendiği için atlandı.`);
        else if (r.awaiting_plan?.length) note("inspector", awaitingPlanText(r.awaiting_plan));
      } else {
        const started = Object.entries(r.planned).filter(([, st]) => st.length).map(([code]) => code);
        // "updated" arka uçta yeniden yazılan (upsert) çekimlerdir; gerçekten değişenleri planlayıcı söyler.
        note("start", `${r.shots} çekim ayrıştırıldı${r.created.length ? `, ${r.created.length} yeni` : ""}.`
          + (started.length ? ` Üretime alındı: ${started.join(", ")}.` : " Değişen girdi yok; hiçbir çekim yeniden üretilmedi.")
          + (r.awaiting_plan?.length ? ` ${awaitingPlanText(r.awaiting_plan)}` : "")
          + (r.orphaned.length ? ` Senaryoda artık olmayan çekimler silinmedi: ${r.orphaned.join(", ")}.` : ""), "ok");
        setScript(null);
        void mutate(`/projects/${id}`);
      }
    } catch (err) {
      note("start", (err as Error).message, "err");
    } finally {
      setSending(false);
    }
  };

  const p = meta?.presets.preview, d = meta?.defaults, eco = meta?.presets.economy;
  const blockedByFreeze = !newProject && frozen;
  const resume = () => act(() => api.post(`/projects/${projectId}/killswitch`, { action: "resume" }), "start");
  return (
    <form className="page three" onSubmit={submit}>
      <Panel title={newProject ? "Yeni proje" : "Proje"} meta={newProject ? "" : project?.title}
        tools={creating && projects && projects.length > 0 ? <button className="btn ghost" type="button" onClick={() => setCreating(false)}>Vazgeç</button> : undefined}>
        <div className="pad stack">
          {newProject ? (
            <>
              <label className="field">Proje adı
                <input type="text" required maxLength={200} value={title} onChange={(e) => setTitle(e.target.value)} />
              </label>
              <label className="field">Sanal bütçe (USD)
                <input type="number" min={1} max={1000000} step={1} required value={budget} onChange={(e) => setBudget(e.target.value)} />
              </label>
              <div className="stack" role="radiogroup" aria-label="Üretim yoğunluğu" style={{ gap: 6 }}>
                <span className="field">Üretim yoğunluğu</span>
                <label className="choice">
                  <input type="radio" name="intensity" value="preview" checked={intensity === "preview"} onChange={() => pickIntensity("preview")} />
                  <span><b>Önizleme</b><small>{p ? `En çok ${p.probe_n} test ve ${p.full_n} tam klip: hızlı sonuç, örnek film için uygun.` : ""}</small></span>
                </label>
                <label className="choice">
                  <input type="radio" name="intensity" value="brief" checked={intensity === "brief"} onChange={() => pickIntensity("brief")} />
                  <span><b>Brifing</b><small>{d ? `${d.probe_n} test ve en çok ${d.full_n} tam klip: brifingdeki yoğunluk.` : ""}</small></span>
                </label>
                {eco && (
                  <label className="choice">
                    <input type="radio" name="intensity" value="economy" checked={intensity === "economy"} onChange={() => pickIntensity("economy")} />
                    <span><b>Üretim (ekonomik)</b><small>{`${eco.probe_n} test (${eco.probe_resolution}) ve ${eco.full_n} tam klip (${eco.full_resolution}), ${eco.max_correction_rounds} düzeltme turu, havuzda ${eco.refined_k} aday: gerçek film için en az üretim.`}</small></span>
                  </label>
                )}
                {trial && (
                  <label className="choice">
                    <input type="radio" name="intensity" value="trial" checked={intensity === "trial"} onChange={() => pickIntensity("trial")} />
                    <span><b>MiniMax H3 denemesi</b><small>
                      {`${trial.probe_n} test ve ${trial.full_n} tam klip, 480p, düzeltme turu yok; varlık onayı ve hareket testleri atlanır; ses yerel (espeak), dudak senkronu yok.`}
                      {perShot != null && ` Çekim başına en çok ~$${perShot.toFixed(3)} GPU süresi (3 sn'lik çekim 4 sn üretilir).`}
                      {" "}Yalnızca MiniMax H3 ({meta?.h3_platform === "vast" ? "vast.ai makinesi" : "RunPod uç noktası"}) gerekir.
                    </small></span>
                  </label>
                )}
              </div>
              {intensity === "trial" && meta?.dry_run && (
                <div className="note err">
                  Ücretsiz moddasınız: bu proje MiniMax H3 yerine yerel motorla üretilir. H3 ile denemek için önce Ayarlar'da
                  sunucu adresini ve erişim anahtarını girip ücretli moda geçin, sonra buraya dönün.
                  <div className="row mt8"><button className="btn" type="button" onClick={() => setPage("settings")}>Ayarlar'ı aç</button></div>
                </div>
              )}
              {intensity === "trial" && meta && !meta.dry_run && (
                <div className="note ok">Ücretli mod açık: bu proje MiniMax H3 sunucunuzda gerçek GPU süresiyle üretilir; sanal bütçe tavanı gerçek harcamanın da sınırıdır.</div>
              )}
            </>
          ) : (
            <dl className="kv">
              <dt>Çekim</dt><dd className="mono">{ov?.scenes.length ?? 0}</dd>
              <dt>Sekans</dt><dd className="mono">{ov?.sequences.length ?? 0}</dd>
              <dt>Bütçe</dt><dd className="mono">${ov?.project.budget_limit_usd.toFixed(2)}</dd>
            </dl>
          )}
          <p className="faint small">
            {meta?.dry_run === false ? "Ücretli mod açık: üretim gerçek harcama yapar ve bütçe limitinde durur." : "Ücretsiz modda harcama sanaldır; gerçek sağlayıcı fiyatlarıyla hesaplanır."}{" "}
            {(ov?.project.settings.lint_strictness ?? meta?.defaults.lint_strictness) === "block"
              ? "Çekimler, içlerindeki karakter, mekân ve nesneler Varlıklar sayfasında kilitlenene dek engelli kalır."
              : "Varlık kontrolü yalnızca uyarır: kilitlenmemiş varlıklar üretimi durdurmaz."}
          </p>
          <button className="btn primary lg" type="submit" disabled={sending || !meta || blockedByFreeze}
            title={blockedByFreeze ? FROZEN_TITLE : undefined}>
            {newProject || !hasShots ? "Senaryoyu işle ve üret" : "Değişiklikleri işle ve üret"}
          </button>
          {blockedByFreeze && (
            <div className="note err">
              Üretim durduruldu (Kill-Switch): senaryo işlenmez. Önce üretime devam edin; tek çekimi denemek için Kurgu'da
              çekimin denetçisindeki "donmayı geçici kaldır" seçeneğini kullanın.
              <div className="row mt8"><button className="btn" type="button" disabled={busy} onClick={resume}>Üretime devam et</button></div>
            </div>
          )}
          {!newProject && hasShots && <p className="faint small">Yalnızca değişen çekimler yeniden üretilir; değişmeyenler olduğu gibi kalır.</p>}
          <NoteBox id="start" />
        </div>
      </Panel>

      <section className="pnl script">
        <header className="pnl-h">
          <h2>Senaryo</h2><span className="meta mono">{lines} satır · {words} kelime</span>
          <span className="grow" />
          {script != null && <button className="btn ghost" type="button" onClick={() => setScript(null)}>Değişiklikleri geri al</button>}
          {newProject && <button className="btn ghost" type="button" onClick={() => setScript(meta?.sample_script ?? "")}>Örnek senaryo</button>}
          {newProject && meta?.trial_script && <button className="btn ghost" type="button" onClick={() => setScript(meta.trial_script!)}>Deneme senaryosu</button>}
        </header>
        <textarea aria-label="Senaryo" spellCheck={false} required minLength={10} value={text} onChange={(e) => setScript(e.target.value)} />
      </section>

      {!newProject && ov && <Intensity />}

      <Estimate body={newProject ? { text, preset: intensity } : { project_id: projectId, ...(script != null ? { text } : {}) }}
        refresh={newProject ? "" : JSON.stringify(ov?.project.settings ?? {}) + (ov?.scenes.length ?? 0)} />

      <Panel title="Biçim">
        <ul className="syntax pad">
          <li><code># SEKANS: Dökümhane</code>Yeni sekans başlatır; sonraki sahne başlıkları ona aittir.</li>
          <li><code>İÇ. TERK EDİLMİŞ DÖKÜMHANE - GECE</code>Sahne başlığı (İÇ., DIŞ., İÇ/DIŞ., INT., EXT.). Mekân başlıktan çıkar.</li>
          <li><code>Kızıl bir ışık kazanların arasından süzülüyor.</code>Aksiyon: her paragraf yeni bir çekim. Koş, atla, patla gibi kelimeler geniş açı ve yüksek aksiyon demektir.</li>
          <li><code>Kerem bir taksiye biner. [NESNE: Taksi]</code>Nesne: paragraf içindeyse o çekime, tek satırdaysa sahnenin bütün çekimlerine bağlanır.</li>
          <li><code>CUT TO:</code>Sonraki çekim kesmeyle gelir: kamera açısı değişebilir, mekân ve karakterlerin durumu korunur. Aynı sahne başlığı altındaki diğer çekimler önceki çekimin karesinden devam eder (anahtar kare zinciri); yeni sahne başlığı zinciri kırar.</li>
          <li><code>{"KEREM\n(tedirgin)\nBurada kimse yok mu?"}</code>Konuşan karakter (büyük harf, en çok 4 kelime), parantezde duygu, ardından replik.</li>
          <li><code>[KARAKTER: Kerem]</code>Konuşmayan karakter: kadroya girer ve adı geçen çekimlere bağlanır; tek satırdaysa sahnenin bütün çekimlerine.</li>
          <li><code>EXT. ROOFTOP - SUNSET</code>Senaryo İngilizce de yazılabilir ([CHARACTER: …], [PROP: …]). Ücretli modda Türkçe tarifler sağlayıcıya gitmeden İngilizceye çevrilir.</li>
        </ul>
      </Panel>
    </form>
  );
}


const RESOLUTIONS = ["480p", "720p"];
const H3MAX_RESOLUTIONS = ["480P", "768P", "1080P"];
const SEEDANCE25_RESOLUTIONS = ["480p", "720p", "1080p"];
const EDIT_TR: Record<string, string> = { full: "tam", medium: "orta", lightning: "Lightning" };

/** Proje yoğunluğu: test ve tam tur aday sayısı, çözünürlükler ve kimlik eşiği (proje ayarları; PATCH /projects). */
function Intensity() {
  const { ov, busy, act, note, meta } = usePanel();
  const cfg = (ov?.project.settings ?? {}) as Record<string, unknown>;
  const [draft, setDraft] = useState<Record<string, string>>({});
  const fields: { key: string; label: string; kind: "int" | "res" | "num" | "edit" | "audio" | "bool" | "seam" | "engine" | "h3res" | "sdraft" | "sres"; min?: number; max?: number; step?: string }[] = [
    { key: "draft_engine", label: "Taslak motoru (yeni çekimler)", kind: "engine" },
    { key: "final_engine", label: "Final motoru (yükseltme)", kind: "engine" },
    { key: "h3max_resolution", label: "H3 Max çözünürlüğü", kind: "h3res" },
    { key: "seedance25_draft", label: "Seedance 2.5 taslağı (480p önizleme; 7 gün içinde 1080p'ye tamamlanır)", kind: "sdraft" },
    { key: "seedance25_resolution", label: "Seedance 2.5 çözünürlüğü (yalnızca taslak kapalıyken)", kind: "sres" },
    { key: "fal_clip_attempts", label: "fal.ai klip denemesi (kalite kapısından kalırsa)", kind: "int", min: 1, max: 5 },
    { key: "probe_n", label: "Test turu aday sayısı", kind: "int", min: 1, max: 200 },
    { key: "probe_resolution", label: "Test turu çözünürlüğü", kind: "res" },
    { key: "full_n", label: "Tam tur en çok aday", kind: "int", min: 1, max: 1000 },
    { key: "wave_size", label: "Tam tur dalga büyüklüğü", kind: "int", min: 1, max: 200 },
    { key: "full_resolution", label: "Tam tur çözünürlüğü", kind: "res" },
    { key: "refined_k", label: "Rafine havuz", kind: "int", min: 1, max: 3 },
    { key: "identity_threshold", label: "Kimlik eşiği (insan: yüz)", kind: "num", min: 0, max: 1, step: "0.01" },
    { key: "look_threshold", label: "Benzerlik eşiği (robot, hayvan: görsel)", kind: "num", min: 0, max: 1, step: "0.01" },
    { key: "qwen_edit_mode", label: "Karakterli ilk kare kalitesi (Qwen-Image-Edit)", kind: "edit" },
    { key: "audio", label: "H3 sesi (ortak ses + video üretimi)", kind: "audio" },
    { key: "chain_end_frame", label: "Kesme öncesi çekim kendi son karesinde biter (Kn_end)", kind: "bool" },
    { key: "seam_mode", label: "Devam eden çekimlerin dikişi", kind: "seam" },
    { key: "seam_crossfade_frames", label: "Dikişte geçiş (kare; 0 kapalı)", kind: "int", min: 0, max: 6 },
    { key: "arrival_review_threshold", label: "Varış eşiği (altı: inceleme önerilir)", kind: "num", min: 0, max: 1, step: "0.01" },
  ];
  const value = (k: string) => draft[k] ?? String(cfg[k] ?? "");
  const engineDefault = (k: string) => {
    const id = k === "final_engine" ? meta?.video?.final_engine ?? "seedance25" : meta?.video?.draft_engine ?? "h3max";
    return ENGINE_TR[id] ?? id;
  };
  const budgetNow = String(ov?.project.budget_limit_usd ?? "");
  const budget = draft.__budget ?? budgetNow;
  const budgetDirty = draft.__budget != null && draft.__budget !== budgetNow;
  const dirty = fields.filter((f) => draft[f.key] != null && draft[f.key] !== String(cfg[f.key] ?? ""));
  const save = () => act(async () => {
    const settings = Object.fromEntries(dirty.map((f) => [f.key, f.kind === "res" || f.kind === "audio" || f.kind === "seam" ? value(f.key)
      : f.kind === "bool" ? value(f.key) === "true" : f.kind === "sdraft" ? (value(f.key) === "" ? null : value(f.key) === "true")
      : f.kind === "edit" || f.kind === "engine" || f.kind === "h3res" || f.kind === "sres" ? (value(f.key) || null) : Number(value(f.key))]));
    await api.patch(`/projects/${ov!.project.id}`, { settings, ...(budgetDirty ? { budget_limit_usd: Number(budget) } : {}) });
    setDraft({});
    note("script", budgetDirty ? `Kaydedildi; bütçe $${Number(budget).toFixed(2)}.` : "Yoğunluk kaydedildi; sonraki üretimlerde geçerli.", "ok");
  }, "script");
  const changes = dirty.length + (budgetDirty ? 1 : 0);
  return (
    <Panel title="Yoğunluk" meta="test ve tam tur">
      <div className="pad stack">
        <table className="tbl">
          <tbody>
            <tr>
              <td style={{ width: "45%" }}>Bütçe (USD)<small className="mono">budget_limit_usd · harcanan ${(ov?.project.spent_usd ?? 0).toFixed(2)}</small></td>
              <td><input type="number" className="mono" style={{ width: 110 }} aria-label="Bütçe (USD)" disabled={busy} min={0.01} step="0.5"
                value={budget} onKeyDown={(e) => { if (e.key === "Enter") e.preventDefault(); }}
                onChange={(e) => setDraft({ ...draft, __budget: e.target.value })} /></td>
            </tr>
            {fields.map((f) => (
              <tr key={f.key}>
                <td style={{ width: "45%" }}>{f.label}<small className="mono">{f.key}</small></td>
                <td>
                  {f.kind === "engine" ? (
                    <select value={value(f.key)} aria-label={f.label} disabled={busy} onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}>
                      <option value="">Ayarlar'daki varsayılan ({engineDefault(f.key)})</option>
                      {engineChoices(meta?.video, String(cfg[f.key] ?? "") || null).map((e) => (
                        <option key={e.id} value={e.id} disabled={e.disabled && e.id !== cfg[f.key]}>{e.label}</option>
                      ))}
                    </select>
                  ) : f.kind === "h3res" || f.kind === "sres" ? (
                    <select value={value(f.key)} aria-label={f.label} disabled={busy} onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}>
                      <option value="">Ayarlar'daki varsayılan ({(f.kind === "h3res" ? meta?.video?.h3max_resolution : meta?.video?.seedance25_resolution) ?? "—"})</option>
                      {(f.kind === "h3res" ? meta?.video?.h3max_resolutions ?? H3MAX_RESOLUTIONS : meta?.video?.seedance25_resolutions ?? SEEDANCE25_RESOLUTIONS).map((r) => <option key={r} value={r}>{r}</option>)}
                    </select>
                  ) : f.kind === "sdraft" ? (
                    <select value={value(f.key)} aria-label={f.label} disabled={busy} onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}>
                      <option value="">Ayarlar'daki varsayılan ({meta?.video?.seedance25_draft == null ? "—" : meta.video.seedance25_draft ? "açık" : "kapalı"})</option>
                      <option value="true">Açık: 480p önizleme + draft_id (en ucuz)</option>
                      <option value="false">Kapalı: seçili çözünürlükte doğrudan</option>
                    </select>
                  ) : f.kind === "bool" ? (
                    <select value={value(f.key) || "true"} aria-label={f.label} disabled={busy} onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}>
                      <option value="true">Açık: video kendi eylemiyle biter (~0,007 $ / çekim)</option>
                      <option value="false">Kapalı: son kare serbest</option>
                    </select>
                  ) : f.kind === "seam" ? (
                    <select value={value(f.key) || "keyframe"} aria-label={f.label} disabled={busy} onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}>
                      <option value="keyframe">Anahtar kare: çekimler paralel, son kare = sonraki çekimin karesi</option>
                      <option value="exact">Birebir: sonraki çekim önceki klibin gerçek son karesinden başlar (sıralı)</option>
                    </select>
                  ) : f.kind === "audio" ? (
                    <select value={value(f.key) || "on"} aria-label={f.label} disabled={busy} onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}>
                      <option value="on">Açık: H3 sesi üretilir (filmde kullanılmaz)</option>
                      <option value="off">Kapalı: ses VAE'si yüklenmez, video sessiz</option>
                    </select>
                  ) : f.kind === "edit" ? (
                    <select value={value(f.key)} aria-label={f.label} disabled={busy} onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}>
                      <option value="">Ayarlar'daki gibi</option>
                      <option value="full">Tam: 40 adım, cfg 4 (~195 sn)</option>
                      <option value="medium">Orta: 20 adım, cfg 4 (~100 sn)</option>
                      <option value="lightning">Lightning: 4 adım, cfg 1 (~15 sn)</option>
                    </select>
                  ) : f.kind === "res" ? (
                    <select value={value(f.key)} aria-label={f.label} disabled={busy} onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}>
                      {RESOLUTIONS.map((r) => <option key={r} value={r}>{r}</option>)}
                    </select>
                  ) : (
                    <input type="number" className="mono" style={{ width: 110 }} aria-label={f.label} disabled={busy}
                      min={f.min} max={f.max} step={f.step ?? "1"} value={value(f.key)}
                      onKeyDown={(e) => { if (e.key === "Enter") e.preventDefault(); }}
                      onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })} />
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="faint small">Taslak motoru yeni çekimlerin motorudur (kaba kurgu); final motoru çekimin denetçisindeki
          "ile yükselt" düğmesinin motorudur. Var olan çekimlerin motoru denetçiden değişir. Varsayılanlar en ucuzudur: H3 Max
          480P ve Seedance 2.5 taslağı (480p önizleme); seçilen çekimler sonra denetçinin Bitirme bölümünden 1080p'ye tamamlanır
          ya da upscale edilir. Seedance 2.5 çözünürlüğü yalnızca taslak kapalıyken kullanılır.</p>
        <p className="faint small">Kimlik eşiği: insan karakterde yüz kimliği (0-1) bunun altında kalan ya da yüzü hiç görünmeyen aday rafine
          havuza giremez; küçük yüzde kimlik ölçülmez, eleme olmaz. Benzerlik eşiği: robot, hayvan gibi karakterlerde kare ile
          referans arasındaki görsel benzerlik. Tam tur için 720p önerilir; test turu 480p'de ucuzdur. Varış: klibin son karesinin
          hedefe (sonraki çekimin karesi ya da çekimin son karesi) benzerliği; sıralamaya girer, eşiğin altı inceleme önerilir.</p>
        <div className="row">
          <button className="btn primary" type="button" disabled={busy || !changes} onClick={save}>Kaydet{changes ? ` (${changes})` : ""}</button>
          {changes > 0 && <button className="btn ghost" type="button" onClick={() => setDraft({})}>Geri al</button>}
        </div>
      </div>
    </Panel>
  );
}


/** Tahmini maliyet (POST /estimate): yeni projede senaryo + ön ayardan, mevcut projede çekimlerden ve proje ayarlarından.
    Rezervasyonla aynı fiyat fonksiyonları; yazarken 600 ms beklenir. */
function Estimate({ body, refresh }: { body: Record<string, unknown>; refresh: string }) {
  const [est, setEst] = useState<CostEstimate | null>(null);
  const [err, setErr] = useState("");
  const key = JSON.stringify(body) + refresh;
  useEffect(() => {
    if (typeof body.text === "string" && body.text.length < 10) return;
    const timer = setTimeout(() => {
      api.post<CostEstimate>("/estimate", body).then((r) => { setEst(r); setErr(""); }).catch((e: Error) => setErr(e.message));
    }, 600);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const frameTr = (f: string | null) => (f === "qwen_edit" ? "karakterli kare" : f === "qwen" ? "kare" : f === "upload" ? "yüklenen kare" : "kare yok (Ref2VA)");
  return (
    <Panel title="Tahmini maliyet" meta={est ? `${est.shots.length} çekim` : ""}>
      <div className="pad stack">
        {err && <p className="warn small">{err}</p>}
        {est && (
          <>
            <dl className="kv">
              <dt>Toplam (tahmini)</dt><dd className="mono">${est.expected_usd.toFixed(2)}</dd>
              <dt>En çok</dt><dd className="mono">${est.max_usd.toFixed(2)}</dd>
              <dt>Model yükleme</dt><dd className="mono">{est.model_loads} × soğuk başlatma = ${est.cold_start_usd.toFixed(3)}</dd>
              {(est.end_frames ?? 0) > 0 && <><dt>Son kareler</dt><dd className="mono">{est.end_frames} × Lightning = ${(est.end_frame_usd ?? 0).toFixed(3)}</dd></>}
              {((est.cut_in_frames ?? 0) + (est.angles_frames ?? 0)) > 0 && <><dt>Kesme kareleri</dt><dd className="mono">{est.cut_in_frames ?? 0} kırp + iyileştir · {est.angles_frames ?? 0} açı LoRA'sı</dd></>}
            </dl>
            <table className="tbl">
              <thead><tr><th>Çekim</th><th>Kare</th><th>Klip</th><th>Tahmini</th><th>En çok</th></tr></thead>
              <tbody>
                {est.shots.map((s) => (
                  <tr key={s.code}>
                    <td className="mono">{s.code}<small>{s.billed_seconds} sn · {s.mode === "ref2va" ? "Ref2VA" : s.engine === "h3" ? "FL2VA" : s.engine}</small></td>
                    <td>{frameTr(s.frame)}{s.frame_usd ? <small className="mono">${s.frame_usd.toFixed(3)}{s.edit_mode ? ` · ${EDIT_TR[s.edit_mode] ?? s.edit_mode}` : ""}</small> : null}
                      {s.end_frame ? <small className="mono">+ son kare ${(s.end_frame_usd ?? 0).toFixed(3)}</small> : null}
                      {s.cut_in_usd != null ? <small className="mono">kırp + iyileştir ${s.cut_in_usd.toFixed(3)}{s.cut_place_usd ? " (ara kare dahil)" : ""}</small> : null}
                      {s.angles_usd != null ? <small className="mono">açı LoRA'sı ${s.angles_usd.toFixed(3)}</small> : null}</td>
                    <td className="mono">{s.clips}</td>
                    <td className="mono">${s.expected_usd.toFixed(3)}</td>
                    <td className="mono">${s.max_usd.toFixed(3)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="faint small">
              Tahmini: kare + {est.settings.probe_n} test ({est.settings.probe_resolution}) + {est.settings.full_n} tam klip
              ({est.settings.full_resolution}); tam tur erken durursa daha az. En çok: kimlik kapısının yeniden denemeleri ve
              {" "}{est.settings.max_correction_rounds} düzeltme turu dahil. {est.dry_run ? "Ücretsiz modda harcama sanaldır, aynı fiyatlarla." : ""}
            </p>
          </>
        )}
      </div>
    </Panel>
  );
}
