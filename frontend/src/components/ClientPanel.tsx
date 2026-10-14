"use client";
/* Teslim › Müşteri: onay kapıları (durum, revizyon turu ve turların sanal maliyeti), müşteriye gönderilen sürümler
   (v001, v002… ve hangisinin onaylandığı) ve süreli inceleme bağlantıları. Video kapıları bağlantıyla, senaryo ve tasarım
   kapıları (müşterinin başka kanaldan gelen kararı) buradan işaretlenir. Her karar kürasyon kaydına düşer. */
import { useState } from "react";
import useSWR from "swr";
import { api, fetcher } from "@/lib/api";
import { GATE_STATUS_TR, roundText, VIDEO_GATES, type Gate } from "@/lib/client";
import { POLL_MS, usePanel } from "@/lib/panel";
import { clock, usd } from "@/lib/text";
import type { ClientState, FilmVersionRow, ReviewLinkRow } from "@/lib/types";
import { Icon } from "./icons";
import { NoteBox, Section } from "./ui";

const TONE: Record<string, string> = { approved: "ok", changes_requested: "bad", pending: "" };
const KIND_FILM: Record<string, string> = { animatic: "animatik", draft: "kaba kurgu", final: "final film", acceptance: "final film" };

function LinkRow({ link }: { link: ReviewLinkRow }) {
  const { busy, act, note } = usePanel();
  const full = typeof window === "undefined" ? link.url : `${window.location.origin}${link.url}`;
  const live = !link.revoked && !link.expired;
  return (
    <li className="link-row">
      <span className="mono">{link.label}</span>
      <input className="mono small" readOnly value={full} aria-label={`${link.label} inceleme bağlantısı`} onFocus={(e) => e.currentTarget.select()} />
      <span className={`tag ${live ? "info" : ""}`}>{link.revoked ? "kapatıldı" : link.expired ? "süresi doldu" : `${new Date(link.expires_at * 1000).toLocaleDateString("tr-TR")} tarihine kadar`}</span>
      {live && <button type="button" className="btn ghost icon-only" title="Bağlantıyı kopyala" aria-label="Bağlantıyı kopyala"
        onClick={() => void navigator.clipboard?.writeText(full).then(() => note("client", `${link.label} bağlantısı kopyalandı.`, "ok"))}><Icon.copy /></button>}
      {live && <button type="button" className="btn ghost" disabled={busy} onClick={() => act(async () => {
        await api.del(`/review-links/${link.id}`);
        note("client", `${link.label} bağlantısı kapatıldı; müşteri artık açamaz.`, "ok");
      }, "client")}>Kapat</button>}
    </li>
  );
}

