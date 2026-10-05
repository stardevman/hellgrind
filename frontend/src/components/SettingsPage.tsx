"use client";
/* Ayarlar: ücretli servislerin bağlantıları (anahtar, model ve uç noktalar, motor seçimi), ücretsiz/ücretli mod ve
   fiyat tablosu. Her şey .env (fiyatlar pricing.yaml) dosyasına yazılır ve servisler yeniden başlatılmadan geçerli
   olur. Anahtarlar yalnızca maskeli görünür; ücretli moda geçiş açık onay ister. */
import { useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { api, fetcher } from "@/lib/api";
import { POLL_MS, usePanel } from "@/lib/panel";
import { clock, usd, usdExact } from "@/lib/text";
import type { PricingTable, ProbeResult, Service, ServiceField, SettingsState } from "@/lib/types";
import { Icon } from "./icons";
import { NoteBox, Panel, Section } from "./ui";

type Sec = "mode" | Service["id"] | "pricing" | "audit";

const tone = (ok: boolean | null) => (ok === true ? "ok" : ok === false ? "bad" : "warn");

/** Servisin fiyat satırları (pricing.yaml'dan; rezervasyonlar bu fiyatlarla yapılır). */
function priceRows(id: Service["id"], t: PricingTable | undefined): [string, string][] {
  if (!t) return [];
  if (id === "elevenlabs") return [["Konuşma (1000 karakter)", usd(t.tts_per_1k_chars)]];
  if (id === "anthropic") return [["Girdi (1M token)", usd(t.llm_per_million_tokens.input)], ["Çıktı (1M token)", usd(t.llm_per_million_tokens.output)]];
  const rows: [string, string][] = [];
  for (const [engine, byRes] of Object.entries(t.video_per_second)) {
    for (const [res, price] of Object.entries(byRes)) rows.push([`${engine} · ${res} (video saniyesi)`, `$${price}`]);
  }
  rows.push(["flux_lora (megapiksel)", `$${t.image_per_megapixel.flux_lora}`], ["Dudak senkronu (dakika)", `$${t.lipsync_per_minute}`]);
  return rows;
}

function KeyBlock({ svc, field, editable, onSaved }: { svc: Service; field: ServiceField; editable: boolean; onSaved: () => void }) {
  const { busy, act, note } = usePanel();
  const [value, setValue] = useState("");
  const [sure, setSure] = useState(false);
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const save = (v: string) => act(async () => {
    await api.patch<SettingsState>("/settings", { values: { [field.env]: v } });
    setValue(""); setSure(false); setProbe(null); onSaved();
    note("settings", v ? `${svc.name} anahtarı kaydedildi; bütün servislerde hemen geçerli.` : `${svc.name} anahtarı silindi.`, "ok");
  }, "settings");
  const test = () => act(async () => {
    const r = await api.post<ProbeResult>(`/settings/test/${svc.id}`);
    setProbe(r);
  }, "settings");
  return (
    <div className="stack">
      <div className="row">
        <span className="dim small" style={{ width: 92 }}>{field.label}</span>
        {field.set ? <span className="mono">{field.masked}</span> : <span className="tag bad">tanımlı değil</span>}
        <span className="grow" />
        <button className="btn" type="button" disabled={busy || !field.set} onClick={test} title="Ücretsiz bir doğrulama isteği; üretim yapmaz">Bağlantıyı sına</button>
        {probe && <span className={`tag ${tone(probe.ok)}`} title={probe.status ? `HTTP ${probe.status}` : ""}>{probe.message}</span>}
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
  const key = svc.fields.find((f) => f.kind === "secret")!;
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
        <KeyBlock svc={svc} field={key} editable={st.editable} onSaved={onSaved} />
      </div>
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
                  ) : (
                    <input type="text" className="mono" style={{ width: "100%" }} spellCheck={false} disabled={!st.editable} aria-label={f.label}
                      value={val(f)} onChange={(e) => setDraft({ ...draft, [f.env]: e.target.value })} />
                  )}
                  {f.default && val(f) !== f.default && <small>varsayılan: <span className="mono">{f.default}</span></small>}
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
      </Section>
    </Panel>
  );
}

function ModeView({ st, onSaved }: { st: SettingsState; onSaved: () => void }) {
  const { busy, act, note } = usePanel();
  const { mutate } = useSWRConfig();
  const [typed, setTyped] = useState("");
  const matches = typed.trim().toLocaleLowerCase("tr") === st.confirmation.toLocaleLowerCase("tr");
  const switchTo = (paid: boolean) => act(async () => {
    await api.post("/settings/mode", paid ? { paid, confirm: typed } : { paid });
    setTyped(""); onSaved(); void mutate("/meta");
    note("settings", paid ? "Ücretli mod açık: bundan sonraki üretimler gerçek servisleri çağırır ve gerçek harcama yapar." : "Ücretsiz moda dönüldü: hiçbir ücretli servis çağrılmaz.", paid ? "err" : "ok");
  }, "settings");
  const fal = st.services.find((s) => s.id === "fal");
  const missingPkgs = st.services.some((s) => (s.id === "fal" || s.needed) && Boolean(s.missing_packages?.length));
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
              {fal && !fal.key_set && <li className="warn">En az fal.ai anahtarı gerekir: görüntü ve video yalnızca fal.ai ile üretilir.</li>}
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
                  <button className="btn danger lg" type="submit" disabled={busy || !matches || st.busy.length > 0 || !fal?.key_set || !st.scorer.ready || missingPkgs}>Ücretli moda geç</button>
                  {(!fal?.key_set || !st.scorer.ready) && <span className="faint small">Önce fal.ai anahtarını girin{st.scorer.ready ? "" : " ve kalite skorlayıcısını hazırlayın"}.</span>}
                  {fal?.key_set && missingPkgs && <span className="faint small">Ücretli paketler imajda kurulu değil: docker compose up -d --build.</span>}
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
