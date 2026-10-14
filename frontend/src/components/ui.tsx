"use client";
/* Ortak küçük parçalar: panel, akordeon bölümü, durum noktası, not, yükleme formu, senkron yerel durum. */
import { useEffect, useState, type FormEvent, type ReactNode, type RefObject } from "react";
import { STATUS_TR, WORKING } from "@/lib/i18n";
import { usePanel } from "@/lib/panel";

/** Açılır pencere (menü, bütçe, yapılacaklar): dışarı tıklama ve Esc kapatır. */
export function useDismiss(open: boolean, close: () => void, box: RefObject<HTMLElement | null>) {
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) close(); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc); };
  }, [open, close, box]);
}

/** Başlıklı panel (kurgu yazılımlarındaki yerleşik pencereler gibi). */
export function Panel({ title, meta, tools, children, className = "", bodyClass = "", id, tour }: {
  title: ReactNode; meta?: ReactNode; tools?: ReactNode; children: ReactNode; className?: string; bodyClass?: string; id?: string;
  /** Rehber turunun hedef adı (data-tour). */
  tour?: string;
}) {
  return (
    <section className={`pnl ${className}`.trim()} aria-labelledby={id} data-tour={tour}>
      <header className="pnl-h">
        <h2 id={id}>{title}</h2>
        {meta != null && meta !== "" && <span className="meta">{meta}</span>}
        <span className="grow" />
        {tools}
      </header>
      <div className={`pnl-b ${bodyClass}`.trim()}>{children}</div>
    </section>
  );
}

/** Açılır-kapanır denetçi bölümü. */
export function Section({ title, meta, open = true, children }: { title: string; meta?: ReactNode; open?: boolean; children: ReactNode }) {
  return (
    <details className="sec" open={open}>
      <summary>{title}{meta != null && meta !== "" && <span className="meta">{meta}</span>}</summary>
      <div className="sec-b">{children}</div>
    </details>
  );
}

export function statusTone(status: string): string {
  if (WORKING.has(status)) return "work";
  if (status === "done") return "ok";
  if (status === "refined") return "info";
  if (status === "board_review" || status === "plan_review" || status === "pending" || status === "parsed") return "warn";
  if (["failed", "needs_review", "frozen", "blocked"].includes(status)) return "bad";
  return "";
}

export function StatusDot({ status }: { status: string }) {
  return <span className={`st ${statusTone(status)}`}>{STATUS_TR[status] || status}</span>;
}

/** Her eylemden sonra yazılan not (hangi aşamaların çalışacağı ya da hata). */
export function NoteBox({ id, className = "" }: { id: string; className?: string }) {
  const { notes } = usePanel();
  const n = notes[id];
  return (
    <div className={`note ${n?.kind || ""} ${className}`.trim()} role="status" aria-live="polite">
      {n?.text || ""}
    </div>
  );
}

/**
 * Sunucudan gelen değerle başlayan, kullanıcı yazarken yoklamanın ezmediği yerel durum. `sig` (ör. çekim kimliği +
 * sunucudaki değer) değişince yerel değer sunucudakine döner.
 */
export function useSynced<T>(server: T, sig: string): [T, (v: T) => void] {
  const [state, setState] = useState({ sig, value: server });
  if (state.sig !== sig) {
    setState({ sig, value: server });
    return [server, (v) => setState({ sig, value: v })];
  }
  return [state.value, (v) => setState({ sig, value: v })];
}

/** Ham gövdeyle tek dosya yükleyen form: dosya seçilmeden gönderilmez, başarılı olunca temizlenir. Ek alanlar
    (children) denetimsiz girdilerdir ve adlarıyla `fields` üzerinden okunur. */
export function UploadForm({ label, accept, button, onUpload, children, className = "form-row", disabled }: {
  label: string; accept: string; button: string; className?: string; disabled?: boolean;
  onUpload: (file: File, fields: (name: string) => string) => Promise<boolean>; children?: ReactNode;
}) {
  const { busy } = usePanel();
  const submit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const form = e.currentTarget;
    const file = (form.elements.namedItem("file") as HTMLInputElement).files?.[0];
    if (!file) return;
    const field = (name: string) => (form.elements.namedItem(name) as HTMLInputElement | null)?.value ?? "";
    void onUpload(file, field).then((ok) => { if (ok) form.reset(); });
  };
  return (
    <form className={className} onSubmit={submit}>
      <label className="field">{label}<input type="file" name="file" accept={accept} required /></label>
      {children}
      <button className="btn" type="submit" disabled={busy || disabled}>{button}</button>
    </form>
  );
}

export const reducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Klavye kısayolu bir yazı alanında basılmadıysa true (kısayollar form doldururken tetiklenmesin). */
export const shortcutTarget = (e: KeyboardEvent) =>
  !(e.target as HTMLElement).closest("input, select, textarea, [contenteditable]") && !e.metaKey && !e.ctrlKey && !e.altKey;
