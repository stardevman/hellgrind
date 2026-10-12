"use client";
/* Bitirme: Seedance 2.5 taslağını 1080p'ye tamamlama ve upscale (klip, çekim ya da son montajdan önce bütün film).
   İkisi de tek adımlı onaydır: tahmin gösterilir, onay gösterilen tutarla gider (tahmin yükselmişse arka uç reddeder). */
import { useEffect, useState } from "react";
import useSWR from "swr";
import { api, fetcher } from "@/lib/api";
import { cheapestUpscale, completeResultText, draftState, promptLengthText, resolutionBadge, shortId, upscaleName, upscaleOptions, upscaleResultText } from "@/lib/finishing";
import { usePanel } from "@/lib/panel";
import { sec1, usdTr } from "@/lib/text";
import type { CompleteResult, DraftPreview, FalPlan, Scene, UpscalePlan, UpscaleScope } from "@/lib/types";
import { Section } from "./ui";

const FROZEN_TITLE = "Üretim durduruldu, önce Üretime devam et";

/** Kapsama göre upscale çağrısı (yollar açık yazılır: test_panel_next her yolu arka uçta arar). */
const upscaleCall = (scope: UpscaleScope, id: string, query: string) =>
  scope === "variation" ? api.post<UpscalePlan>(`/variations/${id}/upscale?${query}`)
    : scope === "scene" ? api.post<UpscalePlan>(`/scenes/${id}/upscale?${query}`)
      : api.post<UpscalePlan>(`/projects/${id}/upscale?${query}`);

/** Upscale onayı. Açılınca ve uç nokta değişince plan kuru çalıştırmayla okunur (fal.ai'ye gidilmez, kuyruğa bir şey
    verilmez); onay seçili uç nokta ve gösterilen toplamla gider. */
export function UpscaleConfirm({ scope, id, onClose, noteKey = "inspector" }: {
  scope: UpscaleScope; id: string; onClose: () => void; noteKey?: string;
}) {
  const { busy, frozen, act, note, meta } = usePanel();
  const fallback = meta?.video?.upscale_endpoint;
  const [endpoint, setEndpoint] = useState(fallback ?? "");
  const [plan, setPlan] = useState<UpscalePlan | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let live = true;
    setErr("");
    upscaleCall(scope, id, `dry_run=true${endpoint ? `&endpoint=${encodeURIComponent(endpoint)}` : ""}`)
      .then((r) => { if (live) setPlan(r); })
      .catch((e: Error) => { if (live) setErr(e.message); });
    return () => { live = false; };
  }, [scope, id, endpoint]);
  const chosen = endpoint || plan?.endpoint || "";
  const loading = !plan || (Boolean(endpoint) && plan.endpoint !== endpoint);
  const cheap = cheapestUpscale(plan?.alternatives);
  const options = upscaleOptions(meta?.video?.upscale_endpoints, plan?.alternatives, fallback ?? plan?.endpoint);
  const confirm = () => act(async () => {
    if (!plan) return;
    const r = await upscaleCall(scope, id, `endpoint=${encodeURIComponent(plan.endpoint)}&expected_usd=${plan.total_usd}`);
    onClose();
    note(noteKey, upscaleResultText(r), "ok");
  }, noteKey);
  const what = scope === "project" ? "Bütün filmin 1080p altındaki kazanan klipleri" : scope === "scene" ? "Çekimin kazanan klibi" : "Bu klip";
  return (
    <div className="hint stack" role="group" aria-label="Upscale onayı" aria-busy={loading}>
      <span>{what} 1080p'ye çıkarılır.</span>
      {err && <p className="bad small">{err}</p>}
      {!plan && !err && <span className="faint small">Tahmin hesaplanıyor…</span>}
      {plan && (
        <>
          <label className="field">Uç nokta
            <select value={chosen} disabled={busy} aria-label="Upscale uç noktası" onChange={(e) => setEndpoint(e.target.value)}>
              {options.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
            </select>
          </label>
          {cheap && cheap.endpoint !== chosen && plan.items.length > 0 && (
            <span className="small">En ucuz: {upscaleName(cheap.endpoint)} <span className="mono">~{usdTr(cheap.usd)}</span>{" "}
              <button className="btn ghost" type="button" disabled={busy} onClick={() => setEndpoint(cheap.endpoint)}>Bunu seç</button></span>
          )}
          {plan.items.length ? (
            <ul className="lines">
              {plan.items.map((x) => (
                <li key={x.variation_id}><span className="mono">~{usdTr(x.cost.usd)}</span> · <span className="mono">{x.scene_code}</span>
                  <span className="faint"> · {sec1(x.seconds)} sn, {x.from ?? "kaynak"} → 1080p</span></li>
              ))}
            </ul>
          ) : <p className="warn small">Upscale edilecek klip yok (kazanan klip yok ya da zaten 1080p).</p>}
          <dl className="kv">
            <dt>Toplam</dt><dd className="mono">~{usdTr(plan.total_usd)}{loading && <span className="faint small"> (güncelleniyor…)</span>}</dd>
            <dt>Kalan bütçe</dt><dd className="mono">{usdTr(plan.remaining_usd)}</dd>
            <dt>Hedef</dt><dd className="mono">{plan.target} · {typeof plan.fps === "number" ? `${plan.fps} kare/sn` : `fps ${plan.fps}`}</dd>
          </dl>
          {plan.warnings.map((w) => <p key={w} className="warn small">{w}</p>)}
          {!plan.budget_ok && <p className="warn small">Bütçe yetmiyor: tahmini {usdTr(plan.total_usd)}, kalan {usdTr(plan.remaining_usd)}.</p>}
          <span className="faint">Hedef fps kaynakla aynı (24); orijinal klip varyasyon olarak kalır; renk eşleme upscale'den sonra yeniden yapılır.
            {scope === "project" ? " Son çekimin montajı bitince film yeniden birleştirilir." : ""}</span>
          {plan.body && (
            <details>
              <summary className="small">İstek gövdesi (örnek)</summary>
              <pre className="prompt" style={{ maxHeight: 220, overflow: "auto", margin: "6px 0 0" }}>{JSON.stringify(plan.body, null, 2)}</pre>
            </details>
          )}
        </>
      )}
      <div className="row">
        <button className="btn good" type="button" disabled={busy || frozen || loading || !plan?.items.length || !plan?.budget_ok}
          title={frozen ? FROZEN_TITLE : undefined} onClick={confirm}>Onayla ve upscale et{plan ? ` (~${usdTr(plan.total_usd)})` : ""}</button>
        <button className="btn ghost" type="button" onClick={onClose}>Vazgeç</button>
      </div>
    </div>
  );
}

