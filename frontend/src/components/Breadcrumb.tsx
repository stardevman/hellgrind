"use client";
/* Alt banttaki kırıntı yolu: Dizi › Sezon 1 › Bölüm 3 (docs/dizi-hiyerarsisi.md). Bölüm bugünkü projedir; URL'de yalnızca
   ?p=<bölüm> kalır, dizi ve sezon bölümden türetilir. Bağımsız film tek parça görünür. Her kırıntı kendi menüsünü yukarı
   açar: diziler, sezonlar, bölümler (numara, ad, durum ve teslim rozeti) ve "Yeni …" formları. Teslim edilmiş bölümün
   yanında "Sürüm sabit · v004" rozeti durur. */
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import useSWR from "swr";
import { api, fetcher } from "@/lib/api";
import { PROJECT_STATUS_TR } from "@/lib/i18n";
import { POLL_MS, usePanel } from "@/lib/panel";
import { episodeLabel, lockLabel } from "@/lib/series";
import type { EpisodeItem, SeriesItem } from "@/lib/types";
import { Icon } from "./icons";

/** Menülü kırıntı: dışarı tıklama ve Esc kapatır. `children` menüyü kapatma işleviyle çizer. */
function Crumb({ label, sub, aria, children, tour, fixed }: { label: string; sub?: string; aria: string; tour?: string; fixed?: boolean; children: (close: () => void) => ReactNode }) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc); };
  }, [open]);
  return (
    <div className={`switcher crumb${fixed ? " fixed" : ""}`} ref={box} data-tour={tour}>
      <button type="button" className="switch-btn" aria-haspopup="menu" aria-expanded={open} aria-label={aria} onClick={() => setOpen((v) => !v)}>
        <span className="sw-title">{label}</span>
        {sub && <span className="sw-status">{sub}</span>}
        <Icon.chevron />
      </button>
      {open && <div className="menu" role="menu">{children(() => setOpen(false))}</div>}
    </div>
  );
}

/** Menü içi tek alanlı form ("Yeni dizi", "Yeni bölüm"). */
function MenuForm({ label, placeholder, onSubmit, onCancel }: { label: string; placeholder: string; onSubmit: (v: string) => void; onCancel: () => void }) {
  const [value, setValue] = useState("");
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => { input.current?.focus(); }, []);
  const submit = (e: FormEvent) => { e.preventDefault(); if (value.trim()) onSubmit(value.trim()); };
  return (
    <form className="menu-form" onSubmit={submit}>
      <label className="field">{label}
        <input ref={input} value={value} maxLength={200} placeholder={placeholder} onChange={(e) => setValue(e.target.value)} />
      </label>
      <div className="row">
        <span className="grow" />
        <button type="button" className="btn ghost" onClick={onCancel}>Vazgeç</button>
        <button type="submit" className="btn primary" disabled={!value.trim()}>Oluştur</button>
      </div>
    </form>
  );
}

function EpisodeRow({ e, on, pick }: { e: EpisodeItem; on: boolean; pick: () => void }) {
  return (
    <button type="button" role="menuitem" className={on ? "on" : ""} onClick={pick}>
      <span className="mi-ck">{on && <Icon.check />}</span>
      <span className="mi-t">{episodeLabel(e.episode_no, e.title)}</span>
      {e.locked ? <span className="mi-s lock-s"><Icon.lock />{lockLabel(e.locked_label)}</span>
        : <span className="mi-s">{PROJECT_STATUS_TR[e.status] || e.status}</span>}
    </button>
  );
}

