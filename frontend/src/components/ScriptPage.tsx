"use client";
/* Senaryo sayfası: yeni proje (ad, sanal bütçe, üretim yoğunluğu) ya da mevcut projenin senaryosu; ortada eş aralıklı
   senaryo düzenleyicisi, sağda biçim kılavuzu. Mevcut projede yalnızca değişen çekimler yeniden üretilir. */
import { useState, type FormEvent } from "react";
import useSWR, { useSWRConfig } from "swr";
import { api, fetcher } from "@/lib/api";
import { usePanel } from "@/lib/panel";
import type { DispatchAll } from "@/lib/types";
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
                  <input type="radio" name="intensity" value="preview" checked={intensity === "preview"} onChange={() => setIntensity("preview")} />
                  <span><b>Önizleme</b><small>{p ? `En çok ${p.probe_n} test ve ${p.full_n} tam klip: hızlı sonuç, örnek film için uygun.` : ""}</small></span>
                </label>
                <label className="choice">
                  <input type="radio" name="intensity" value="brief" checked={intensity === "brief"} onChange={() => setIntensity("brief")} />
                  <span><b>Brifing</b><small>{d ? `${d.probe_n} test ve en çok ${d.full_n} tam klip: brifingdeki yoğunluk.` : ""}</small></span>
                </label>
              </div>
            </>
          ) : (
            <dl className="kv">
              <dt>Çekim</dt><dd className="mono">{ov?.scenes.length ?? 0}</dd>
              <dt>Sekans</dt><dd className="mono">{ov?.sequences.length ?? 0}</dd>
              <dt>Bütçe</dt><dd className="mono">${ov?.project.budget_limit_usd.toFixed(2)}</dd>
            </dl>
          )}
          <p className="faint small">
            Ücretsiz modda harcama sanaldır; gerçek sağlayıcı fiyatlarıyla hesaplanır. Çekimler, içlerindeki karakter, mekân ve nesneler
            Varlıklar sayfasında kilitlenene dek engelli kalır.
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
        </ul>
      </Panel>
    </form>
  );
}
