"use client";
import useSWR from "swr";
import { fetcher } from "@/lib/api";
import { KIND_TR, PURGE_TR } from "@/lib/i18n";
import { POLL_MS, usePanel } from "@/lib/panel";
import { pct, ratio, usd } from "@/lib/text";
import type { CurationItem, Metrics } from "@/lib/types";

function Share({ x, label }: { x: number | null; label: string }) {
  const w = Math.max(0, Math.min(100, (x || 0) * 100));
  return (
    <div className="share" title={label}>
      <div className="track" role="img" aria-label={label}><i style={{ width: `${w.toFixed(1)}%` }} /></div>
      <span className="num">{pct(x)}</span>
    </div>
  );
}

function Fig({ value, label, hint }: { value: string; label: string; hint?: string }) {
  return <div className="fig"><b className="num">{value}</b><span>{label}</span>{hint && <small>{hint}</small>}</div>;
}

const EXPORTS = [["edl", "EDL (CMX3600)", "film.edl"], ["otio", "OpenTimelineIO", "film.otio"], ["package", "Çekim paketi (zip)", "paket.zip"]] as const;

export function EfficiencyPane() {
  const { ov, projectId } = usePanel();
  const { data: m } = useSWR<Metrics>(projectId ? `/projects/${projectId}/metrics` : null, fetcher, { refreshInterval: POLL_MS });
  const { data: curation } = useSWR<CurationItem[]>(projectId ? `/projects/${projectId}/curation?limit=40` : null, fetcher, { refreshInterval: POLL_MS });
  const hasFilm = Boolean(ov?.timeline);
  const purgeTotal = m ? Object.values(m.purge_reasons).reduce((a, b) => a + b, 0) : 0;
  const minutes = m?.film_minutes ? `${ratio(Math.round(m.film_minutes * 600) / 10)} sn` : "—";

  return (
    <>
      <section className="panel" aria-labelledby="eff-title">
        <header><h2 id="eff-title">Verimlilik</h2>{m && <span className="muted">{m.variations} varyasyon, {m.accepted_shots} kabul edilen çekim</span>}</header>
        {!m ? <p className="loading">Yükleniyor…</p> : (
          <div className="body eff-body">
            <div className="eff-figures">
              <Fig value={ratio(m.variations_per_accepted_shot)} label="Kabul edilen çekim başına varyasyon" hint={`${m.variations} varyasyon / ${m.accepted_shots} çekim`} />
              <Fig value={m.cost_per_film_minute_usd == null ? "—" : usd(m.cost_per_film_minute_usd)} label="Film dakikası başına sanal maliyet"
                hint={`${usd(m.spent_usd)} / ${minutes} ${m.film_minutes_source === "film" ? "film" : "tamamlanan çekim"}`} />
              <Fig value={pct(m.probe.success_rate)} label="Test turu başarı oranı" hint={`${m.probe.succeeded} başarılı / ${m.probe.rounds} tur`} />
              <Fig value={ratio(m.probe.mean_correction_rounds)} label="Ortalama düzeltme turu" hint={`${m.probe.scenes} çekimde`} />
              <Fig value={pct(m.draft.upgrade_ratio)} label="Kaba kurgudan iyileştirilen" hint={`${m.draft.upgraded} / ${m.draft.drafted} taslak çekim`} />
            </div>
            <div className="eff-grid">
              <div>
                <h3>Sekanslar</h3>
                <div className="table-wrap">
                  <table className="eff-table">
                    {m.sequences.length ? (
                      <>
                        <thead><tr><th>Sekans</th><th className="r">Varyasyon</th><th className="r">Kabul</th><th className="r">Varyasyon/çekim</th>
                          <th className="r">Harcama</th><th>Varyasyon payı</th><th>Harcama payı</th></tr></thead>
                        <tbody>
                          {m.sequences.map((q) => {
                            const name = `${q.code} · ${q.title}`;
                            return (
                              <tr key={q.code}>
                                <td>{name}</td><td className="r num">{q.variations}</td><td className="r num">{q.accepted_shots}</td>
                                <td className="r num">{ratio(q.variations_per_accepted_shot)}</td><td className="r num">{usd(q.spent_usd)}</td>
                                <td><Share x={q.variation_share} label={`${name}: varyasyon payı ${pct(q.variation_share)}`} /></td>
                                <td><Share x={q.spend_share} label={`${name}: harcama payı ${pct(q.spend_share)}`} /></td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </>
                    ) : <tbody><tr><td className="muted">Sekans yok.</td></tr></tbody>}
                  </table>
                </div>
              </div>
              <div>
                <h3>Eleme nedenleri</h3>
                <ul className="eff-list">
                  {purgeTotal ? Object.entries(m.purge_reasons).map(([r, n]) => {
                    const label = PURGE_TR[r] || r;
                    return <li key={r}><span>{label}</span><span className="num">{n}</span><Share x={n / purgeTotal} label={`${label}: ${n} varyasyon`} /></li>;
                  }) : <li className="muted">Henüz elenen varyasyon yok.</li>}
                </ul>
                <h3 className="mt16">Harcama kalemleri</h3>
                <ul className="eff-list">
                  {Object.keys(m.spent_by_kind).length ? Object.entries(m.spent_by_kind).map(([k, v]) => (
                    <li key={k}><span>{KIND_TR[k] || k}</span><span className="num">{usd(v)}</span></li>
                  )) : <li className="muted">Henüz harcama yok.</li>}
                </ul>
              </div>
            </div>
          </div>
        )}
      </section>
      <div className="style-grid mtgap">
        <section className="panel" aria-labelledby="curation-title">
          <header>
            <h2 id="curation-title">Son kararlar</h2><span className="spacer" />
            <a className="btn small" href={`/api/projects/${projectId}/curation.jsonl`} download>Kürasyon kaydını indir (JSONL)</a>
          </header>
          <ol className="curation">
            {curation?.length ? curation.map((e) => (
              <li key={e.id}>
                <time>{e.at ? new Date(e.at).toLocaleTimeString("tr-TR", { hour12: false }) : ""}</time>
                <span>{e.action_tr}</span><span className="muted">{e.scene_code || ""}</span>
              </li>
            )) : <li><span /><span className="muted">Henüz karar kaydı yok.</span><span /></li>}
          </ol>
        </section>
        <section className="panel" aria-labelledby="export-title">
          <header><h2 id="export-title">Profesyonel dışa aktarma</h2></header>
          <div className="body">
            <p className="muted">
              {hasFilm
                ? "Kesme listesi DaVinci Resolve ya da Premiere'e aktarılabilir. Paket, her çekim için final videoyu, rafine adayları ve ayrı ses stem'lerini içerir."
                : "Dışa aktarma birleştirilmiş filmin kesme listesinden yapılır. Önce Sahneler sekmesinden filmi birleştir."}
            </p>
            {hasFilm && (
              <div className="actions-row mt12">
                {EXPORTS.map(([f, label, name]) => (
                  <a key={f} className="btn small" href={`/api/projects/${projectId}/export?format=${f}`} download={name}>{label}</a>
                ))}
              </div>
            )}
          </div>
        </section>
      </div>
    </>
  );
}
