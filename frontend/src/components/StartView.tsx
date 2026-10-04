"use client";
import { useState, type FormEvent } from "react";
import { useSWRConfig } from "swr";
import { api } from "@/lib/api";
import { usePanel } from "@/lib/panel";
import type { DispatchAll } from "@/lib/types";
import { NoteBox } from "./ui";

/** Yeni proje ve boş durum: proje adı, sanal bütçe, üretim yoğunluğu ve örnek senaryoyla dolu metin alanı. */
export function StartView() {
  const { meta, projects, projectId, creating, setCreating, selectProject, note } = usePanel();
  const { mutate } = useSWRConfig();
  const newProject = creating || !projectId;
  const [title, setTitle] = useState("Örnek film");
  const [budget, setBudget] = useState("60");
  const [intensity, setIntensity] = useState("preview");
  const [script, setScript] = useState<string | null>(null);      // null: kullanıcı dokunmadı, örnek senaryo gösterilir
  const [sending, setSending] = useState(false);
  const text = script ?? meta?.sample_script ?? "";

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
      const r = await api.post<DispatchAll>(`/projects/${id}/script`, { text });
      note("start", "");
      await mutate("/projects");                       // yeni proje listede olmadan seçilirse en yeniye düşülürdü
      selectProject(id);
      if (r.busy?.length) note("inspector", `${r.busy.join(", ")} şu an işlendiği için atlandı.`);
    } catch (err) {
      note("start", (err as Error).message, "err");
    } finally {
      setSending(false);
    }
  };

  const p = meta?.presets.preview, d = meta?.defaults;
  return (
    <section className="start panel">
      <header>
        <h2>{newProject ? "Yeni proje" : "Senaryoyu yükle"}</h2>
        <span className="spacer" />
        {creating && projects && projects.length > 0 && (
          <button className="btn small" type="button" onClick={() => setCreating(false)}>Vazgeç</button>
        )}
      </header>
      <div className="body">
        <form onSubmit={submit}>
          {newProject && (
            <>
              <div className="row">
                <label className="grow">Proje adı
                  <input type="text" required maxLength={200} value={title} onChange={(e) => setTitle(e.target.value)} />
                </label>
                <label>Sanal bütçe (USD)
                  <input type="number" min={1} max={1000000} step={1} required value={budget} onChange={(e) => setBudget(e.target.value)} />
                </label>
              </div>
              <div className="intensity" role="radiogroup" aria-label="Üretim yoğunluğu">
                <span className="muted">Üretim yoğunluğu</span>
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
          )}
          <label>Senaryo
            <textarea rows={16} spellCheck={false} required minLength={10} value={text} onChange={(e) => setScript(e.target.value)} />
          </label>
          <div className="row row-center">
            <button className="btn primary" type="submit" disabled={sending || !meta}>Senaryoyu işle ve üret</button>
            <span className="muted">
              Ücretsiz modda harcama sanaldır; gerçek sağlayıcı fiyatlarıyla hesaplanır. Çekimler, içlerindeki karakter,
              mekân ve nesneler Varlıklar sekmesinde kilitlenene dek engelli kalır.
            </span>
          </div>
          <NoteBox id="start" />
        </form>
      </div>
    </section>
  );
}