export function Breadcrumb({ onRemove }: { onRemove: () => void }) {
  const { projects, projectId, selectProject, setCreating, setPage, ov, act } = usePanel();
  const { data: tree, mutate } = useSWR<SeriesItem[]>("/series", fetcher, { refreshInterval: POLL_MS * 5 });
  const [form, setForm] = useState<"series" | "episode" | null>(null);
  const current = projects?.find((p) => p.id === projectId);
  const ep = ov?.project.episode;
  const series = ep?.series ? tree?.find((x) => x.id === ep.series!.id) : undefined;
  const season = series?.seasons.find((z) => z.id === ep?.season?.id);
  const loose = (projects ?? []).filter((p) => !p.season_id);
  const budget = ov?.project.budget_limit_usd ?? 50;

  const open = (id: string) => { selectProject(id); setForm(null); };
  const firstOf = (q: SeriesItem) => q.seasons.flatMap((z) => z.episodes)[0];
  const newEpisode = (seasonId: string, title: string) => act(async () => {
    const r = await api.post<{ project_id: string }>(`/seasons/${seasonId}/episodes`, { title, budget_limit_usd: budget, settings: {} });
    await mutate();
    open(r.project_id);
    setPage("script");
  }, "global");
  const newSeries = (title: string) => act(async () => {
    const q = await api.post<SeriesItem>("/series", { title });
    const r = await api.post<{ project_id: string }>(`/seasons/${q.seasons[0].id}/episodes`, { title: "Bölüm 1", budget_limit_usd: budget, settings: {} });
    await mutate();
    open(r.project_id);
    setPage("script");
  }, "global");
  const newSeason = (q: SeriesItem) => act(async () => {
    const z = await api.post<{ id: string; number: number }>(`/series/${q.id}/seasons`, {});
    const r = await api.post<{ project_id: string }>(`/seasons/${z.id}/episodes`, { title: "Bölüm 1", budget_limit_usd: budget, settings: {} });
    await mutate();
    open(r.project_id);
    setPage("script");
  }, "global");
  const attach = (q: SeriesItem) => act(async () => {
    const last = q.seasons[q.seasons.length - 1];
    await api.post(`/projects/${projectId}/episode`, { season_id: last.id });
    await mutate();
  }, "global");

  /** Dizi menüsü: diziler (ilk bölüme gider), bağımsız filmler ve "Yeni dizi" / "Yeni proje". */
  const scopeMenu = (close: () => void) => (
    <>
      {form === "series" ? (
        <MenuForm label="Dizi adı" placeholder="ör. Döküm Hanesi" onCancel={() => setForm(null)} onSubmit={(v) => { close(); void newSeries(v); }} />
      ) : (
        <>
          {tree?.map((q) => {
            const first = firstOf(q);
            return (
              <button key={q.id} type="button" role="menuitem" className={q.id === series?.id ? "on" : ""} disabled={!first}
                onClick={() => { if (first) { open(first.id); close(); } }}>
                <span className="mi-ck">{q.id === series?.id && <Icon.check />}</span>
                <span className="mi-t">{q.title}</span>
                <span className="mi-s">{q.seasons.length} sezon · {q.seasons.reduce((n, z) => n + z.episodes.length, 0)} bölüm</span>
              </button>
            );
          })}
          {tree?.length ? <div className="menu-sep" /> : null}
          {loose.map((p) => (
            <button key={p.id} type="button" role="menuitem" className={p.id === projectId ? "on" : ""} onClick={() => { open(p.id); close(); }}>
              <span className="mi-ck">{p.id === projectId && <Icon.check />}</span>
              <span className="mi-t">{p.title}</span>
              <span className="mi-s">{p.locked ? "Sürüm sabit" : PROJECT_STATUS_TR[p.display_status ?? p.status] || p.status}</span>
            </button>
          ))}
          {loose.length ? <div className="menu-sep" /> : null}
          <button type="button" role="menuitem" onClick={() => setForm("series")}>
            <span className="mi-ck"><Icon.plus /></span><span className="mi-t">Yeni dizi…</span>
          </button>
          <button type="button" role="menuitem" onClick={() => { close(); setCreating(true); setPage("script"); }}>
            <span className="mi-ck"><Icon.plus /></span><span className="mi-t">Yeni bağımsız film</span>
          </button>
          {current && !current.season_id && tree?.map((q) => (
            <button key={`a-${q.id}`} type="button" role="menuitem" onClick={() => { close(); void attach(q); }}>
              <span className="mi-ck"><Icon.plus /></span><span className="mi-t">Bu filmi “{q.title}” dizisine bölüm olarak ekle</span>
            </button>
          ))}
          {current && (
            <button type="button" role="menuitem" className="danger" onClick={() => { close(); onRemove(); }}>
              <span className="mi-ck"><Icon.trash /></span><span className="mi-t">{current.season_id ? "Bölümü sil…" : "Projeyi sil…"}</span>
            </button>
          )}
        </>
      )}
    </>
  );

  if (!ep?.series || !ep.season) {
    return (
      <nav className="crumbs" aria-label="Proje" data-tour="project">
        <Crumb aria="Proje seçici" label={current ? current.title : projects?.length ? "Proje seç" : "Henüz proje yok"}
          sub={current ? (current.locked ? "Sürüm sabit" : PROJECT_STATUS_TR[current.display_status ?? current.status] || current.status) : undefined}>
          {scopeMenu}
        </Crumb>
        <LockChip />
      </nav>
    );
  }
  return (
    <nav className="crumbs" aria-label="Dizi, sezon ve bölüm" data-tour="project">
      <Crumb aria="Dizi seçici" label={ep.series.title}>{scopeMenu}</Crumb>
      <span className="crumb-sep" aria-hidden="true">›</span>
      <Crumb aria="Sezon seçici" label={`Sezon ${ep.season.number}`} fixed>
        {(close) => (
          <>
            {series?.seasons.map((z) => {
              const first = z.episodes[0];
              return (
                <button key={z.id} type="button" role="menuitem" className={z.id === season?.id ? "on" : ""} disabled={!first}
                  onClick={() => { if (first) { open(first.id); close(); } }}>
                  <span className="mi-ck">{z.id === season?.id && <Icon.check />}</span>
                  <span className="mi-t">Sezon {z.number}{z.title ? ` · ${z.title}` : ""}</span>
                  <span className="mi-s">{z.episodes.length} bölüm</span>
                </button>
              );
            })}
            <div className="menu-sep" />
            {series && (
              <button type="button" role="menuitem" onClick={() => { close(); void newSeason(series); }}>
                <span className="mi-ck"><Icon.plus /></span><span className="mi-t">Yeni sezon (ilk bölümüyle)</span>
              </button>
            )}
          </>
        )}
      </Crumb>
      <span className="crumb-sep" aria-hidden="true">›</span>
      <Crumb aria="Bölüm seçici" label={episodeLabel(ep.episode_no, current?.title ?? ov?.project.title ?? "")}>
        {(close) => form === "episode" && season ? (
          <MenuForm label="Bölüm adı" placeholder="ör. Kayıp Kalıp" onCancel={() => setForm(null)} onSubmit={(v) => { close(); void newEpisode(season.id, v); }} />
        ) : (
          <>
            {season?.episodes.map((e) => <EpisodeRow key={e.id} e={e} on={e.id === projectId} pick={() => { open(e.id); close(); }} />)}
            <div className="menu-sep" />
            <button type="button" role="menuitem" onClick={() => setForm("episode")}>
              <span className="mi-ck"><Icon.plus /></span><span className="mi-t">Yeni bölüm…</span>
            </button>
            <button type="button" role="menuitem" className="danger" onClick={() => { close(); onRemove(); }}>
              <span className="mi-ck"><Icon.trash /></span><span className="mi-t">Bölümü sil…</span>
            </button>
          </>
        )}
      </Crumb>
      <LockChip />
    </nav>
  );
}

/** "Sürüm sabit · v004": Kabul kapısı onaylanmış, teslim edilmiş bölüm. */
function LockChip() {
  const { ov } = usePanel();
  const locked = ov?.project.episode?.locked;
  if (!locked) return null;
  return (
    <span className="chip lock-chip" data-tour="episode-lock"
      title="Bölüm teslim edildi: Kabul'deki film sürümüne ve o anki dizi varlığı sürümlerine sabit. Üretim, senaryo, stil ve bölüme özel varlıklar kilitli; kilidi yönetici açar.">
      <Icon.lock />{lockLabel(locked.label)}
    </span>
  );
}
