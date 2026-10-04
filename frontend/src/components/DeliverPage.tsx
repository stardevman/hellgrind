"use client";
/* Teslim sayfası (dışa aktarma sayfaları gibi): solda birleştirme ve dışa aktarma kuyruğu, ortada film ve kesme şeridi,
   sağda verimlilik raporu ve son kürasyon kararları. */
import { useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { api, fetcher } from "@/lib/api";
import { EMOTION_COLOR, KIND_TR, PURGE_TR } from "@/lib/i18n";
import { POLL_MS, usePanel } from "@/lib/panel";
import { pct, ratio, usd } from "@/lib/text";
import { timecode } from "@/lib/timeline";
import type { CurationItem, Metrics } from "@/lib/types";
import { Icon } from "./icons";
import { NoteBox, Panel, Section } from "./ui";

function Share({ x, label }: { x: number | null; label: string }) {
  const w = Math.max(0, Math.min(100, (x || 0) * 100));
  return (
    <div className="share" title={label}>
      <div className="bar" role="img" aria-label={label}><i style={{ width: `${w.toFixed(1)}%` }} /></div>
      <span>{pct(x)}</span>
    </div>
  );
}

function Kpi({ value, label, hint }: { value: string; label: string; hint?: string }) {
  return <div className="kpi"><b>{value}</b><span>{label}</span>{hint && <small>{hint}</small>}</div>;
}

const EXPORTS = [
  ["edl", "EDL (CMX3600)", "film.edl", "Kesme listesi: DaVinci Resolve, Premiere, Avid", "file"],
  ["otio", "OpenTimelineIO", "film.otio", "Zaman çizelgesi; renk sıçramaları işaretli", "list"],
  ["package", "Çekim paketi (zip)", "paket.zip", "Her çekim için final, rafine adaylar ve ayrı ses stem'leri", "zip"],
] as const;

export function DeliverPage() {
  const { ov, projectId, filmKind, setFilmKind, assembling, setAssembling, busy, act, note } = usePanel();
  const { data: m } = useSWR<Metrics>(projectId ? `/projects/${projectId}/metrics` : null, fetcher, { refreshInterval: POLL_MS });
  const { data: curation } = useSWR<CurationItem[]>(projectId ? `/projects/${projectId}/curation?limit=40` : null, fetcher, { refreshInterval: POLL_MS });
  const video = useRef<HTMLVideoElement | null>(null);
  const [now, setNow] = useState(0);
  const draft = filmKind === "draft";
  const film = (draft ? ov?.draft_timeline : ov?.timeline) ?? null;
  const waiting = Boolean(assembling && !(film && film.url !== assembling.before) && Date.now() - assembling.at < 90000);
  useEffect(() => { if (assembling && !waiting) setAssembling(null); }, [assembling, waiting, setAssembling]);
  if (!ov) return null;

  const done = draft ? ov.scenes.filter((s) => s.draft_url || s.status === "done").length : ov.scenes.filter((s) => s.status === "done").length;
  const clips = film?.cut?.clips ?? [];
  const bands = film?.cut?.sequences ?? [];
  const jumpsTo = new Set((ov.project.continuity?.pairs ?? []).filter((x) => x.jump).map((x) => x.to));
  const hasFilm = Boolean(ov.timeline);
  const length = clips.length ? clips[clips.length - 1].out_s : 0;
  const assemble = () => {
    const before = film?.url ?? null;
    void act(async () => {
      if (draft) await api.post(`/projects/${projectId}/assemble?kind=draft`);
      else await api.post(`/projects/${projectId}/assemble`);
      setAssembling({ before, at: Date.now() });
      note("deliver", draft ? "Kaba kurgu birleştirmeye alındı." : "Film birleştirmeye alındı.", "ok");
    }, "deliver");
  };
  const purgeTotal = m ? Object.values(m.purge_reasons).reduce((a, b) => a + b, 0) : 0;
  const minutes = m?.film_minutes ? `${ratio(Math.round(m.film_minutes * 600) / 10)} sn` : "—";

  return (
    <div className="page deliver">
      <Panel title="Teslim" meta={hasFilm ? `final ${timecode(ov.timeline?.cut?.clips?.at(-1)?.out_s ?? 0)}` : "film yok"}>
        <div className="pad stack">
          <div className="seg" role="group" aria-label="Film türü">
            <button type="button" aria-pressed={draft} onClick={() => setFilmKind("draft")}>Kaba kurgu</button>
            <button type="button" aria-pressed={!draft} onClick={() => setFilmKind("final")}>Final</button>
          </div>
          <button className="btn primary lg" type="button" disabled={done === 0 || waiting || busy} onClick={assemble}>
            {waiting ? "Birleştiriliyor…" : draft ? `Kaba kurguyu birleştir (${done} sahne)` : `Tamamlanan ${done} sahneyi birleştir`}
          </button>
          <NoteBox id="deliver" />
        </div>
        <div className="side-h">Profesyonel dışa aktarma</div>
        <ul className="queue">
          {EXPORTS.map(([f, label, name, about, ico]) => {
            const Ico = Icon[ico];
            return (
              <li key={f}>
                <span className="ico"><Ico /></span>
                <span><b>{label}</b><small>{about}</small></span>
                {hasFilm ? <a className="btn" href={`/api/projects/${projectId}/export?format=${f}`} download={name}>İndir</a>
                  : <button className="btn" type="button" disabled title="Önce filmi birleştir">İndir</button>}
              </li>
            );
          })}
          <li>
            <span className="ico"><Icon.board /></span>
            <span><b>Storyboard PDF</b><small>Sayfa başına 6 kare, sekans başlıklarıyla</small></span>
            <a className="btn" href={`/api/projects/${projectId}/storyboard.pdf`} download="storyboard.pdf">İndir</a>
          </li>
          <li>
            <span className="ico"><Icon.list /></span>
            <span><b>Kürasyon kaydı (JSONL)</b><small>Yönetmen kararları: kalite modeli eğitim verisi</small></span>
            <a className="btn" href={`/api/projects/${projectId}/curation.jsonl`} download>Kürasyon kaydını indir</a>
          </li>
        </ul>
        {!hasFilm && <p className="pad faint small">Profesyonel dışa aktarma birleştirilmiş filmin kesme listesinden yapılır; önce filmi birleştir.</p>}
      </Panel>

      <section className="pnl viewer">
        <header className="pnl-h"><h2>{draft ? "Kaba kurgu" : "Film"}</h2><span className="meta mono">{timecode(now)} / {timecode(length)}</span></header>
        <div className="screen">
          {film ? <video key={film.url} ref={video} src={film.url} controls preload="metadata" playsInline onTimeUpdate={(e) => setNow(e.currentTarget.currentTime)} />
            : <p className="none">Henüz film yok. Çekimler tamamlanınca birleştirin.</p>}
        </div>
        <div style={{ background: "var(--panel)", borderTop: "1px solid var(--line)" }}>
          <div className="cutbar" aria-label="Sekanslar" style={{ height: 16, marginBottom: 0 }}>
            {bands.map((b) => (
              <span key={b.code} className="band" style={{ position: "static", flex: `${Math.max(0.1, b.out_s - b.in_s)} 1 0` }} title={`${b.code} · ${b.title}`}>{b.code} · {b.title}</span>
            ))}
          </div>
          <div className="cutbar" aria-label="Kesme listesi">
            {clips.map((c) => (
              <button key={c.scene_code} type="button"
                className={`cut${jumpsTo.has(c.scene_code) ? " jump-in" : ""}${now >= c.in_s && now < c.out_s ? " active" : ""}`}
                style={{ flex: `${Math.max(0.1, c.out_s - c.in_s)} 1 0`, background: EMOTION_COLOR[c.emotion || ""] || "#9ca0a9" }}
                title={`${c.scene_code} · ${c.description || ""}`} aria-label={`${c.scene_code} sahnesine git`}
                onClick={() => { const v = video.current; if (v) { v.currentTime = c.in_s + 0.05; v.play().catch(() => {}); } }}>{c.scene_code}</button>
            ))}
          </div>
        </div>
      </section>

      <Panel title="Rapor" meta={m ? `${m.variations} varyasyon, ${m.accepted_shots} kabul edilen çekim` : ""}>
        {!m ? <p className="loading">Yükleniyor…</p> : (
          <>
            <div className="kpis">
              <Kpi value={ratio(m.variations_per_accepted_shot)} label="Kabul edilen çekim başına varyasyon" hint={`${m.variations} varyasyon / ${m.accepted_shots} çekim`} />
              <Kpi value={m.cost_per_film_minute_usd == null ? "—" : usd(m.cost_per_film_minute_usd)} label="Film dakikası başına sanal maliyet"
                hint={`${usd(m.spent_usd)} / ${minutes} ${m.film_minutes_source === "film" ? "film" : "tamamlanan çekim"}`} />
              <Kpi value={pct(m.probe.success_rate)} label="Test turu başarı oranı" hint={`${m.probe.succeeded} başarılı / ${m.probe.rounds} tur`} />
              <Kpi value={ratio(m.probe.mean_correction_rounds)} label="Ortalama düzeltme turu" hint={`${m.probe.scenes} çekimde`} />
              <Kpi value={pct(m.draft.upgrade_ratio)} label="Kaba kurgudan iyileştirilen" hint={`${m.draft.upgraded} / ${m.draft.drafted} taslak çekim`} />
            </div>
            <Section title="Sekanslar">
              {m.sequences.length ? (
                <table className="tbl">
                  <thead><tr><th>Sekans</th><th className="r">Var.</th><th className="r">Kabul</th><th>Varyasyon payı</th><th>Harcama payı</th></tr></thead>
                  <tbody>
                    {m.sequences.map((q) => {
                      const name = `${q.code} · ${q.title}`;
                      return (
                        <tr key={q.code}>
                          <td>{name}<small>{usd(q.spent_usd)} · {ratio(q.variations_per_accepted_shot)} var./çekim</small></td>
                          <td className="r mono">{q.variations}</td><td className="r mono">{q.accepted_shots}</td>
                          <td><Share x={q.variation_share} label={`${name}: varyasyon payı ${pct(q.variation_share)}`} /></td>
                          <td><Share x={q.spend_share} label={`${name}: harcama payı ${pct(q.spend_share)}`} /></td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              ) : <p className="faint">Sekans yok.</p>}
            </Section>
            <Section title="Eleme nedenleri" meta={String(purgeTotal)}>
              {purgeTotal ? Object.entries(m.purge_reasons).map(([r, n]) => {
                const label = PURGE_TR[r] || r;
                return <div key={r} className="stack" style={{ gap: 2 }}><span className="row"><span className="grow">{label}</span><span className="mono">{n}</span></span><Share x={n / purgeTotal} label={`${label}: ${n} varyasyon`} /></div>;
              }) : <p className="faint">Henüz elenen varyasyon yok.</p>}
            </Section>
            <Section title="Harcama kalemleri" open={false}>
              {Object.keys(m.spent_by_kind).length ? Object.entries(m.spent_by_kind).map(([k, v]) => (
                <span key={k} className="row"><span className="grow">{KIND_TR[k] || k}</span><span className="mono">{usd(v)}</span></span>
              )) : <p className="faint">Henüz harcama yok.</p>}
            </Section>
            <Section title="Son kararlar" meta={curation ? String(curation.length) : ""}>
              <ol className="ev">
                {curation?.length ? curation.map((e) => (
                  <li key={e.id}><time>{e.at ? new Date(e.at).toLocaleTimeString("tr-TR", { hour12: false }) : ""}</time>
                    <span>{e.action_tr} <span className="faint mono">{e.scene_code || ""}</span></span></li>
                )) : <li><span /><span className="faint">Henüz karar kaydı yok.</span></li>}
              </ol>
            </Section>
          </>
        )}
      </Panel>
    </div>
  );
}
