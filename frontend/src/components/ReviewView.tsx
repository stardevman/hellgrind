"use client";
/* Müşteri inceleme sayfası (/panel/review/?token=…): sade ve salt okunur. Oynatıcı, sürüm adı, zaman kodlu not alanı
   (oynatıcının o anki zamanı otomatik alınır), "Onayla" ve "Değişiklik iste". Bütçe, motor, maliyet ve iç ayrıntılar
   bu sayfada hiçbir zaman görünmez: belirteçli uç (/api/review/{token}) yalnızca bunları döndürür. */
import { useRef, useState, type FormEvent } from "react";
import { useSearchParams } from "next/navigation";
import useSWR from "swr";
import { api, ApiError, fetcher } from "@/lib/api";
import { timecode } from "@/lib/timeline";
import type { ReviewState } from "@/lib/types";

const DECIDED: Record<string, string> = {
  approved: "Bu sürümü onayladınız. Teşekkürler.",
  changes_requested: "Değişiklik istediniz; notlarınız ekibe iletildi. Yeni sürüm hazır olunca yeni bir bağlantı alacaksınız.",
  superseded: "Bu sürümün yerine daha yeni bir sürüm gönderildi. Bu bağlantıdan yalnızca izleyebilirsiniz; kararınızı yeni sürümün bağlantısından verin.",
};

export function ReviewView() {
  const token = useSearchParams().get("token") ?? "";
  const { data, error, mutate } = useSWR<ReviewState>(token ? `/review/${encodeURIComponent(token)}` : null, fetcher,
    { revalidateOnFocus: false, shouldRetryOnError: false });
  const video = useRef<HTMLVideoElement | null>(null);
  const [now, setNow] = useState(0);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; bad?: boolean } | null>(null);
  const [confirm, setConfirm] = useState<"approve" | "changes" | null>(null);
  const [why, setWhy] = useState("");

  if (!token) return <main className="review"><p className="review-msg">Bağlantıda inceleme belirteci yok.</p></main>;
  if (error) {
    const gone = (error as ApiError).status === 410;
    return <main className="review"><p className="review-msg">{gone ? "Bu inceleme bağlantısının süresi doldu ya da kapatıldı." : "İnceleme bağlantısı bulunamadı."}</p></main>;
  }
  if (!data) return <main className="review"><p className="review-msg">Yükleniyor…</p></main>;

  const run = async (fn: () => Promise<ReviewState>, ok: string) => {
    setBusy(true); setMsg(null);
    try { await mutate(await fn(), false); setMsg({ text: ok }); } catch (e) { setMsg({ text: (e as Error).message, bad: true }); } finally { setBusy(false); }
  };
  const addNote = (e: FormEvent) => {
    e.preventDefault();
    const at = Number(now.toFixed(2));
    if (!text.trim()) return;
    void run(() => api.post<ReviewState>(`/review/${encodeURIComponent(token)}/notes`, { at_s: at, text }), `Not ${timecode(at)} anına eklendi.`)
      .then(() => setText(""));
  };
  const decide = (decision: "approve" | "changes") => {
    setConfirm(null);
    void run(() => api.post<ReviewState>(`/review/${encodeURIComponent(token)}/decision`, { decision, note: why.trim() || null }),
      decision === "approve" ? "Onayınız kaydedildi." : "Değişiklik isteğiniz kaydedildi.");
  };
  const seek = (t: number) => { const v = video.current; if (v) { v.currentTime = t; void v.play().catch(() => {}); } };
  const open = data.status === "sent";
  return (
    <main className="review">
      <header className="review-h">
        <div><h1>{data.title}</h1><p className="faint">{data.gate_tr} · <b className="mono">{data.label}</b></p></div>
        <span className={`tag ${data.status === "approved" ? "ok" : data.status === "changes_requested" ? "bad" : ""}`}>
          {data.status === "approved" ? "Onaylandı" : data.status === "changes_requested" ? "Değişiklik istendi"
            : data.status === "superseded" ? "Yerini yenisi aldı" : "İncelemenizi bekliyor"}
        </span>
      </header>
      <section className="review-player">
        <video ref={video} src={data.video_url} controls preload="metadata" playsInline onTimeUpdate={(e) => setNow(e.currentTarget.currentTime)} />
        <div className="review-tc mono" aria-live="off">{timecode(now)}</div>
      </section>
      <div className="review-body">
        <section aria-labelledby="rv-notes">
          <h2 id="rv-notes">Notlar</h2>
          {open && (
            <form className="review-form" onSubmit={addNote}>
              <label className="field"><span>Not · <span className="mono">{timecode(now)}</span> anına eklenir</span>
                <textarea rows={3} maxLength={2000} value={text} onChange={(e) => setText(e.target.value)} placeholder="ör. Bu kesmede ışık daha sıcak olmalı" />
              </label>
              <button className="btn" type="submit" disabled={busy || !text.trim()}>Notu ekle</button>
            </form>
          )}
          <ol className="review-notes">
            {data.notes.length ? data.notes.map((n) => (
              <li key={n.id}>
                {n.at_s != null ? <button type="button" className="mono" onClick={() => seek(n.at_s!)} title="Bu ana git">{timecode(n.at_s)}</button> : <span className="mono faint">genel</span>}
                <span>{n.text}</span>
              </li>
            )) : <li className="faint">Henüz not yok.</li>}
          </ol>
        </section>
        <section aria-labelledby="rv-decision" className="review-decide">
          <h2 id="rv-decision">Karar</h2>
          {open ? (
            <>
              <label className="field">Kısa açıklama (isteğe bağlı)<textarea rows={2} maxLength={2000} value={why} onChange={(e) => setWhy(e.target.value)} /></label>
              <div className="row">
                <button className="btn primary" type="button" disabled={busy} onClick={() => setConfirm("approve")}>Onayla</button>
                <button className="btn" type="button" disabled={busy} onClick={() => setConfirm("changes")}>Değişiklik iste</button>
              </div>
            </>
          ) : <p>{DECIDED[data.status]}</p>}
          {msg && <div className={`note ${msg.bad ? "err" : "ok"}`} role="status">{msg.text}</div>}
          <p className="faint small">Bağlantı {new Date(data.expires_at * 1000).toLocaleDateString("tr-TR")} tarihine kadar geçerlidir.</p>
        </section>
      </div>
      {confirm && (
        <div className="modal-back" onMouseDown={(e) => { if (e.target === e.currentTarget) setConfirm(null); }}>
          <div className="modal" role="alertdialog" aria-modal="true" aria-labelledby="rv-c-t">
            <h2 id="rv-c-t">{confirm === "approve" ? `${data.label} onaylansın mı?` : `${data.label} için değişiklik istensin mi?`}</h2>
            <p className="faint small">{confirm === "approve" ? "Onay bu sürüm için kesindir." : "Notlarınız ekibe iletilir; yeni sürüm ayrı bir bağlantıyla gelir."}</p>
            <div className="row"><span className="grow" />
              <button type="button" className="btn ghost" onClick={() => setConfirm(null)}>Vazgeç</button>
              <button type="button" className="btn primary" onClick={() => decide(confirm)}>{confirm === "approve" ? "Onayla" : "Değişiklik iste"}</button>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}
