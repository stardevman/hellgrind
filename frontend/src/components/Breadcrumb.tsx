"use client";
/* Alt banttaki kırıntı yolu: Dizi › Sezon 1 › Bölüm 3 (docs/dizi-hiyerarsisi.md). Bölüm bugünkü projedir; URL'de yalnızca
   ?p=<bölüm> kalır, dizi ve sezon bölümden türetilir. Bağımsız film tek parça görünür. İlk parça (dizi ya da film adı)
   Proje yöneticisini açar; sezon ve bölüm parçaları kendi menülerini yukarı açar (sezonlar, bölümler, "Yeni …" formları,
   silme). Teslim edilmiş bölümün yanında "Sürüm sabit · v004" rozeti durur. */
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { PROJECT_STATUS_TR } from "@/lib/i18n";
import { usePanel } from "@/lib/panel";
import { episodeLabel, lockLabel } from "@/lib/series";
import { useSeriesActions } from "@/lib/seriesActions";
import type { EpisodeItem } from "@/lib/types";
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

export function Breadcrumb({ onOpenManager }: { onOpenManager: () => void }) {
  const { projects, projectId, selectProject, ov } = usePanel();
  const { tree, current, newEpisode, newSeason, remove } = useSeriesActions();
  const [form, setForm] = useState<"episode" | null>(null);
  const ep = ov?.project.episode;
  const series = ep?.series ? tree?.find((x) => x.id === ep.series!.id) : undefined;
  const season = series?.seasons.find((z) => z.id === ep?.season?.id);
  const open = (id: string) => { selectProject(id); setForm(null); };
  const status = current ? (current.locked ? "Sürüm sabit" : PROJECT_STATUS_TR[current.display_status ?? current.status] || current.status) : undefined;
  /** İlk parça: dizi ya da film adı; tıklanınca Proje yöneticisi açılır. */
  const root = (label: string, sub?: string) => (
    <div className="switcher crumb" data-tour="project">
      <button type="button" className="switch-btn" aria-haspopup="dialog" aria-label="Proje yöneticisi" onClick={onOpenManager}
        title="Proje yöneticisi: diziler, bölümler, bağımsız filmler; yeni proje">
        <span className="sw-title">{label}</span>
        {sub && <span className="sw-status">{sub}</span>}
        <Icon.chevron />
      </button>
    </div>
  );

  if (!ep?.series || !ep.season) {
    return (
      <nav className="crumbs" aria-label="Proje">
        {root(current ? current.title : projects?.length ? "Proje seç" : "Henüz proje yok", status)}
        <LockChip />
      </nav>
    );
  }
  return (
    <nav className="crumbs" aria-label="Dizi, sezon ve bölüm">
      {root(ep.series.title)}
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
              <button type="button" role="menuitem" onClick={() => { close(); void newSeason(series.id); }}>
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
            <button type="button" role="menuitem" className="danger" onClick={() => { close(); void remove(); }}>
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
