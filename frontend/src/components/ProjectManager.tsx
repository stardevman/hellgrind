"use client";
/* Proje yöneticisi (Resolve'daki gibi; alt banttaki proje adına tıklayınca, proje hiç yokken kendiliğinden açılır):
   diziler sezon sezon, bağımsız filmler, "Yeni film" ve "Yeni dizi", sezon başına "Yeni bölüm", dizi başına "Yeni sezon";
   altta açık projeyi diziye bağlama ve silme. Kartta durum, harcanan / limit ve teslim kilidi görünür; açık projenin
   kartında ilk hazır çekimin karesi durur. Proje yokken kapatılamaz: önce bir proje açılır. */
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { PROJECT_STATUS_TR } from "@/lib/i18n";
import { usePanel } from "@/lib/panel";
import { episodeLabel } from "@/lib/series";
import { useSeriesActions } from "@/lib/seriesActions";
import { usd } from "@/lib/text";
import type { ProjectListItem } from "@/lib/types";
import { Icon } from "./icons";

/** Tek alanlı küçük form ("Yeni dizi", "Yeni bölüm"). */
function NameForm({ label, placeholder, onSubmit, onCancel }: { label: string; placeholder: string; onSubmit: (v: string) => void; onCancel: () => void }) {
  const [value, setValue] = useState("");
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => { input.current?.focus(); }, []);
  const submit = (e: FormEvent) => { e.preventDefault(); if (value.trim()) onSubmit(value.trim()); };
  return (
    <form className="pm-form" onSubmit={submit}>
      <label className="field">{label}<input ref={input} value={value} maxLength={200} placeholder={placeholder} onChange={(e) => setValue(e.target.value)} /></label>
      <div className="row"><span className="grow" /><button type="button" className="btn ghost" onClick={onCancel}>Vazgeç</button>
        <button type="submit" className="btn primary" disabled={!value.trim()}>Oluştur</button></div>
    </form>
  );
}

function Card({ p, sub, on, thumb, pick }: { p: ProjectListItem; sub: string; on: boolean; thumb?: string | null; pick: () => void }) {
  const limit = p.budget_limit_usd || 1;
  const status = p.locked ? "Sürüm sabit" : PROJECT_STATUS_TR[p.display_status ?? p.status] || p.status;
  return (
    <button type="button" className={`pm-card${on ? " on" : ""}`} onClick={pick} aria-current={on ? "true" : undefined}>
      <span className="pm-th">{thumb ? <img src={thumb} alt="" /> : <Icon.film />}</span>
      <span className="pm-t">{p.title}</span>
      <span className="pm-s">{sub} · {p.locked ? <span className="ok"><Icon.lock />{status}</span> : status}</span>
      <span className="pm-bud" title={`Harcanan ${usd(p.spent_usd)} / limit ${usd(p.budget_limit_usd)}`}>
        <i style={{ width: `${Math.min(100, (p.spent_usd / limit) * 100).toFixed(1)}%` }} />
      </span>
    </button>
  );
}