/** Seedance 2.5 taslağı: kimlik, kalan süre (7 gün) ve "Taslağı 1080p'ye tamamla" onayı; yanında daha ucuz upscale. */
function DraftBox({ sc, onUpscale }: { sc: Scene; onUpscale: () => void }) {
  const { busy, frozen, act, note, setVar } = usePanel();
  const [open, setOpen] = useState(false);
  const { data: pv } = useSWR<DraftPreview>(`/scenes/${sc.id}/draft`, fetcher, { revalidateOnFocus: false, keepPreviousData: true });
  const d = sc.seedance_draft!;
  const st = draftState(d);
  const parts = pv?.part?.length ? pv.part : d.part?.length ? d.part : [sc.scene_code];
  const why = pv && !pv.available ? pv.reason || "Tamamlama şu an uygun değil." : "";
  const confirm = () => act(async () => {
    if (!pv?.cost) return;
    const r = await api.post<CompleteResult>(`/scenes/${sc.id}/seedance/complete?expected_usd=${pv.cost.usd}`);
    setOpen(false);
    setVar(null);
    note("inspector", completeResultText(r), "ok");
  }, "inspector");
  const cheaper = pv?.cheaper && !d.completed_at ? (
    <button className="btn" type="button" disabled={busy || frozen} title={frozen ? FROZEN_TITLE : "Upscale onayını açar (uç nokta, tahmin ve kalan bütçe)"}
      onClick={() => { setOpen(false); onUpscale(); }}>{pv.cheaper.label}</button>
  ) : null;
  return (
    <>
      <h4 className="small dim">Seedance taslağı</h4>
      <dl className="kv">
        <dt>Taslak</dt><dd><span className="mono" title={d.draft_id}>{shortId(d.draft_id)}</span>{d.local && <span className="faint small"> (yerel)</span>}</dd>
        <dt>Geçerlilik</dt><dd className={st.tone ? `${st.tone} small` : "small"}>{st.text}</dd>
        <dt>Parça</dt><dd className="mono small">{parts.join(" + ")}</dd>
      </dl>
      {!d.completed_at && (
        <div className="row">
          <button className="btn good" type="button" aria-expanded={open} disabled={busy || frozen || !pv?.available || !pv.cost}
            title={frozen ? FROZEN_TITLE : why || "Tahmin, ayrılan tutar (%20 pay), kalan bütçe ve parça gösterilir; tamamlama onaydan sonra başlar"}
            onClick={() => setOpen(!open)}>Taslağı 1080p'ye tamamla</button>
          <span className="faint small">{pv?.label ? <>tahmini <span className="mono">{pv.label}</span></> : pv ? "" : "tahmin hesaplanıyor…"}</span>
        </div>
      )}
      {why && !d.completed_at && <p className={d.expired ? "bad small" : "warn small"}>{why}</p>}
      {!open && why && cheaper && <div className="row">{cheaper}</div>}
      {open && pv?.available && pv.cost && (
        <div className="hint stack" role="group" aria-label="Taslağı 1080p'ye tamamlama onayı">
          <span>{parts.length > 1 ? <>Parçanın tamamı tamamlanır ({parts.length} çekim, tek istek): <span className="mono">{parts.join(", ")}</span></>
            : <>Yalnızca <span className="mono">{sc.scene_code}</span> tamamlanır.</>}</span>
          <dl className="kv">
            <dt>Tahmin</dt><dd><span className="mono">{pv.label ?? `≈ ${usdTr(pv.cost.usd)}`}</span>
              <span className="faint small"> · {sec1(pv.seconds ?? d.duration_s)} sn, {Math.round(pv.cost.tokens).toLocaleString("tr-TR")} token</span></dd>
            <dt>Ayrılan</dt><dd><span className="mono">~{usdTr(pv.cost.reserve_usd)}</span><span className="faint small"> (%20 pay; bütçe kapısı bunu ayırır)</span></dd>
            <dt>Kalan bütçe</dt><dd className="mono">{usdTr(pv.remaining_usd)}</dd>
          </dl>
          <span className="faint">Taslak 7 gün içinde yalnızca 1080p'ye tamamlanır; taslak kesimleri varyasyon olarak kalır.</span>
          <div className="row">
            <button className="btn good" type="button" disabled={busy || frozen} onClick={confirm}>Onayla ve tamamla ({pv.label ?? `≈ ${usdTr(pv.cost.usd)}`})</button>
            {cheaper && <span className="faint small">ya da</span>}
            {cheaper}
            <button className="btn ghost" type="button" onClick={() => setOpen(false)}>Vazgeç</button>
          </div>
        </div>
      )}
    </>
  );
}

