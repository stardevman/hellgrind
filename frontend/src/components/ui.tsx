"use client";
/* Ortak küçük parçalar. */
import { useState, type FormEvent, type ReactNode } from "react";
import { STATUS_TR, WORKING } from "@/lib/i18n";
import { usePanel } from "@/lib/panel";

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

export function StatusPill({ status }: { status: string }) {
  return <span className={`status ${WORKING.has(status) ? "work" : status}`}>{STATUS_TR[status] || status}</span>;
}

/** Video: aynı adres kaldıkça öğe yeniden oluşturulmaz (2 saniyelik yoklama oynatmayı bozmasın). */
export function Video({ src, poster, empty, onTime, videoRef }: {
  src: string | null | undefined; poster?: string | null; empty: string;
  onTime?: (t: number) => void; videoRef?: (el: HTMLVideoElement | null) => void;
}) {
  return (
    <div className="media">
      {src
        ? <video key={src} src={src} poster={poster || undefined} controls preload="metadata" playsInline ref={videoRef}
            onTimeUpdate={onTime ? (e) => onTime(e.currentTarget.currentTime) : undefined} />
        : <p className="none">{empty}</p>}
    </div>
  );
}

/**
 * Sunucudan gelen değerle başlayan, kullanıcı yazarken yoklamanın ezmediği yerel durum. `sig` (ör. sahne kimliği +
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
      <label>{label}<input type="file" name="file" accept={accept} required /></label>
      {children}
      <button className="btn small" type="submit" disabled={busy || disabled}>{button}</button>
    </form>
  );
}

export const reducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