export function ProjectManager({ onClose, locked }: { onClose: () => void; locked?: boolean }) {
  const { projects, projectId, selectProject, setCreating, setPage, ov, busy } = usePanel();
  const { tree, current, newEpisode, newSeries, newSeason, attach, remove } = useSeriesActions();
  const [q, setQ] = useState("");
  const [form, setForm] = useState<{ kind: "series" } | { kind: "episode"; season: string } | null>(null);
  const [attachTo, setAttachTo] = useState("");
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    input.current?.focus();
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape" && !locked) onClose(); };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [onClose, locked]);
  const byId = useMemo(() => new Map((projects ?? []).map((p) => [p.id, p])), [projects]);
  const needle = q.trim().toLocaleLowerCase("tr");
  const match = (t: string) => !needle || t.toLocaleLowerCase("tr").includes(needle);
  const loose = (projects ?? []).filter((p) => !p.season_id && match(p.title));
  const thumb = ov?.scenes.flatMap((s) => s.variations).find((v) => v.rank === 1 && v.thumb_url)?.thumb_url ?? null;
  const open = (id: string) => { selectProject(id); onClose(); };
  const fresh = () => { setCreating(true); setPage("script"); onClose(); };
  const series = (tree ?? []).map((s) => ({
    ...s, seasons: s.seasons.map((z) => ({ ...z, episodes: z.episodes.filter((e) => match(e.title) || match(s.title)) })),
  })).filter((s) => !needle || s.seasons.some((z) => z.episodes.length));
  const none = projects && projects.length === 0;
  return (
    <div className="modal-back" onMouseDown={(e) => { if (e.target === e.currentTarget && !locked) onClose(); }}>
      <div className="pm" role="dialog" aria-modal="true" aria-labelledby="pm-t">
        <div className="pm-h">
          <Icon.film /><h2 id="pm-t">Proje yöneticisi</h2>
          <label className="pm-q"><Icon.search /><input ref={input} type="text" value={q} placeholder="Ara…" aria-label="Proje ara" onChange={(e) => setQ(e.target.value)} /></label>
          <span className="grow" />
          <button type="button" className="btn" onClick={() => setForm({ kind: "series" })}><Icon.plus />Yeni dizi</button>
          {!locked && <button type="button" className="btn ghost icon-only" onClick={onClose} aria-label="Kapat"><Icon.close /></button>}
        </div>
        <div className="pm-b">
          {none && <p className="hint">Henüz proje yok. Bir bağımsız film ya da dizi oluşturarak başlayın; ad, bütçe ve senaryo bir sonraki ekranda sorulur.</p>}
          {form?.kind === "series" && (
            <NameForm label="Dizi adı" placeholder="ör. Döküm Hanesi" onCancel={() => setForm(null)} onSubmit={(v) => { setForm(null); void newSeries(v).then(onClose); }} />
          )}
          {series.map((s) => (
            <section key={s.id} className="pm-sec">
              {s.seasons.map((z) => (
                <div key={z.id} className="pm-season">
                  <h3>{s.title} <span className="faint">› Sezon {z.number}{z.title ? ` · ${z.title}` : ""}</span>
                    <span className="grow" />
                    <button type="button" className="btn ghost" disabled={busy} onClick={() => setForm({ kind: "episode", season: z.id })}><Icon.plus />Yeni bölüm</button>
                  </h3>
                  {form?.kind === "episode" && form.season === z.id && (
                    <NameForm label="Bölüm adı" placeholder="ör. Kayıp Kalıp" onCancel={() => setForm(null)} onSubmit={(v) => { setForm(null); void newEpisode(z.id, v).then(onClose); }} />
                  )}
                  <div className="pm-grid">
                    {z.episodes.map((e) => {
                      const p = byId.get(e.id) ?? { id: e.id, title: e.title, status: e.status, spent_usd: 0, budget_limit_usd: 0, locked: e.locked };
                      return <Card key={e.id} p={p} sub={episodeLabel(e.episode_no, "")} on={e.id === projectId} thumb={e.id === projectId ? thumb : null} pick={() => open(e.id)} />;
                    })}
                  </div>
                </div>
              ))}
              <button type="button" className="btn ghost pm-more" disabled={busy} onClick={() => void newSeason(s.id).then(onClose)}><Icon.plus />{s.title}: yeni sezon (ilk bölümüyle)</button>
            </section>
          ))}
          <section className="pm-sec">
            <h3>Bağımsız filmler</h3>
            <div className="pm-grid">
              <button type="button" className="pm-card new" onClick={fresh}><Icon.plus /><span className="pm-t">Yeni film</span><span className="pm-s">Ad, bütçe ve senaryo</span></button>
              {loose.map((p) => <Card key={p.id} p={p} sub="Bağımsız film" on={p.id === projectId} thumb={p.id === projectId ? thumb : null} pick={() => open(p.id)} />)}
            </div>
          </section>
          {current && (
            <section className="pm-sec pm-current">
              <h3>Açık proje: {current.title}</h3>
              <div className="row">
                {!current.season_id && tree?.length ? (
                  <>
                    <select value={attachTo} onChange={(e) => setAttachTo(e.target.value)} aria-label="Dizi">
                      <option value="">Diziye bölüm olarak ekle…</option>
                      {tree.map((s) => <option key={s.id} value={s.id}>{s.title}</option>)}
                    </select>
                    <button type="button" className="btn" disabled={!attachTo || busy} onClick={() => { const s = tree.find((x) => x.id === attachTo); if (s) void attach(s).then(() => setAttachTo("")); }}>Ekle</button>
                  </>
                ) : null}
                <span className="grow" />
                <button type="button" className="btn danger" disabled={busy} onClick={() => void remove()}><Icon.trash />{current.season_id ? "Bölümü sil…" : "Projeyi sil…"}</button>
              </div>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
