"use client";
/* Servis geçmişi: ücretli servislere (ücretsiz modda sanal olarak) yapılan her çağrı. Solda özet (gerçek ve sanal
   harcama; servis, model, işlem, proje ve gün dağılımı), ortada süzgeçli çağrı listesi, sağda seçili çağrının ayrıntısı
   (model, sağlayıcının istek kimliği, parametreler, tutar, hata). Süzgeçler URL'de durur. */
import { useState, type ReactNode } from "react";
import useSWR from "swr";
import { fetcher } from "@/lib/api";
import { CALL_MODE_TR, CALL_STATUS_TR, STEP_TR, TIER_TR, TYPE_TR } from "@/lib/i18n";
import { POLL_MS, usePanel, type HistoryFilter } from "@/lib/panel";
import { callAmountText, stepValue, usdExact, whenText } from "@/lib/text";
import type { CallFigures, CallPage, CallSummary, ServiceCall } from "@/lib/types";
import { Icon } from "./icons";
import { Panel, Section } from "./ui";

const PAGE_SIZE = 100;
const MAX_ROWS = 500;
const SERVICES = [["runpod", "MiniMax H3 · RunPod"], ["vast", "MiniMax H3 · vast.ai"], ["fal", "fal.ai"], ["elevenlabs", "ElevenLabs"], ["anthropic", "Anthropic"]] as const;
const PERIODS = [["1", "Son 24 saat"], ["7", "Son 7 gün"], ["30", "Son 30 gün"]] as const;
const statusTone = (s: string) => (s === "succeeded" ? "ok" : s === "failed" ? "bad" : "work");

/** Süzgeçlerin sorgu dizgesi ("?" olmadan): liste, özet ve CSV aynı süzgeci kullanır. */
function filterQuery(f: HistoryFilter): string {
  const q = new URLSearchParams();
  const fields: [keyof HistoryFilter, string][] = [["project", "project_id"], ["service", "service"], ["operation", "operation"],
    ["mode", "mode"], ["status", "status"], ["days", "days"]];
  for (const [k, param] of fields) if (f[k]) q.set(param, f[k]!);
  return q.toString();
}

const fullTime = (iso: string) => new Date(iso).toLocaleString("tr-TR", { dateStyle: "medium", timeStyle: "medium" });

function Kpi({ value, label, hint }: { value: string; label: string; hint?: string }) {
  return <div className="kpi"><b>{value}</b><span>{label}</span>{hint && <small>{hint}</small>}</div>;
}