export function ClientPanel() {
  const { ov, projectId, busy, act, note } = usePanel();
  const { data: st, mutate } = useSWR<ClientState>(projectId ? `/projects/${projectId}/client` : null, fetcher, { refreshInterval: POLL_MS * 2 });
  const [days, setDays] = useState("7");
  if (!st || !ov) return <p className="loading">Yükleniyor…</p>;
  const filmOf = (g: Gate) => (g === "animatic" ? ov.animatic : g === "draft" ? ov.draft_timeline : ov.timeline);
  const sendAndLink = (g: Gate) => act(async () => {
    const latest = st.versions.filter((v) => v.gate === g).at(-1);
    const row = st.gates.find((x) => x.gate === g)!;
    // Aynı filmin sürümü zaten gönderilmiş ve karar beklemiyorsa yeni sürüm açılmaz; değişiklik istendiyse yeni tur.
    const reuse = latest && latest.status === "sent" && filmOf(g)?.url === latest.url ? latest : null;
    if (!reuse && row.rounds >= row.limit && latest?.status === "changes_requested"
      && !window.confirm(`${row.label}: sözleşmedeki ${row.limit} revizyon turu doldu. Yine de yeni sürüm gönderilsin mi?`)) return;
    const v = reuse ?? await api.post<FilmVersionRow>(`/projects/${projectId}/versions`, { gate: g });
    const link = await api.post<ReviewLinkRow>(`/projects/${projectId}/review-links`, { version_id: v.id, days: Number(days) });
    await mutate();
    const full = `${window.location.origin}${link.url}`;
    void navigator.clipboard?.writeText(full).catch(() => {});
    note("client", `${v.label} (${row.label}) için inceleme bağlantısı oluşturuldu ve kopyalandı: ${full}`, "ok");
  }, "client");
  const mark = (g: Gate, status: string) => act(async () => {
    await api.post(`/projects/${projectId}/gates/${g}`, { status });
    await mutate();
    note("client", `${st.gates.find((x) => x.gate === g)!.label}: ${GATE_STATUS_TR[status]}.`, status === "changes_requested" ? "err" : "ok");
  }, "client");
  const approved = st.versions.filter((v) => v.status === "approved");
  return (
    <div className="client">
      <div className="pad stack">
        <label className="field" style={{ maxWidth: 220 }}>Bağlantının süresi
          <select value={days} onChange={(e) => setDays(e.target.value)}>
            {[["1", "1 gün"], ["3", "3 gün"], ["7", "7 gün"], ["14", "14 gün"], ["30", "30 gün"]].map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
        </label>
        <NoteBox id="client" />
      </div>
      <ol className="gates">
        {st.gates.map((g) => {
          const film = VIDEO_GATES.includes(g.gate as Gate) ? filmOf(g.gate as Gate) : null;
          return (
            <li key={g.gate} className={`gate ${g.status}`}>
              <div className="row">
                <b>{g.label}</b>
                <span className={`tag ${TONE[g.status]}`}>{g.status === "pending" && g.versions.some((v) => v.status === "sent") ? "müşteride" : GATE_STATUS_TR[g.status]}</span>
                <span className={`small mono${g.over_limit ? " bad" : " faint"}`} title="Değişiklik isteği sayısı / sözleşmedeki tur sınırı">{roundText(g)}</span>
                <span className="grow" />
                {g.video ? (
                  <button type="button" className="btn" disabled={busy || !film} data-tour="review-link"
                    title={film ? `Şu anki ${KIND_FILM[g.gate]} sürüm olarak etiketlenir ve süreli bağlantı oluşturulur` : `Önce ${KIND_FILM[g.gate]} birleştirilmeli`}
                    onClick={() => void sendAndLink(g.gate as Gate)}>İnceleme bağlantısı oluştur</button>
                ) : (
                  <>
                    <button type="button" className="btn" disabled={busy} onClick={() => void mark(g.gate as Gate, "approved")}>Müşteri onayladı</button>
                    <button type="button" className="btn ghost" disabled={busy} onClick={() => void mark(g.gate as Gate, "changes_requested")}>Değişiklik istedi</button>
                  </>
                )}
              </div>
              {g.versions.length > 0 && (
                <ol className="rounds">
                  {g.versions.map((v, i) => (
                    <li key={v.id}><span className="mono">{v.label}</span><span className="faint small">tur {i + 1} · {v.created_at ? clock(v.created_at) : ""}</span>
                      <span className={`tag ${TONE[v.status] ?? "warn"}`}>{v.status === "sent" ? "müşteride" : GATE_STATUS_TR[v.status]}</span>
                      <span className="grow" /><span className="mono small" title="Önceki gönderimden bu yana yapılan harcama">{v.cost_usd != null ? usd(v.cost_usd) : ""}</span></li>
                  ))}
                </ol>
              )}
            </li>
          );
        })}
      </ol>
      <Section title="Sürümler" meta={approved.length ? `onaylı: ${approved.map((v) => v.label).join(", ")}` : `${st.versions.length}`}>
        {st.versions.length ? (
          <table className="tbl">
            <thead><tr><th>Sürüm</th><th>Kapı</th><th>Durum</th><th className="r">Tur maliyeti</th></tr></thead>
            <tbody>{st.versions.map((v) => (
              <tr key={v.id}><td className="mono"><a href={v.url} target="_blank" rel="noreferrer">{v.label}</a></td><td>{v.gate_tr}</td>
                <td><span className={`tag ${TONE[v.status] ?? "warn"}`}>{v.status === "sent" ? "müşteride" : GATE_STATUS_TR[v.status]}</span></td>
                <td className="r mono">{v.cost_usd != null ? usd(v.cost_usd) : "—"}</td></tr>
            ))}</tbody>
          </table>
        ) : <p className="faint small">Henüz müşteriye sürüm gönderilmedi.</p>}
      </Section>
      <Section title="İnceleme bağlantıları" meta={String(st.links.filter((x) => !x.revoked && !x.expired).length)} open={false}>
        {st.links.length ? <ul className="links">{st.links.map((x) => <LinkRow key={x.id} link={x} />)}</ul> : <p className="faint small">Bağlantı yok.</p>}
      </Section>
    </div>
  );
}
