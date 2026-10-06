"use client";
/* Ayarlar: ücretli servislerin bağlantıları (anahtar, model ve uç noktalar, motor seçimi), ücretsiz/ücretli mod ve
   fiyat tablosu. Her şey .env (fiyatlar pricing.yaml) dosyasına yazılır ve servisler yeniden başlatılmadan geçerli
   olur. Anahtarlar yalnızca maskeli görünür; ücretli moda geçiş açık onay ister. */
import { useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { api, fetcher } from "@/lib/api";
import { POLL_MS, usePanel } from "@/lib/panel";
import { bytesText, clock, durText, stepDurations, usd, usdExact, whenText } from "@/lib/text";
import { COMFY_KIND_TR } from "@/lib/i18n";
import type { ComfyState, PricingTable, ProbeResult, ReplacementJob, ReplacementState, Service, ServiceField, SettingsState, SetupLog, TlsState } from "@/lib/types";
import { Icon } from "./icons";
import { NoteBox, Panel, Section } from "./ui";

type Sec = "mode" | Service["id"] | "pricing" | "audit";

const tone = (ok: boolean | null) => (ok === true ? "ok" : ok === false ? "bad" : "warn");

/** Servisin fiyat satırları (pricing.yaml'dan; rezervasyonlar bu fiyatlarla yapılır). */
function priceRows(id: Service["id"], t: PricingTable | undefined): [string, string][] {
  if (!t) return [];
  if (id === "elevenlabs") return [["Konuşma (1000 karakter)", usd(t.tts_per_1k_chars)]];
  if (id === "anthropic") return [["Girdi (1M token)", usd(t.llm_per_million_tokens.input)], ["Çıktı (1M token)", usd(t.llm_per_million_tokens.output)]];
  if (id === "vast") {
    const h3 = t.self_hosted?.h3;
    if (!h3) return [];
    const rows: [string, string][] = [["GPU makinesi (saat)", `$${h3.gpu_hourly_usd}`]];
    for (const [res, sec] of Object.entries(h3.render_seconds)) {
      rows.push([`${res}: video saniyesi başına GPU süresi ${sec} sn (tahmin)`, `$${(h3.gpu_hourly_usd / 3600 * sec).toFixed(4)}`]);
    }
    if (h3.keyframe_seconds) rows.push([`Storyboard ilk karesi (ComfyUI, ${h3.keyframe_seconds} sn GPU, tahmin)`, `$${(h3.gpu_hourly_usd / 3600 * h3.keyframe_seconds).toFixed(4)}`]);
    rows.push(["Boşta bekleme (dakika)", `$${(h3.gpu_hourly_usd / 60).toFixed(4)}`]);
    return rows;
  }
  const rows: [string, string][] = [];
  for (const [engine, byRes] of Object.entries(t.video_per_second)) {
    for (const [res, price] of Object.entries(byRes)) rows.push([`${engine} · ${res} (video saniyesi)`, `$${price}`]);
  }
  rows.push(["flux_lora (megapiksel)", `$${t.image_per_megapixel.flux_lora}`]);
  if (t.image_per_image?.flux_kontext != null) rows.push(["flux_kontext: karakter referanslı kare (görsel)", `$${t.image_per_image.flux_kontext}`]);
  rows.push(["Dudak senkronu (dakika)", `$${t.lipsync_per_minute}`]);
  return rows;
}

function KeyBlock({ svc, field, editable, onSaved, probe: canProbe = true }: { svc: Service; field: ServiceField; editable: boolean; onSaved: () => void; probe?: boolean }) {
  const { busy, act, note } = usePanel();
  const [value, setValue] = useState("");
  const [sure, setSure] = useState(false);
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const save = (v: string) => act(async () => {
    await api.patch<SettingsState>("/settings", { values: { [field.env]: v } });
    setValue(""); setSure(false); setProbe(null); onSaved();
    note("settings", v ? `${field.label} kaydedildi; bütün servislerde hemen geçerli.` : `${field.label} silindi.`, "ok");
  }, "settings");
  const test = () => act(async () => {
    const r = await api.post<ProbeResult>(`/settings/test/${svc.id}`);
    setProbe(r);
  }, "settings");
  return (
    <div className="stack">
      <div className="row">
        <span className="dim small" style={{ minWidth: 92 }}>{field.label}</span>
        {field.set ? <span className="mono">{field.masked}</span> : <span className="tag bad">tanımlı değil</span>}
        <span className="grow" />
        {canProbe && <button className="btn" type="button" disabled={busy || !field.set} onClick={test} title="Ücretsiz bir doğrulama isteği; üretim yapmaz">Bağlantıyı sına</button>}
      </div>
      <div className="row">
        {probe && <span className={`tag ${tone(probe.ok)}`} style={{ whiteSpace: "normal" }} title={probe.status ? `HTTP ${probe.status}` : ""}>{probe.message}</span>}
      </div>
      {editable && (
        <form className="row" onSubmit={(e) => { e.preventDefault(); if (value.trim()) void save(value.trim()); }}>
          <input className="grow mono" type="password" autoComplete="off" spellCheck={false} aria-label={`${svc.name} yeni API anahtarı`}
            placeholder={field.set ? "Yeni anahtarı yapıştır (değiştirmek için)" : "Anahtarı yapıştır"} value={value} onChange={(e) => setValue(e.target.value)} />
          <button className="btn primary" type="submit" disabled={busy || !value.trim()}>Anahtarı kaydet</button>
          {field.set && (sure
            ? <button className="btn danger" type="button" disabled={busy} onClick={() => save("")}>Eminim, sil</button>
            : <button className="btn ghost" type="button" onClick={() => setSure(true)}>Anahtarı sil</button>)}
        </form>
      )}
    </div>
  );
}

function ServiceView({ svc, st, prices, onSaved }: { svc: Service; st: SettingsState; prices: PricingTable | undefined; onSaved: () => void }) {
  const { busy, act, note, setHistory } = usePanel();
  const keys = svc.fields.filter((f) => f.kind === "secret");
  const others = svc.fields.filter((f) => f.kind !== "secret");
  const [draft, setDraft] = useState<Record<string, string>>({});
  const val = (f: ServiceField) => draft[f.env] ?? f.value ?? "";
  const dirty = others.filter((f) => draft[f.env] != null && draft[f.env] !== f.value);
  const save = () => act(async () => {
    const r = await api.patch<{ changed: string[] }>("/settings", { values: Object.fromEntries(dirty.map((f) => [f.env, val(f).trim()])) });
    setDraft({}); onSaved();
    note("settings", r.changed.length ? `Kaydedildi: ${r.changed.join(", ")}. Yeniden başlatma gerekmez.` : "Değişiklik yok.", "ok");
  }, "settings");
  return (
    <Panel title={svc.name} meta={svc.purpose}
      tools={<a className="btn ghost" href={svc.console} target="_blank" rel="noopener noreferrer">Hesap paneli ↗</a>}>
      <div className="pad stack">
        <div className="row">
          <span className={`tag ${svc.key_set ? "ok" : "bad"}`}>{svc.key_set ? "Anahtar tanımlı" : "Anahtar yok"}</span>
          <span className={`tag ${svc.active ? "info" : ""}`}>{svc.active ? "Ücretli modda kullanılır" : "Ücretli modda da kullanılmaz"}</span>
          <span className={`tag ${st.dry_run ? "ok" : "bad"}`}>{st.dry_run ? "Şu an kilitli: ücretsiz mod" : "Ücretli mod açık"}</span>
          <span className="grow" />
          <span className="dim small">Gerçek harcama <b className="mono">{usdExact(svc.spent_usd)}</b> · sanal <span className="mono">{usdExact(svc.virtual_usd)}</span></span>
          <button className="btn ghost" type="button" onClick={() => setHistory({ service: svc.id }, true)} title={`${svc.name} çağrılarının geçmişi`}><Icon.receipt />Geçmiş</button>
        </div>
        {keys.map((k) => <KeyBlock key={k.env} svc={svc} field={k} editable={st.editable} onSaved={onSaved} probe={k.env === svc.key_env} />)}
      </div>
      {svc.id === "vast" && <BoxSection st={st} svc={svc} />}
      {svc.id === "vast" && st.comfy && <ComfySection comfy={st.comfy} editable={st.editable} onSaved={onSaved} />}
      <Section title="Model ve uç noktalar" meta={`${others.length}`}>
        <table className="tbl">
          <tbody>
            {others.map((f) => (
              <tr key={f.env}>
                <td style={{ width: "34%" }}>{f.label}<small className="mono">{f.env}</small></td>
                <td>
                  {f.kind === "choice" ? (
                    <select value={val(f)} disabled={!st.editable} aria-label={f.label} onChange={(e) => setDraft({ ...draft, [f.env]: e.target.value })}>
                      {(f.choices ?? []).map((c) => <option key={c} value={c}>{c}</option>)}
                    </select>
                  ) : f.kind === "number" ? (
                    <input type="number" className="mono" style={{ width: 140 }} disabled={!st.editable} aria-label={f.label}
                      min={f.minimum ?? 0} step="any" value={val(f)} onChange={(e) => setDraft({ ...draft, [f.env]: e.target.value })} />
                  ) : (
                    <input type="text" className="mono" style={{ width: "100%" }} spellCheck={false} disabled={!st.editable} aria-label={f.label}
                      placeholder={f.env === "H3_SETUP_SCRIPT_URL" ? "boş: backend/vast/setup.sh isteğe gömülür"
                        : f.kind === "url" ? "https://ornek-dort-kelime.trycloudflare.com" : undefined}
                      value={val(f)} onChange={(e) => setDraft({ ...draft, [f.env]: e.target.value })} />
                  )}
                  {f.default && val(f) !== f.default && <small>varsayılan: <span className="mono">{f.default}</span></small>}
                  {f.minimum != null && Number(val(f)) < f.minimum && <small className="bad">en az {f.minimum}</small>}
                </td>
                <td className="r" style={{ whiteSpace: "nowrap" }}>
                  {f.default && val(f) !== f.default && st.editable && (
                    <button className="btn ghost" type="button" onClick={() => setDraft({ ...draft, [f.env]: f.default! })}>Varsayılan</button>
                  )}
                  {f.url && <a className="btn ghost" href={f.url} target="_blank" rel="noopener noreferrer">Model sayfası ↗</a>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {st.editable && (
          <div className="row">
            <button className="btn primary" type="button" disabled={busy || !dirty.length} onClick={save}>Değişiklikleri kaydet{dirty.length ? ` (${dirty.length})` : ""}</button>
            {dirty.length > 0 && <button className="btn ghost" type="button" onClick={() => setDraft({})}>Geri al</button>}
          </div>
        )}
      </Section>
      <Section title="Kullanıldığı yerler" meta={`${svc.used_by.length}`}>
        <ul className="lines">{svc.used_by.map((u) => <li key={u}>{u}</li>)}</ul>
      </Section>
      <Section title="Fiyatlar" meta="pricing.yaml">
        <table className="tbl"><tbody>{priceRows(svc.id, prices).map(([k, v]) => <tr key={k}><td>{k}</td><td className="r mono">{v}</td></tr>)}</tbody></table>
        <p className="faint small">Bütçe rezervasyonu ve Kill-Switch bu fiyatlarla hesaplanır; Fiyat tablosu bölümünden düzenlenir.</p>
        {svc.id === "vast" && (
          <p className="faint small">vast.ai makineyi saatle faturalar. Her işin bedeli, sunucunun bildirdiği GPU süresi × saatlik ücrettir;
            makinenin boşta geçen süresi bütçeye yansımaz. Kullanmadığınızda makineyi vast.ai'de durdurun.</p>
        )}
      </Section>
    </Panel>
  );
}

/** H3 makinesi: elle başlat / durdur, yeni makine kiralama (teklif → onay → kiralama → setup.sh), kurulum günlüğü, çıktı
    güvencesi ve sertifika sabitleme. Makineye dokunan her eylem yalnızca ücretli modda (vast.ai API'si ücretli kilitten
    geçer); kayıtlar ücretsiz modda da okunur. */
function BoxSection({ st, svc }: { st: SettingsState; svc: Service }) {
  const { busy, act, note } = usePanel();
  const { data: rep, mutate: refreshRep } = useSWR<ReplacementState>("/settings/vast/replacement", fetcher, { refreshInterval: POLL_MS * 2 });
  const p = rep?.proposal, j = rep?.job;
  const active = Boolean(j && ["queued", "renting", "booting", "installing"].includes(j.state));
  const decide = (action: "approve" | "reject") => act(async () => {
    await api.post("/settings/vast/replacement", { action });
    await refreshRep();
    note("settings", action === "approve" ? "Yeni makine kiralanıyor; kurulumu aşağıdaki günlükten izleyin (modeller ~105 GB)." : "Öneri reddedildi.", "ok");
  }, "settings");
  const provision = () => act(async () => {
    await api.post("/settings/vast/provision");
    await refreshRep();
    note("settings", "Uygun teklif bulundu: aşağıda onayınızı bekliyor. Onaylamadan hiçbir şey kiralanmaz.", "ok");
  }, "settings");
  const run = (action: "start" | "stop") => act(async () => {
    const r = await api.post<{ status: string; geolocation: string | null }>("/settings/vast/instance", { action });
    note("settings", `vast.ai makinesi: ${action === "start" ? "başlatılıyor" : "durduruluyor"} (şu an: ${r.status}${r.geolocation ? ", " + r.geolocation : ""}).`, "ok");
  }, "settings");
  const initial = p?.kind === "initial";
  return (
    <Section title="Makine" meta={rep?.instance_id ? `vast.ai · ${rep.instance_id}` : "vast.ai · makine yok"}>
      <p className="faint small">Kuyruk ayarlanan süre boş kalırsa makine kendiliğinden durdurulur; boşta geçen süre son etkin projenin
        bütçesine yazılır ve Kill-Switch'e yansır. Durdurulmuş makinenin diski vast.ai'de ücretlenmeye devam eder.</p>
      <div className="row">
        <button className="btn" type="button" disabled={busy || st.dry_run || !rep?.instance_id} onClick={() => run("start")}>Makineyi başlat</button>
        <button className="btn" type="button" disabled={busy || st.dry_run || !rep?.instance_id} onClick={() => run("stop")}>Makineyi durdur</button>
        <button className="btn" type="button" disabled={busy || st.dry_run || !svc.key_set || active || Boolean(p)} onClick={provision}
          title="Uygun teklifi bulur ve onayınıza sunar; onaylarsanız kiralanır ve setup.sh ile kurulur. Hiçbir makine silinmez.">Yeni makine kirala</button>
        {st.dry_run && <span className="faint small">Ücretsiz modda makine buradan yönetilmez ve kiralanmaz.</span>}
      </div>
      {rep?.conditions && <p className="faint small">Teklif koşulları: {rep.conditions} (Model ve uç noktalar'dan değiştirilir).</p>}
      {p && !active && (
        <div className="note warn" style={{ marginTop: 10 }}>
          <b>{initial ? "Yeni makine önerisi" : "Yedek makine önerisi"}</b> · {p.reason}
          <dl className="kv" style={{ marginTop: 6 }}>
            <dt>Teklif</dt><dd className="mono">{p.gpu} · {p.dph.toFixed(3)} $/sa · {p.geolocation ?? "konum bilinmiyor"}</dd>
            <dt>Makine</dt><dd className="mono">{p.ram_gb} GB RAM · {p.disk_gb} GB disk{p.inet_mbps ? ` · ${Math.round(p.inet_mbps)} Mbps` : ""}{p.reliability ? ` · güvenilirlik ${(p.reliability * 100).toFixed(1)}%` : ""}</dd>
            <dt>Sonra</dt><dd>{initial
              ? "setup.sh ComfyUI'yi, H3 modellerini ve görsel modelini kurar; kimlik, adres ve belirteç Ayarlar'a yazılır. Hiçbir makine silinmez."
              : `setup.sh ComfyUI'yi, H3 modellerini ve görsel modelini kurar; hazır olunca eski makine${p.old_instance_id ? ` (${p.old_instance_id})` : ""} yalnızca ürettiği her çıktı yerel depoda doğrulanmışsa silinir.`}</dd>
          </dl>
          <div className="row">
            <button className="btn primary" type="button" disabled={busy || st.dry_run} onClick={() => decide("approve")}>Onayla ve kirala</button>
            <button className="btn ghost" type="button" disabled={busy} onClick={() => decide("reject")}>Reddet</button>
          </div>
        </div>
      )}
      {j && <JobView rep={rep!} st={st} />}
      {rep?.outputs && <OutputsView rep={rep} st={st} onDone={() => void refreshRep()} />}
      {rep?.tls && <TlsView rep={rep} editable={st.editable} onDone={() => void refreshRep()} />}
    </Section>
  );
}

/** Kiralama ve kurulum: aşamalar ve süreleri, setup.sh günlüğü (portalın kimlik doğrulamalı günlük portundan). */
function JobView({ rep, st }: { rep: ReplacementState; st: SettingsState }) {
  const j = rep.job!;
  const watching = !st.dry_run && Boolean(j.log_url) && (j.state === "installing" || j.state === "ready" || j.state === "failed");
  const live = j.state === "installing";
  const { data: log } = useSWR<SetupLog>(watching ? "/settings/vast/setup-log" : null, fetcher, { refreshInterval: live ? 5000 : 0 });
  const now = Date.now() / 1000;
  const steps = stepDurations(j.steps, now);
  const total = j.started_at ? (["ready", "failed"].includes(j.state) ? (j.updated_at ?? now) : now) - j.started_at : null;
  return (
    <div className={`note ${j.state === "failed" || j.retained ? "err" : j.state === "ready" ? "ok" : ""}`} style={{ marginTop: 10 }}>
      <div className="row">
        <b>{j.kind === "initial" ? "Yeni makine" : "Yedek makine"}: {rep.states[j.state] ?? j.state}</b>
        {j.new_instance_id && <span className="mono small">· {j.new_instance_id}</span>}
        {j.url && <span className="mono small">· {j.url}</span>}
        <span className="grow" />
        {total != null && <span className="mono small" title="Kiralama isteğinden bu yana">{durText(total)}</span>}
      </div>
      {steps.length > 1 && (
        <p className="small dim" style={{ margin: "6px 0 0" }}>
          {steps.map(([name, secs], i) => <span key={name}>{i ? " → " : ""}{rep.states[name] ?? name} <span className="mono">{durText(secs)}</span></span>)}
        </p>
      )}
      {j.message && <p className="small" style={{ margin: "6px 0 0" }}>{j.message}</p>}
      {j.retained && <p className="small bad" style={{ margin: "6px 0 0" }}>Makine {j.retained.instance_id} silinmeden önce: aşağıdaki Çıktı güvencesi'nden eksikleri indirin ya da kaybı kabul edin.</p>}
      {(jobActive(j) || j.log_url) && (
        <details open={live} style={{ marginTop: 8 }}>
          <summary className="small">Kurulum günlüğü (setup.sh){log?.step ? ` · ${log.step}` : ""}{log?.done ? ` · bitti: ${durText(log.total_s)}${log.ok ? "" : " (eksik)"}` : ""}</summary>
          {log?.phases && log.phases.length > 0 && (
            <p className="small dim" style={{ margin: "6px 0" }}>{log.phases.map((ph) => `${ph.name}: ${durText(ph.seconds)}`).join(" · ")}</p>
          )}
          {log?.available
            ? <pre className="mono small" style={{ maxHeight: 260, overflow: "auto", whiteSpace: "pre-wrap", margin: "6px 0 0" }}>{log.lines.slice(-60).join("\n")}</pre>
            : <p className="faint small">{st.dry_run ? "Ücretsiz modda makineye bağlanılmaz." : log?.message ?? "Makine açılınca günlük burada görünür."}</p>}
          <p className="faint small">Makinede: vast.ai konsolunda makinenin terminali ya da SSH ile <span className="mono">tail -f /var/log/hellgrind/setup.log</span>.
            Satırlar başlangıçtan geçen süreyi, her indirmenin hızını ve sonda toplam kurulum süresini gösterir.</p>
        </details>
      )}
    </div>
  );
}

const jobActive = (j: ReplacementJob) => ["queued", "renting", "booting", "installing"].includes(j.state);

/** Çıktı güvencesi: her H3 çıktısı indirilip yerel depoda doğrulanır; eksik çıktısı olan makine silinmez. */
function OutputsView({ rep, st, onDone }: { rep: ReplacementState; st: SettingsState; onDone: () => void }) {
  const { busy, act, note } = usePanel();
  const [sure, setSure] = useState<string | null>(null);
  const o = rep.outputs!, r = rep.retrieval;
  const retrieving = Boolean(r && ["starting", "waiting", "downloading"].includes(r.state));
  const go = (instance_id: string, action: "retrieve" | "abandon", destroy_after = false) => act(async () => {
    const res = await api.post<{ lost?: number }>("/settings/vast/outputs", { action, instance_id, destroy_after });
    setSure(null); onDone();
    note("settings", action === "retrieve" ? "Eksik çıktılar indiriliyor; makine çalışmıyorsa başlatılır." : `Makine ${instance_id} silindi; ${res.lost ?? 0} çıktının kaybı kabul edildi.`, action === "retrieve" ? "ok" : "err");
  }, "settings");
  const unsafeTotal = o.machines.reduce((n, m) => n + m.unsafe, 0);
  return (
    <div style={{ marginTop: 12 }}>
      <h3 className="small">Çıktı güvencesi {unsafeTotal ? <span className="tag bad">{unsafeTotal} çıktı yalnızca makinede</span> : <span className="tag ok">bütün çıktılar yerel depoda</span>}</h3>
      <p className="faint small">Her video ve storyboard karesi iş biter bitmez indirilir; boyutu sunucunun bildirdiğiyle, depodaki kopyası SHA-256 ile
        doğrulanır (meta veri dahil). Eski makine yalnızca ürettiği her çıktı doğrulanmışsa silinir.</p>
      {o.machines.length > 0 && (
        <table className="tbl">
          <thead><tr><th>Makine</th><th className="r">Yerel depoda</th><th className="r">Yalnızca makinede</th><th className="r">Üretilemedi / kayıp</th><th /></tr></thead>
          <tbody>
            {o.machines.map((m) => {
              const id = m.instance_id ?? "";
              return (
                <tr key={id || "yok"}>
                  <td className="mono">{id || "yönetimsiz"}{m.current && <small>Ayarlardaki makine</small>}</td>
                  <td className="r mono">{m.stored}<small>{bytesText(m.stored_bytes)}</small></td>
                  <td className={`r mono ${m.unsafe ? "bad" : ""}`}>{m.unsafe}</td>
                  <td className="r mono">{m.failed} / {m.lost}</td>
                  <td className="r" style={{ whiteSpace: "nowrap" }}>
                    {m.unsafe > 0 && id && (m.current
                      ? <button className="btn" type="button" disabled={busy || st.dry_run || retrieving} onClick={() => go(id, "retrieve")}>Eksikleri indir</button>
                      : <>
                        <button className="btn" type="button" disabled={busy || st.dry_run || retrieving} onClick={() => go(id, "retrieve", true)}
                          title="Makine çalışmıyorsa başlatılır (saatlik ücret işler); hepsi indirilip doğrulanınca makine silinir">Eksikleri indir, sonra sil</button>
                        {sure === id
                          ? <button className="btn danger" type="button" disabled={busy || st.dry_run} onClick={() => go(id, "abandon")}>Eminim: {m.unsafe} çıktıyı kaybet ve sil</button>
                          : <button className="btn ghost" type="button" disabled={busy || st.dry_run} onClick={() => setSure(id)}>Kayıp say ve sil</button>}
                      </>)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {r && <p className={`small ${r.state === "failed" ? "bad" : "dim"}`}>Yeniden indirme ({r.instance_id}): <b>{rep.retrieval_states?.[r.state] ?? r.state}</b>{r.message ? ` · ${r.message}` : ""}</p>}
      {o.unsafe.length > 0 && (
        <details>
          <summary className="small">Yerel depoda olmayan çıktılar ({o.unsafe.length}{o.unsafe.length >= 50 ? "+" : ""})</summary>
          <table className="tbl">
            <tbody>
              {o.unsafe.slice(0, 20).map((u) => (
                <tr key={u.id}>
                  <td className="mono small">{u.instance_id ?? "—"}</td>
                  <td className="small">{u.kind === "video" ? "video" : "storyboard karesi"}{u.task ? ` · ${u.task}` : ""}<small className="mono">{u.job_id}</small></td>
                  <td className="small">{u.status === "completed" ? "bitti, indirilemedi" : "gönderildi"}{u.error ? <small>{u.error}</small> : null}</td>
                  <td className="r small mono">{whenText(u.submitted_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </div>
  );
}

/** Sunucu sertifikası: doğrula / sabitle / kapalı; sabitlenen parmak izi ve uyarılar. */
function TlsView({ rep, editable, onDone }: { rep: ReplacementState; editable: boolean; onDone: () => void }) {
  const { busy, act, note } = usePanel();
  const t = rep.tls!;
  const reset = () => act(async () => {
    await api.del("/settings/vast/tls-pin");
    onDone();
    note("settings", "Sertifika sabitlemesi sıfırlandı: bir sonraki başarılı bağlantı yeniden sabitler.", "ok");
  }, "settings");
  return (
    <div style={{ marginTop: 12 }}>
      <h3 className="small">Sunucu sertifikası <span className={`tag ${t.mode === "off" || t.fallback ? "warn" : "ok"}`}>{TLS_TR[t.mode]}</span></h3>
      {t.mismatch && (
        <div className="note err small">Sertifika değişti ({t.mismatch.endpoint}): beklenen <span className="mono">{t.mismatch.expected.slice(0, 23)}…</span>,
          gelen <span className="mono">{t.mismatch.seen.slice(0, 23)}…</span>. İstek gönderilmedi; üretim durdu. Makineyi bilerek yenilediyseniz
          (setup.sh günlüğündeki parmak iziyle karşılaştırın) sabitlemeyi sıfırlayın.</div>
      )}
      {t.fallback && !t.mismatch && (
        <div className="note warn small">Sertifika sabitlenemedi ({t.fallback.endpoint}: {t.fallback.reason}); bağlantı doğrulamasız sürüyor (trafik yine şifreli).</div>
      )}
      {t.mode === "pin" && (
        <p className="small dim">{t.pin
          ? <>Sabitlenen parmak izi ({t.endpoint}, {whenText(t.pin.pinned_at)}): <span className="mono" style={{ wordBreak: "break-all" }}>{t.pin.sha256}</span></>
          : <>Henüz sabitlenmedi: ilk başarılı bağlantıda {t.endpoint ?? "sunucu"} sertifikasının SHA-256 parmak izi kaydedilir.</>}</p>
      )}
      {t.mode === "off" && <p className="small warn">Doğrulama kapalı: sunucunun kimliği denetlenmez. Kendinden imzalı portalda "pin" önerilir.</p>}
      {editable && t.mode === "pin" && (t.pin || t.mismatch) && (
        <button className="btn ghost" type="button" disabled={busy} onClick={reset}>Sabitlemeyi sıfırla</button>
      )}
    </div>
  );
}

const TLS_TR: Record<TlsState["mode"], string> = { verify: "doğrulanıyor", pin: "parmak izi sabitleme", off: "doğrulama kapalı" };

/** ComfyUI iş akışları: ComfyUI'de Workflow › Export (API) ile kaydedilen JSON, değişen değerler yer tutucuyla. */
function ComfySection({ comfy, editable, onSaved }: { comfy: ComfyState; editable: boolean; onSaved: () => void }) {
  const { busy, act, note } = usePanel();
  const upload = (kind: string) => (file: File) => act(async () => {
    await api.put(`/settings/comfy/${kind}`, { text: await file.text() });
    onSaved();
    note("settings", `${COMFY_KIND_TR[kind]} iş akışı doğrulandı ve kaydedildi.`, "ok");
  }, "settings");
  const remove = (kind: string) => act(async () => {
    await api.del(`/settings/comfy/${kind}`);
    onSaved();
    note("settings", `${COMFY_KIND_TR[kind]} iş akışı kaldırıldı.`, "ok");
  }, "settings");
  return (
    <Section title="ComfyUI iş akışları" meta={comfy.backend === "comfyui" ? "arka uç: ComfyUI" : "arka uç: SGLang (kullanılmıyor)"} open={comfy.backend === "comfyui"}>
      <p className="faint small">Yer tutucular: <span className="mono">{"{{prompt}} {{seed}} {{width}} {{height}} {{frames}} {{duration}} {{steps}} {{first_frame}} {{reference_1}}…{{reference_9}} {{filename_prefix}}"}</span>.
        ComfyUI makinede 127.0.0.1'de çalışır; adres SSH tüneli ya da portalın kimlik doğrulamalı vekilidir. Klasör: <span className="mono">{comfy.dir}</span></p>
      <table className="tbl">
        <tbody>
          {comfy.workflows.map((w) => (
            <tr key={w.kind}>
              <td style={{ width: "30%" }}>{COMFY_KIND_TR[w.kind] ?? w.kind}<small className="mono">{w.kind}.json</small></td>
              <td>
                {w.error ? <span className="tag bad" style={{ whiteSpace: "normal" }}>{w.error}</span>
                  : w.present ? <span className="tag ok">hazır</span> : <span className="tag">yok</span>}
                {w.present && !w.error && <small className="mono">{w.placeholders.map((x) => `{{${x}}}`).join(" ")}</small>}
                {!w.present && <small>gerekli: <span className="mono">{w.required.map((x) => `{{${x}}}`).join(" ")}</span></small>}
              </td>
              <td className="r" style={{ whiteSpace: "nowrap" }}>
                {editable && (
                  <label className="btn" aria-disabled={busy} title={`${w.kind}.json dosyasını seç (ComfyUI › Export (API))`}>
                    {w.present ? "Değiştir" : "Yükle"}
                    <input type="file" accept="application/json,.json" hidden disabled={busy}
                      onChange={(e) => { const f = e.currentTarget.files?.[0]; e.currentTarget.value = ""; if (f) void upload(w.kind)(f); }} />
                  </label>
                )}
                {editable && w.present && <button className="btn ghost" type="button" disabled={busy} onClick={() => remove(w.kind)}>Kaldır</button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}

function ModeView({ st, onSaved }: { st: SettingsState; onSaved: () => void }) {
  const { busy, act, note } = usePanel();
  const { mutate } = useSWRConfig();
  const [typed, setTyped] = useState("");
  const matches = typed.trim().toLocaleLowerCase("tr") === st.confirmation.toLocaleLowerCase("tr");
  const switchTo = (paid: boolean) => act(async () => {
    const r = await api.post<{ vast_note?: string | null }>("/settings/mode", paid ? { paid, confirm: typed } : { paid });
    setTyped(""); onSaved(); void mutate("/meta");
    note("settings", paid ? "Ücretli mod açık: bundan sonraki üretimler gerçek servisleri çağırır ve gerçek harcama yapar." : `Ücretsiz moda dönüldü: hiçbir ücretli servis çağrılmaz.${r.vast_note ? " " + r.vast_note : ""}`, paid ? "err" : "ok");
  }, "settings");
  const h3 = st.services.find((s) => s.id === "vast");
  const missingPkgs = st.services.some((s) => s.needed && Boolean(s.missing_packages?.length));
  return (
    <Panel title="Çalışma modu" meta={st.dry_run ? "ücretsiz" : "ücretli"}>
      <div className="pad stack">
        <div className={`note ${st.dry_run ? "ok" : "err"}`} style={{ fontSize: 13 }}>
          {st.dry_run ? "Ücretsiz mod açık: hiçbir ücretli servis çağrılmaz. Harcamalar gerçek fiyatlarla sanal olarak tutulur."
            : "Ücretli mod açık: üretimler gerçek servisleri çağırır ve gerçek harcama yapar. Bütçe limiti, sekans tavanları ve Kill-Switch geçerlidir."}
        </div>
        {st.dry_run ? (
          <>
            <h3 className="small">Ücretli moda geçmeden önce</h3>
            <ul className="lines">
              {st.services.map((s) => (
                <li key={s.id}>
                  {!s.needed ? <span className="st">{s.name}: gerekmez ({s.not_needed_reason})</span>
                    : <span className={`st ${s.key_set ? "ok" : "bad"}`}>{s.name}: {s.key_set ? "anahtar tanımlı" : "anahtar yok: bu servisle üretim yapılamaz"}</span>}
                  {s.needed && Boolean(s.missing_packages?.length) && (
                    <span className="st bad"> · paket kurulu değil ({s.missing_packages!.join(", ")}): imajı yeniden derleyin</span>
                  )}
                </li>
              ))}
              <li><span className={`st ${st.scorer.ready ? "ok" : "bad"}`}>Kalite skorlayıcısı: {st.scorer.message}</span></li>
              {st.translator && <li><span className={`st ${st.translator.ready ? "ok" : "warn"}`}>Prompt çevirisi: {st.translator.message}</span></li>}
              {h3 && !h3.key_set && <li className="warn">vast.ai API anahtarı gerekir: video MiniMax H3 ile vast.ai makinesinde üretilir.</li>}
              {h3?.key_set && !h3.active && <li className="warn">Makine henüz tanımlı değil: ücretli moda geçtikten sonra MiniMax H3 › Makine › <b>Yeni makine kirala</b> ile kurun ya da makine kimliğini ve sunucu adresini girin. Makine hazır olana dek üretim başlamaz.</li>}
              {h3?.active && <li>vast.ai makinesinin çalıştığını ve "Bağlantıyı sına" sonucunun "hazır" olduğunu doğrulayın.</li>}
              <li>Fiyat tablosunu sağlayıcıların güncel fiyatlarıyla karşılaştırın; rezervasyonlar bu fiyatlarla yapılır.</li>
              <li>Projelerin bütçe limitlerini ve hız bekçisini (10 dakikalık harcama sınırı) gözden geçirin.</li>
              <li>Mod, servisler yeniden başlatılmadan geçerli olur; o andan sonra başlayan her ücretli çağrı gerçek harcamadır.</li>
            </ul>
            {st.busy.length > 0 && <p className="warn small">Üretim sürüyor ({st.busy.slice(0, 6).join(", ")}{st.busy.length > 6 ? "…" : ""}); bitmeden ücretli moda geçilmez.</p>}
            {st.editable ? (
              <form className="stack" onSubmit={(e) => { e.preventDefault(); if (matches) void switchTo(true); }}>
                <label className="field"><span>Onaylamak için kutuya <b className="mono" style={{ color: "var(--text)" }}>{st.confirmation}</b> yazın</span>
                  <input type="text" autoComplete="off" value={typed} onChange={(e) => setTyped(e.target.value)} />
                </label>
                <div className="row">
                  <button className="btn danger lg" type="submit" disabled={busy || !matches || st.busy.length > 0 || !h3?.key_set || !st.scorer.ready || missingPkgs}>Ücretli moda geç</button>
                  {(!h3?.key_set || !st.scorer.ready) && <span className="faint small">Önce vast.ai API anahtarını girin{st.scorer.ready ? "" : " ve kalite skorlayıcısını hazırlayın"}.</span>}
                  {h3?.key_set && missingPkgs && <span className="faint small">Ücretli paketler imajda kurulu değil: docker compose up -d --build.</span>}
                </div>
              </form>
            ) : <p className="faint small">Ayar dosyası bağlı değil; mod yalnızca .env elle düzenlenerek değişir.</p>}
          </>
        ) : (
          <div className="row"><button className="btn primary lg" type="button" disabled={busy || !st.editable} onClick={() => switchTo(false)}>Ücretsiz moda dön</button></div>
        )}
      </div>
    </Panel>
  );
}

function PricingView({ onSaved }: { onSaved: () => void }) {
  const { busy, act, note } = usePanel();
  const { data, mutate } = useSWR<{ path: string; text: string; table: PricingTable }>("/settings/pricing", fetcher, { revalidateOnFocus: false });
  const [text, setText] = useState<string | null>(null);
  if (!data) return <Panel title="Fiyat tablosu"><p className="loading">Yükleniyor…</p></Panel>;
  const value = text ?? data.text;
  const save = () => act(async () => {
    const r = await api.put<{ path: string; text: string; table: PricingTable }>("/settings/pricing", { text: value });
    await mutate(r, { revalidate: false });
    setText(null); onSaved();
    note("settings", "Fiyat tablosu doğrulandı ve kaydedildi; rezervasyonlar hemen yeni fiyatlarla yapılır.", "ok");
  }, "settings");
  return (
    <section className="pnl script">
      <header className="pnl-h">
        <h2>Fiyat tablosu</h2><span className="meta mono">{data.path}</span><span className="grow" />
        {text != null && <button className="btn ghost" type="button" onClick={() => setText(null)}>Geri al</button>}
        <button className="btn primary" type="button" disabled={busy || text == null} onClick={save}>Doğrula ve kaydet</button>
      </header>
      <textarea aria-label="Fiyat tablosu (YAML)" spellCheck={false} value={value} onChange={(e) => setText(e.target.value)} />
    </section>
  );
}

export function SettingsPage() {
  const { busy, setHistory } = usePanel();
  const { data: st, mutate } = useSWR<SettingsState>("/settings", fetcher, { refreshInterval: POLL_MS * 2 });
  const { data: prices } = useSWR<{ table: PricingTable }>("/settings/pricing", fetcher, { revalidateOnFocus: false });
  const [sec, setSec] = useState<Sec>("mode");
  if (!st) return <p className="loading">Yükleniyor…</p>;
  const refresh = () => { void mutate(); };
  const svc = st.services.find((s) => s.id === sec);
  const item = (id: Sec, label: string, right: React.ReactNode) => (
    <li key={id}><button type="button" aria-current={sec === id} onClick={() => setSec(id)}>{label}<span className="count">{right}</span></button></li>
  );
  return (
    <div className="page three">
      <Panel title="Ayarlar" meta="ücretli servisler">
        <div className="side-h">Mod</div>
        <ul className="list">{item("mode", "Çalışma modu", <span className={`st ${st.dry_run ? "ok" : "bad"}`}>{st.dry_run ? "ücretsiz" : "ücretli"}</span>)}</ul>
        <div className="side-h">Servisler</div>
        <ul className="list">
          {st.services.map((s) => item(s.id, s.name, <span className={`st ${s.key_set ? (s.active ? "ok" : "info") : "bad"}`}>{s.key_set ? (s.active ? "kullanımda" : "hazır") : "anahtar yok"}</span>))}
        </ul>
        <div className="side-h">Bütçe</div>
        <ul className="list">
          {item("pricing", "Fiyat tablosu", "yaml")}
          {item("audit", "Değişiklik kaydı", st.audit.length)}
        </ul>
        <p className="pad faint small">
          {st.editable ? <>Ayarlar <span className="mono">{st.env_file}</span> dosyasına yazılır ve servisler yeniden başlatılmadan geçerli olur. Bu dosya git'e girmez.</>
            : "Ayar dosyası bağlı değil: ayarlar yalnızca görüntülenir. docker-compose.yml'de ./.env bağlantısını kontrol edin."}
        </p>
      </Panel>

      {sec === "mode" ? <ModeView st={st} onSaved={refresh} />
        : svc ? <ServiceView key={svc.id} svc={svc} st={st} prices={prices?.table} onSaved={refresh} />
        : sec === "pricing" ? <PricingView onSaved={refresh} />
        : (
          <Panel title="Değişiklik kaydı" meta="anahtar değerleri kaydedilmez">
            <ol className="ev">
              {st.audit.length ? st.audit.map((a, i) => (
                <li key={`${a.ts}-${i}`}><time>{clock(a.ts)}</time><span><span className="mono faint">{a.field}</span> · {a.detail}</span></li>
              )) : <li><span /><span className="faint">Henüz değişiklik yok.</span></li>}
            </ol>
          </Panel>
        )}

      <Panel title="Özet" meta={busy ? "kaydediliyor…" : ""}>
        <div className="kpis" style={{ gridTemplateColumns: "1fr 1fr" }}>
          <div className="kpi"><b>{st.dry_run ? "Ücretsiz" : "Ücretli"}</b><span>Çalışma modu</span></div>
          <div className="kpi"><b>{usdExact(st.spent_usd)}</b><span>Gerçek harcama</span><small>bütün projeler, silinenler dahil</small></div>
          <div className="kpi"><b>{usdExact(st.virtual_usd)}</b><span>Sanal harcama</span><small>ücretsiz modda; para harcanmadı</small></div>
        </div>
        <table className="tbl">
          <thead><tr><th>Servis</th><th>Anahtar</th><th className="r">Gerçek</th><th className="r">Sanal</th></tr></thead>
          <tbody>
            {st.services.map((s) => (
              <tr key={s.id}><td>{s.name}</td><td><span className={`st ${s.key_set ? "ok" : "bad"}`}>{s.key_set ? "tanımlı" : "yok"}</span></td>
                <td className="r mono">{usdExact(s.spent_usd)}</td><td className="r mono faint">{usdExact(s.virtual_usd)}</td></tr>
            ))}
          </tbody>
        </table>
        <div className="pad row">
          <button className="btn" type="button" onClick={() => setHistory({}, true)}><Icon.receipt />Harcama geçmişi</button>
          <span className="faint small">Her çağrı: servis, model, istek kimliği, tutar ve hata.</span>
        </div>
        <div className="pad stack">
          <NoteBox id="settings" />
          <p className="faint small">Anahtarlar panelde yalnızca maskeli görünür ve hiçbir yanıtta ya da kayıtta açık yazılmaz. Bağlantı sınaması üretim yapmaz ve ücret doğurmaz.</p>
        </div>
      </Panel>
    </div>
  );
}