/** Dağılım tablosu: ad, çağrı sayısı, gerçek ve sanal tutar. Adı tıklanabilir satır o değere süzer. */
function Breakdown<T extends CallFigures>({ title, rows, name, pick, open = true }: {
  title: string; rows: T[] | undefined; name: (r: T) => ReactNode; pick?: (r: T) => void; open?: boolean;
}) {
  if (!rows?.length) return null;
  return (
    <Section title={title} meta={String(rows.length)} open={open}>
      <table className="tbl">
        <thead><tr><th>{title}</th><th className="r">Çağrı</th><th className="r">Gerçek</th><th className="r">Sanal</th></tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              <td>{pick ? <button type="button" className="text-btn" onClick={() => pick(r)} title="Bu değere süz">{name(r)}</button> : name(r)}
                {r.failed > 0 && <small>{r.failed} başarısız</small>}</td>
              <td className="r mono">{r.calls}</td>
              <td className="r mono">{usdExact(r.real_usd)}</td>
              <td className="r mono faint">{usdExact(r.virtual_usd)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}

function Summary({ sum, filtered }: { sum: CallSummary | undefined; filtered: boolean }) {
  const { setHistory } = usePanel();
  if (!sum) return <Panel title="Özet"><p className="loading">Yükleniyor…</p></Panel>;
  const t = sum.totals;
  const span = sum.first_at ? `${new Date(sum.first_at).toLocaleDateString("tr-TR")} – ${new Date(sum.last_at!).toLocaleDateString("tr-TR")}` : "";
  return (
    <Panel title="Özet" meta={filtered ? "süzgece göre" : span}>
      <div className="kpis" style={{ gridTemplateColumns: "1fr 1fr" }}>
        <Kpi value={usdExact(t.real_usd)} label="Gerçek harcama" hint={`${t.real_calls} çağrı`} />
        <Kpi value={usdExact(t.virtual_usd)} label="Sanal harcama" hint={`${t.virtual_calls} çağrı · para harcanmadı`} />
        <Kpi value={String(t.failed)} label="Başarısız çağrı" hint="tutarları bütçeye iade edildi" />
        <Kpi value={String(t.running)} label="Süren çağrı" hint={`${usdExact(t.reserved_usd)} ayrıldı`} />
      </div>
      {t.unknown_usd > 0 && (
        <p className="pad faint small">Modu bilinmeyen eski kayıtlar: <span className="mono">{usdExact(t.unknown_usd)}</span></p>
      )}
      <Breakdown title="Servis" rows={sum.by_service} name={(r) => r.name} pick={(r) => setHistory({ service: r.service, call: null })} />
      <Breakdown title="Model" rows={sum.by_model} name={(r) => <span className="mono small">{r.model ?? "—"}</span>} />
      <Breakdown title="İşlem" rows={sum.by_operation} name={(r) => r.title} pick={(r) => setHistory({ operation: r.operation, call: null })} />
      <Breakdown title="Proje" rows={sum.by_project} open={false}
        name={(r) => <>{r.title ?? "—"}{r.deleted && <span className="faint"> (silindi)</span>}</>}
        pick={(r) => r.project_id && setHistory({ project: r.project_id, call: null })} />
      <Breakdown title="Gün" rows={sum.by_day} open={false}
        name={(r) => new Date(`${r.day}T12:00:00`).toLocaleDateString("tr-TR", { day: "numeric", month: "short", year: "numeric" })} />
      <p className="pad faint small">
        Gerçek: ücretli modda sağlayıcıya giden çağrı. Sanal: ücretsiz modda aynı işin fiyat tablosundaki karşılığı; para harcanmaz.
      </p>
    </Panel>
  );
}

/** Çağrının nerede yapıldığı: proje (bütün projeler listelenirken), çekim ya da varlık, tur ve deneme. */
function whereText(c: ServiceCall, withProject: boolean): string {
  return [withProject ? c.project_title : null, c.subject,
    c.tier ? `${TIER_TR[c.tier] ?? c.tier} ${(c.wave_no ?? 0) + 1}. dalga` : null,
    c.attempt && c.attempt > 1 ? `${c.attempt}. deneme` : null].filter(Boolean).join(" · ");
}

function priceNote(c: ServiceCall): string {
  if (c.mode === "virtual") return "Ücretsiz mod: bu çağrı sağlayıcıya gitmedi. Tutar, ücretli modda harcanacak olanın fiyat tablosundaki karşılığıdır; model de ücretli modda çağrılacak olandır.";
  if (c.status === "failed") {
    return c.request_id
      ? "Rezervasyon bütçeye iade edildi. İstek sağlayıcının kuyruğuna girdi: hata sonuç alınırken olduysa sağlayıcı yine de ücretlendirmiş olabilir. İstek kimliğini sağlayıcının panelinde kontrol edin."
      : "Rezervasyon bütçeye iade edildi; istek sağlayıcıya ulaşmadı.";
  }
  if (c.service === "runpod") return "Tutar, RunPod'un bildirdiği çalışma süresi (ve worker'ın ilk işiyse soğuk başlatma) × GPU tipinin saniye ücretiyle hesaplandı. Boşta bekleyen worker'lar ayrı satırda; kesin tutar RunPod faturasındadır.";
  if (c.service === "vast") return "Tutar, sunucunun bildirdiği GPU süresi × fiyat tablosundaki saatlik ücretle hesaplandı. Makinenin boşta geçen süresi buraya yansımaz; kesin tutar vast.ai faturasındadır.";
  if (c.service === "fal") return "fal.ai yanıtı fatura tutarı döndürmez: işlenen tutar fiyat tablosundan hesaplanır. Kesin tutar için istek kimliğini fal.ai panelinde arayın.";
  if (c.service === "anthropic") return "Tutar, yanıttaki belirteç kullanımından fiyat tablosuyla hesaplandı.";
  if (c.service === "elevenlabs") return "Tutar, karakter sayısından fiyat tablosuyla hesaplandı; kesin tutar ElevenLabs hesabındadır.";
  return "";
}

function Detail({ c }: { c: ServiceCall }) {
  const { projectId, selectProject, selectScene, setPage, gotoAsset } = usePanel();
  const [copied, setCopied] = useState(false);
  const here = c.project_id != null && c.project_id === projectId;
  const amount = callAmountText(c);
  const copy = () => { void navigator.clipboard?.writeText(c.request_id ?? "").then(() => setCopied(true), () => {}); };
  const old = [c.model_inferred && "model etiketten tahmin edildi", c.mode_inferred && "mod ayar değişiklik kaydından çıkarıldı"].filter(Boolean);
  return (
    <div className="pad stack">
      <dl className="kv wide">
        <dt>Servis</dt>
        <dd>{c.service_name}{c.console && <> · <a href={c.console} target="_blank" rel="noopener noreferrer">hesap paneli ↗</a></>}</dd>
        <dt>Model</dt><dd className="mono">{c.model ?? "—"}{c.model_inferred && <span className="faint"> (tahmini)</span>}</dd>
        <dt>Mod</dt>
        <dd><span className={`tag ${c.mode === "real" ? "warn" : ""}`}>{CALL_MODE_TR[c.mode]}</span>
          {c.mode === "virtual" && <span className="faint"> para harcanmadı</span>}{c.mode_inferred && <span className="faint"> (tahmini)</span>}</dd>
        <dt>Durum</dt><dd><span className={`st ${statusTone(c.status)}`}>{CALL_STATUS_TR[c.status]}</span></dd>
        <dt>Tutar</dt><dd className="mono">{amount.value}{amount.note && <span className="faint"> · {amount.note}</span>}</dd>
        <dt>Tahmin</dt><dd className="mono">{usdExact(c.estimate_usd)}</dd>
        <dt>Proje</dt>
        <dd>{c.project_title ?? "—"}{c.project_id == null && c.project_title && <span className="faint"> (silindi)</span>}
          {c.project_id && !here && <> · <button type="button" className="text-btn" onClick={() => selectProject(c.project_id)}>projeyi aç</button></>}</dd>
        {c.subject && (
          <>
            <dt>{c.asset_id ? (TYPE_TR[c.asset_type ?? ""] ?? "Varlık") : "Çekim"}</dt>
            <dd><span className="mono">{c.subject}</span>
              {here && c.scene_id && <> · <button type="button" className="text-btn" onClick={() => { selectScene(c.scene_id!); setPage("edit"); }}>kurguda aç</button></>}
              {here && c.asset_id && !c.scene_id && <> · <button type="button" className="text-btn" onClick={() => gotoAsset(c.asset_id!)}>varlığı aç</button></>}
            </dd>
          </>
        )}
        {c.tier && <><dt>Varyasyon</dt><dd>{TIER_TR[c.tier] ?? c.tier} · {(c.wave_no ?? 0) + 1}. dalga</dd></>}
        {c.attempt != null && <><dt>Deneme</dt><dd>{c.attempt}</dd></>}
        <dt>Başladı</dt><dd>{fullTime(c.started_at)}</dd>
        <dt>Süre</dt><dd>{c.duration_s == null ? "sürüyor" : `${c.duration_s.toFixed(1).replace(".", ",")} sn`}</dd>
        <dt>İstek kimliği</dt>
        <dd>{c.request_id ? (
          <span className="row"><span className="mono small" style={{ wordBreak: "break-all" }}>{c.request_id}</span>
            <button type="button" className="btn ghost icon-btn" onClick={copy} title="Kopyala" aria-label="İstek kimliğini kopyala"><Icon.copy /></button>
            {copied && <span className="faint small">kopyalandı</span>}</span>
        ) : <span className="faint">{c.mode === "virtual" ? "yok (sanal çağrı)" : "kaydedilmedi"}</span>}</dd>
        <dt>Defter etiketi</dt><dd className="mono small">{c.label}</dd>
        <dt>Rezervasyon</dt><dd className="mono small faint" style={{ wordBreak: "break-all" }}>{c.reservation}</dd>
      </dl>
      {c.error && <div className="note err" role="status"><b>Hata{c.error_type ? ` (${c.error_type})` : ""}:</b> {c.error}</div>}
      {priceNote(c) && <p className="faint small">{priceNote(c)}</p>}
      {c.backfilled && <p className="faint small">Bu kayıt servis geçmişi eklenmeden önce yapıldı; bütçe defterinden aktarıldı{old.length ? ` (${old.join(", ")})` : ""}.</p>}
      {c.steps.length > 0 && (
        <Section title="Sağlayıcıya giden istekler" meta={String(c.steps.length)}>
          <ol className="steps">
            {c.steps.map((st, i) => {
              const params = Object.entries(st).filter(([k]) => !["model", "request_id", "prompt"].includes(k));
              return (
                <li key={i}>
                  <div className="mono small">{st.model}</div>
                  {st.request_id && <div className="faint small">istek <span className="mono">{st.request_id}</span></div>}
                  {params.length > 0 && (
                    <dl className="kv wide">
                      {params.map(([k, v]) => <span key={k} style={{ display: "contents" }}><dt>{STEP_TR[k] ?? k}</dt><dd className="mono small">{stepValue(k, v)}</dd></span>)}
                    </dl>
                  )}
                  {st.prompt && <details><summary className="small">İstem</summary><p className="small dim">{st.prompt}</p></details>}
                </li>
              );
            })}
          </ol>
        </Section>
      )}
    </div>
  );
}

export function HistoryPage() {
  const { history: h, setHistory, projects, meta } = usePanel();
  const [limit, setLimit] = useState(PAGE_SIZE);
  const [last, setLast] = useState<ServiceCall | null>(null);
  const qs = filterQuery(h);
  const tz = typeof Intl !== "undefined" ? Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC" : "UTC";
  const opts = { refreshInterval: POLL_MS * 2, keepPreviousData: true };
  const { data: page, error } = useSWR<CallPage>(`/history?${qs}&limit=${limit}`, fetcher, opts);
  const { data: sum } = useSWR<CallSummary>(`/history/summary?${qs}&tz=${encodeURIComponent(tz)}`, fetcher, opts);
  const items = page?.items ?? [];
  const selected = items.find((c) => String(c.id) === h.call) ?? (last && String(last.id) === h.call ? last : null);
  const filtered = Boolean(qs);
  const set = (f: HistoryFilter) => { setLimit(PAGE_SIZE); setHistory({ ...f, call: null }); };
  const pick = (c: ServiceCall) => { setLast(c); setHistory({ call: String(c.id) }); };
  const opName = h.operation ? sum?.by_operation.find((o) => o.operation === h.operation)?.title ?? h.operation : null;

  const select = (label: string, key: keyof HistoryFilter, all: string, options: readonly (readonly [string, string])[]) => (
    <label className="field">{label}
      <select value={h[key] ?? ""} onChange={(e) => set({ [key]: e.target.value || null } as HistoryFilter)}>
        <option value="">{all}</option>
        {options.map(([v, t]) => <option key={v} value={v}>{t}</option>)}
      </select>
    </label>
  );

  return (
    <div className="page history">
      <Summary sum={sum} filtered={filtered} />

      <Panel title="Çağrılar" className="h-calls" meta={page ? `${items.length}${page.next ? "+" : ""} kayıt` : ""}
        tools={<a className="btn" href={`/api/history.csv?${qs}`} download title="Süzgece uyan bütün çağrılar">CSV indir</a>}>
        <div className="hist-filters">
          {select("Proje", "project", "Bütün projeler", (projects ?? []).map((p) => [p.id, p.title] as const))}
          {select("Servis", "service", "Bütün servisler", SERVICES)}
          {select("Mod", "mode", "Gerçek ve sanal", [["real", "Yalnızca gerçek"], ["virtual", "Yalnızca sanal"]])}
          {select("Durum", "status", "Bütün durumlar", [["succeeded", "Başarılı"], ["failed", "Başarısız"], ["running", "Sürüyor"]])}
          {select("Dönem", "days", "Bütün zamanlar", PERIODS)}
          {opName && <span className="tag info">{opName}<button type="button" aria-label="İşlem süzgecini kaldır" onClick={() => set({ operation: null })}>×</button></span>}
          {filtered && <button type="button" className="btn ghost" onClick={() => setHistory({}, true)}>Süzgeçleri temizle</button>}
        </div>
        {error && !page ? <p className="empty">Geçmiş alınamadı: {(error as Error).message}</p>
          : !page ? <p className="loading">Yükleniyor…</p>
          : !items.length ? (
            <p className="empty">{filtered ? "Bu süzgece uyan çağrı yok." : meta?.dry_run === false
              ? "Henüz çağrı yok. Ücretli moddaki her üretim burada görünecek."
              : "Henüz çağrı yok. Ücretsiz modda üretimler burada sanal tutarlarıyla görünür."}</p>
          ) : (
            <table className="tbl calls">
              <thead><tr><th>Zaman</th><th>İşlem</th><th>Servis ve model</th><th>Mod</th><th>Durum</th><th className="r">Tutar</th></tr></thead>
              <tbody>
                {items.map((c) => {
                  const amount = callAmountText(c);
                  const on = String(c.id) === h.call;
                  return (
                    <tr key={c.id} className={on ? "sel" : ""} onClick={() => pick(c)}>
                      <td className="mono nowrap" title={fullTime(c.started_at)}>{whenText(c.started_at)}</td>
                      <td><button type="button" className="text-btn" aria-pressed={on} onClick={(e) => { e.stopPropagation(); pick(c); }}>{c.operation_title}</button>
                        <small>{whereText(c, !h.project) || "—"}</small></td>
                      <td>{c.service_name}<small className="mono">{c.model ?? "—"}</small></td>
                      <td><span className={`tag ${c.mode === "real" ? "warn" : ""}`}>{CALL_MODE_TR[c.mode]}</span></td>
                      <td><span className={`st ${statusTone(c.status)}`}>{CALL_STATUS_TR[c.status]}</span></td>
                      <td className="r mono">{amount.value}{amount.note && <small>{amount.note}</small>}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        {page?.next && (
          <div className="pad row">
            {limit < MAX_ROWS
              ? <button type="button" className="btn" onClick={() => setLimit(Math.min(MAX_ROWS, limit + PAGE_SIZE))}>Daha eski çağrıları göster</button>
              : <span className="faint small">Panel en çok {MAX_ROWS} çağrı gösterir; hepsi için CSV indirin ya da süzgeci daraltın.</span>}
          </div>
        )}
      </Panel>

      <Panel title={selected ? selected.operation_title : "Ayrıntı"} meta={selected ? `#${selected.id}` : ""}>
        {selected ? <Detail key={selected.id} c={selected} />
          : <p className="pad faint">Listeden bir çağrı seçin: hangi servisin hangi modeli çağrıldı, sağlayıcının istek kimliği, gönderilen parametreler, tutar ve varsa hata burada görünür.</p>}
      </Panel>
    </div>
  );
}
