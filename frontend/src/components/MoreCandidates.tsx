"use client";

/* Tek adaylı üretim: hat her çekim için tek aday üretir. Skor düşükse (kalite kapısından kaldıysa ya da eşiğin altındaysa)
   burada yönetmene "yeni aday üreteyim mi?" diye sorulur; tahmini ek maliyet ve kalan bütçe onaydan önce gösterilir.
   Skor iyiyse soru gelmez ama yönetmen isterse yine yeni aday isteyebilir. */
import { useState } from "react";
import useSWR from "swr";
import { api, fetcher } from "@/lib/api";
import { usePanel } from "@/lib/panel";
import { stagesText, usdTr } from "@/lib/text";
import type { MoreCandidatesEstimate, Scene } from "@/lib/types";
import { Section } from "./ui";

interface MoreResult { stages: string[]; n: number; total_usd_estimate: number }

export function MoreCandidates({ sc }: { sc: Scene }) {
  const { busy, frozen, act, note } = usePanel();
  const offer = sc.more_candidates;
  const [open, setOpen] = useState(false);
  const [n, setN] = useState(1);
  const [hidden, setHidden] = useState(false);
  const asking = Boolean(offer?.ask) && !hidden;
  const expanded = asking || open;
  const { data: est, error } = useSWR<MoreCandidatesEstimate>(
    offer && expanded ? `/scenes/${sc.id}/candidates/estimate?n=${n}` : null, fetcher);
  if (!offer) return null;
  const go = () => act(async () => {
    const r = await api.post<MoreResult>(`/scenes/${sc.id}/candidates?n=${n}`);
    setOpen(false);
    note("inspector", `${sc.scene_code}: ${r.n} yeni aday üretiliyor (tahmini ek maliyet ~${usdTr(r.total_usd_estimate)}). `
      + `En yüksek skorlu aday kurguya girer; önceki aday silinmez. Ardından: ${stagesText(r.stages.filter((x) => x !== "VISUAL"))}.`, "ok");
  }, "inspector");
  const decline = () => act(async () => {
    await api.post(`/scenes/${sc.id}/candidates/decline`);
    setHidden(true);
    setOpen(false);
    note("inspector", `${sc.scene_code}: yeni aday üretilmeyecek; mevcut aday kalıyor.`, "ok");
  }, "inspector");
  const why = frozen ? "Proje durduruldu: önce üretime devam edin." : est?.over_budget ? "Kalan bütçe bu tahmine yetmiyor." : "";
  return (
    <Section title="Yeni aday" meta={offer.score != null ? `skor ${offer.score.toFixed(2)} · eşik ${offer.threshold.toFixed(2)}` : undefined}>
      {asking ? (
        <p className={offer.reason === "gate" ? "warn" : "caution"}><b>{offer.message}</b></p>
      ) : (
        <p className="faint small">Bu çekim için tek aday üretildi. Hat kendiliğinden yeni aday üretmez; isterseniz siz isteyebilirsiniz.</p>
      )}
      {!expanded ? (
        <div className="row">
          <button className="btn" type="button" disabled={busy || frozen} onClick={() => setOpen(true)}>Yeni aday iste…</button>
        </div>
      ) : (
        <div className="hint stack" role="group" aria-label="Yeni aday onayı">
          <label className="row small">Kaç yeni aday?
            <select value={n} aria-label="Yeni aday sayısı" disabled={busy} onChange={(e) => setN(Number(e.target.value))}>
              {Array.from({ length: offer.max }, (_, i) => i + 1).map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
          </label>
          {est ? (
            <>
              <dl className="kv">
                <dt>Ek maliyet</dt>
                <dd className="mono">~{usdTr(est.total_usd)}{est.n > 1 && <span className="faint"> ({est.n} × ~{usdTr(est.per_candidate_usd)})</span>}</dd>
                <dt>Kalan bütçe</dt><dd className="mono">{usdTr(est.remaining_usd)}</dd>
                <dt>Motor</dt><dd className="small">{est.engine_label ?? est.engine ?? "—"} · <span className="mono">{est.resolution}</span></dd>
              </dl>
              <p className="warn small">
                Uyarı: her yeni aday ayrıca ücretlendirilir{est.virtual ? " (ücretsiz modda bu tutar sanaldır; gerçek fiyatlarla bütçeden düşer)" : ""}.
                {est.shots.length > 1 && <> Bu çekim sahnenin parçasıyla birlikte üretilir: <span className="mono">{est.shots.join(", ")}</span>.</>}
              </p>
              {est.over_budget && <p className="warn small">Bütçe yetmiyor: tahmini {usdTr(est.total_usd)}, kalan {usdTr(est.remaining_usd)}.</p>}
            </>
          ) : <p className="faint small">{error ? "Tahmin alınamadı." : "Tahmin hesaplanıyor…"}</p>}
          <div className="row">
            <button className="btn good" type="button" disabled={busy || frozen || !est || est.over_budget} title={why || undefined} onClick={go}>
              Evet, {n} yeni aday üret{est ? ` (~${usdTr(est.total_usd)})` : ""}</button>
            {asking
              ? <button className="btn ghost" type="button" disabled={busy} onClick={decline}>Hayır, bu aday kalsın</button>
              : <button className="btn ghost" type="button" onClick={() => setOpen(false)}>Vazgeç</button>}
          </div>
        </div>
      )}
    </Section>
  );
}
