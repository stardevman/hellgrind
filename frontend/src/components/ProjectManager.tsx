"use client";
/* Proje yöneticisi (Resolve'daki gibi, alt çubuğun solundaki ev simgesiyle açılır): diziler sezon sezon, bağımsız filmler
   ve "Yeni film" kartı. Kartta durum, harcanan / limit ve teslim kilidi görünür; açık projenin kartında ilk hazır çekimin
   karesi durur. Yeni dizi, sezon ve bölüm üst banttaki Dizi › Sezon › Bölüm menülerinden açılır. */
import { useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import { fetcher } from "@/lib/api";
import { PROJECT_STATUS_TR } from "@/lib/i18n";
import { usePanel } from "@/lib/panel";
import { episodeLabel } from "@/lib/series";
import { usd } from "@/lib/text";
import type { ProjectListItem, SeriesItem } from "@/lib/types";
import { Icon } from "./icons";

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

export function ProjectManager({ onClose }: { onClose: () => void }) {
  const { projects, projectId, selectProject, setCreating, setPage, ov } = usePanel();
  const { data: tree } = useSWR<SeriesItem[]>("/series", fetcher);
  const [q, setQ] = useState("");
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    input.current?.focus();
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [onClose]);
  const byId = useMemo(() => new Map((projects ?? []).map((p) => [p.id, p])), [projects]);
  const needle = q.trim().toLocaleLowerCase("tr");
  const match = (t: string) => !needle || t.toLocaleLowerCase("tr").includes(needle);
  const loose = (projects ?? []).filter((p) => !p.season_id && match(p.title));
  const thumb = ov?.scenes.flatMap((s) => s.variations).find((v) => v.rank === 1 && v.thumb_url)?.thumb_url ?? null;
  const open = (id: string) => { selectProject(id); onClose(); };
  const fresh = () => { setCreating(true); setPage("script"); onClose(); };
  const series = (tree ?? []).map((s) => ({
    ...s, seasons: s.seasons.map((z) => ({ ...z, episodes: z.episodes.filter((e) => match(e.title) || match(s.title)) })).filter((z) => z.episodes.length),
  })).filter((s) => s.seasons.length);
  return (
    <div className="modal-back" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="pm" role="dialog" aria-modal="true" aria-labelledby="pm-t">
        <div className="pm-h">
          <Icon.home /><h2 id="pm-t">Proje yöneticisi</h2>
          <label className="pm-q"><Icon.search /><input ref={input} type="text" value={q} placeholder="Ara…" aria-label="Proje ara" onChange={(e) => setQ(e.target.value)} /></label>
          <span className="grow" />
          <button type="button" className="btn ghost icon-only" onClick={onClose} aria-label="Kapat"><Icon.close /></button>
        </div>
        <div className="pm-b">
          {series.map((s) => s.seasons.map((z) => (
            <section key={z.id} className="pm-sec">
              <h3>{s.title} <span className="faint">› Sezon {z.number}{z.title ? ` · ${z.title}` : ""}</span></h3>
              <div className="pm-grid">
                {z.episodes.map((e) => {
                  const p = byId.get(e.id) ?? { id: e.id, title: e.title, status: e.status, spent_usd: 0, budget_limit_usd: 0, locked: e.locked };
                  return <Card key={e.id} p={p} sub={episodeLabel(e.episode_no, "")} on={e.id === projectId} thumb={e.id === projectId ? thumb : null} pick={() => open(e.id)} />;
                })}
              </div>
            </section>
          )))}
          <section className="pm-sec">
            <h3>Bağımsız filmler</h3>
            <div className="pm-grid">
              <button type="button" className="pm-card new" onClick={fresh}><Icon.plus /><span className="pm-t">Yeni film</span><span className="pm-s">Ad, bütçe ve senaryo</span></button>
              {loose.map((p) => <Card key={p.id} p={p} sub="Bağımsız film" on={p.id === projectId} thumb={p.id === projectId ? thumb : null} pick={() => open(p.id)} />)}
            </div>
          </section>
          <p className="faint small">Yeni dizi, sezon ve bölüm üst banttaki Dizi › Sezon › Bölüm menülerinden açılır. Teslim edilmiş bölümler “Sürüm sabit” rozetiyle görünür.</p>
        </div>
      </div>
    </div>
  );
}