/** Denetçinin bitirme bölümü: Seedance taslağı (varsa) ve çekimin kazanan klibinin upscale'i. */
export function Finishing({ sc }: { sc: Scene }) {
  const { busy, frozen } = usePanel();
  const [up, setUp] = useState(false);
  const winner = sc.variations.find((v) => v.rank === 1);
  if (!winner && !sc.seedance_draft) return null;
  const badge = winner ? resolutionBadge(winner) : null;
  const pending = Boolean(sc.seedance_draft && !sc.seedance_draft.completed_at && !sc.seedance_draft.expired);
  return (
    <Section title="Bitirme (1080p)" meta={badge ? badge.long : winner?.resolution ?? (sc.seedance_draft ? "taslak" : "")} open={pending}>
      {sc.seedance_draft && <DraftBox sc={sc} onUpscale={() => setUp(true)} />}
      {winner && (badge
        ? <p className="small"><span className="tag ok" title={badge.title}>{badge.long}</span> <span className="faint">kazanan klip zaten 1080p.</span></p>
        : (
          <div className="row">
            <button className="btn" type="button" aria-expanded={up} disabled={busy || frozen} title={frozen ? FROZEN_TITLE : "Uç nokta, klip başına tahmin, toplam ve kalan bütçe gösterilir"}
              onClick={() => setUp(!up)}>Upscale (1080p)</button>
            <span className="faint small">kazanan klip{winner.resolution ? ` (${winner.resolution})` : ""} → 1080p</span>
          </div>
        ))}
      {up && <UpscaleConfirm scope="scene" id={sc.id} onClose={() => setUp(false)} />}
    </Section>
  );
}

/** Planın bitirme tahminleri (dl içinde): istem uzunluğu, upscale (varsayılan uç nokta ve en ucuzu) ve Seedance taslağını
    1080p'ye tamamlama. */
export function PlanFinishing({ plan }: { plan: FalPlan }) {
  const { meta } = usePanel();
  const len = promptLengthText(plan.prompt_chars, plan.prompt_limit);
  const ups = plan.upscale_estimate ?? {};
  const def = meta?.video?.upscale_endpoint && ups[meta.video.upscale_endpoint] ? meta.video.upscale_endpoint : Object.keys(ups)[0];
  const d = def ? ups[def] : undefined;
  const cheap = cheapestUpscale(Object.fromEntries(Object.entries(ups).map(([k, v]) => [k, v.usd])));
  const done = plan.complete_estimate;
  return (
    <>
      {len && <><dt>İstem</dt><dd className={len.over ? "warn small" : "small"}>{len.text.replace(/^İstem: /, "")}{len.over ? " (sınırı aşıyor)" : ""}</dd></>}
      {plan.draft && <><dt>Taslak</dt><dd className="small">Seedance 2.5 taslağı: 480p önizleme, 7 gün içinde 1080p'ye tamamlanabilir</dd></>}
      {d && (
        <>
          <dt>Upscale</dt>
          <dd className="small"><span className="mono">~{usdTr(d.usd)}</span> {upscaleName(d.endpoint)}
            {cheap && cheap.endpoint !== d.endpoint && <span className="faint"> · en ucuz {upscaleName(cheap.endpoint)} <span className="mono">~{usdTr(cheap.usd)}</span></span>}
            {d.warning && <div className="warn">{d.warning}</div>}</dd>
        </>
      )}
      {done && <><dt>Tamamlama</dt><dd className="small">1080p'ye tamamlama <span className="mono">≈ {usdTr(done.usd)}</span>
        <span className="faint"> (ayrılan ~{usdTr(done.reserve_usd)}, %20 pay)</span></dd></>}
    </>
  );
}
