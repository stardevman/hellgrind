"use client";
/* Senaryo sayfası: yeni proje (ad, sanal bütçe, üretim yoğunluğu) ya da mevcut projenin senaryosu; ortada eş aralıklı
   senaryo düzenleyicisi, sağda biçim kılavuzu. Mevcut projede yalnızca değişen çekimler yeniden üretilir. */
import { useState, type FormEvent } from "react";
import useSWR, { useSWRConfig } from "swr";
import { api, fetcher } from "@/lib/api";
import { usePanel } from "@/lib/panel";
import type { DispatchAll, PricingTable } from "@/lib/types";
import { NoteBox, Panel } from "./ui";

interface Ingest extends DispatchAll { cast: string[]; shots: number; created: string[]; updated: string[]; orphaned: string[] }

export function ScriptPage() {
  const { meta, projects, projectId, creating, setCreating, selectProject, setPage, note, ov } = usePanel();
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
  const h3 = prices?.table.self_hosted?.h3;
  const perShot = trial && h3 ? (Number(trial.probe_n) + Number(trial.full_n))
    * (h3.gpu_hourly_usd / 3600) * (h3.render_seconds["480p"] ?? Math.max(...Object.values(h3.render_seconds))) * 4 : null;
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
      } else {
        const started = Object.entries(r.planned).filter(([, st]) => st.length).map(([code]) => code);
        // "updated" arka uçta yeniden yazılan (upsert) çekimlerdir; gerçekten değişenleri planlayıcı söyler.
        note("start", `${r.shots} çekim ayrıştırıldı${r.created.length ? `, ${r.created.length} yeni` : ""}.`
          + (started.length ? ` Üretime alındı: ${started.join(", ")}.` : " Değişen girdi yok; hiçbir çekim yeniden üretilmedi.")
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

  const p = meta?.presets.preview, d = meta?.defaults;
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
                {trial && (
                  <label className="choice">
                    <input type="radio" name="intensity" value="trial" checked={intensity === "trial"} onChange={() => pickIntensity("trial")} />
                    <span><b>MiniMax H3 denemesi</b><small>
                      {`${trial.probe_n} test ve ${trial.full_n} tam klip, 480p, düzeltme turu yok; varlık onayı ve hareket testleri atlanır; ses yerel (espeak), dudak senkronu yok.`}
                      {perShot != null && ` Çekim başına en çok ~$${perShot.toFixed(3)} GPU süresi (3 sn'lik çekim 4 sn üretilir).`}
                      {" "}Yalnızca MiniMax H3 sunucusu (vast.ai) gerekir.
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
          <button className="btn primary lg" type="submit" disabled={sending || !meta}>
            {newProject || !hasShots ? "Senaryoyu işle ve üret" : "Değişiklikleri işle ve üret"}
          </button>
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

      <Panel title="Biçim">
        <ul className="syntax pad">
          <li><code># SEKANS: Dökümhane</code>Yeni sekans başlatır; sonraki sahne başlıkları ona aittir.</li>
          <li><code>İÇ. TERK EDİLMİŞ DÖKÜMHANE - GECE</code>Sahne başlığı (İÇ., DIŞ., İÇ/DIŞ., INT., EXT.). Mekân başlıktan çıkar.</li>
          <li><code>Kızıl bir ışık kazanların arasından süzülüyor.</code>Aksiyon: her paragraf yeni bir çekim. Koş, atla, patla gibi kelimeler geniş açı ve yüksek aksiyon demektir.</li>
          <li><code>Kerem bir taksiye biner. [NESNE: Taksi]</code>Nesne: paragraf içindeyse o çekime, tek satırdaysa sahnenin bütün çekimlerine bağlanır.</li>
          <li><code>{"KEREM\n(tedirgin)\nBurada kimse yok mu?"}</code>Konuşan karakter (büyük harf, en çok 4 kelime), parantezde duygu, ardından replik.</li>
          <li><code>[KARAKTER: Kerem]</code>Konuşmayan karakter: kadroya girer ve adı geçen çekimlere bağlanır; tek satırdaysa sahnenin bütün çekimlerine.</li>
          <li><code>EXT. ROOFTOP - SUNSET</code>Senaryo İngilizce de yazılabilir ([CHARACTER: …], [PROP: …]). Ücretli modda Türkçe tarifler sağlayıcıya gitmeden İngilizceye çevrilir.</li>
        </ul>
      </Panel>
    </form>
  );
}
