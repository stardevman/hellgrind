"use client";
/* Varlıklar sayfası (yapım takip yazılımları gibi ana-ayrıntı düzeni): solda kütüphane süzgeçleri, ortada varlık
   ızgarası, sağda seçili varlığın onay akışı: referans, durağan onayı, hareket testi, kilit. Görseller kırpılmadan
   sığdırılır; referansa tıklanınca tam boyu açılır ve indirilebilir. Dizi bölümünde kapsam süzgeci (Dizi kütüphanesi / Bu
   bölüm) gelir: dizi varlığı bölüme kopyalanmaz, bağlanır; kütüphanede yeni sürümü olan varlık için geçişin etkisi önce
   gösterilir. */
import { useEffect, useRef, useState, type FormEvent } from "react";
import useSWR from "swr";
import { api, fetcher } from "@/lib/api";
import { ANOMALY_TR, ASTATUS_ORDER, ASTATUS_TR, REF_ACCEPT, REF_KINDS, REF_TR, TEST_TR, TYPE_TR } from "@/lib/i18n";
import { POLL_MS, usePanel } from "@/lib/panel";
import { dispatchText, lowerTr, usd } from "@/lib/text";
import { inScope, SCOPE_TR, upgradeText, type AssetScope } from "@/lib/series";
import type { Asset, DispatchAll, UpgradeImpact } from "@/lib/types";
import { LineUp } from "./Broadcast";
import { Icon } from "./icons";
import { NoteBox, Panel, Section, reducedMotion } from "./ui";

const TONE: Record<string, string> = { draft: "bad", still_approved: "warn", motion_tested: "info", locked: "ok" };

const thumbOf = (a: Asset) => a.references.still?.thumb_url ?? a.references.turnaround?.frame_urls?.[0]
  ?? a.references.expressions?.thumb_url ?? a.motion_test?.thumb_url ?? null;

interface Shown { src: string; name: string }

/** İndirilecek dosyanın adı: "Ada - Durağan görsel.png" (uzantı depo anahtarından). */
function fileName(src: string, name: string): string {
  const key = new URLSearchParams(src.split("?")[1] ?? "").get("key") ?? "";
  const ext = /\.[a-z0-9]{2,5}$/i.exec(key)?.[0] ?? ".jpg";
  return `${name.replace(/[\\/:*?"<>|]+/g, "-")}${ext}`;
}

/** Tam boy görsel: ekranı kaplayan karartma, kırpılmamış görsel, indir ve yeni sekmede aç. Esc ya da dışarı tıklama kapatır. */
function Lightbox({ shown, onClose }: { shown: Shown; onClose: () => void }) {
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    close.current?.focus();
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [onClose]);
  return (
    <div className="lightbox" role="dialog" aria-modal="true" aria-label={shown.name} onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="lb-bar">
        <span className="grow">{shown.name}</span>
        <a className="btn" href={shown.src} download={fileName(shown.src, shown.name)}><Icon.download />İndir</a>
        <a className="btn ghost" href={shown.src} target="_blank" rel="noreferrer">Yeni sekmede aç</a>
        <button ref={close} type="button" className="btn ghost" onClick={onClose}>Kapat</button>
      </div>
      <img src={shown.src} alt={shown.name} onMouseDown={(e) => e.stopPropagation()} />
    </div>
  );
}

/** Tıklanınca tam boyunu açan küçük görsel. */
function Zoomable({ thumb, full, name, className, onOpen }: { thumb: string; full?: string | null; name: string; className?: string; onOpen: (s: Shown) => void }) {
  return (
    <button type="button" className={`zoom ${className ?? ""}`.trim()} title="Tam boyutu aç" onClick={() => onOpen({ src: full || thumb, name })}>
      <img src={thumb} alt={name} loading="lazy" />
    </button>
  );
}

function Pipeline({ a }: { a: Asset }) {
  const rank = ASTATUS_ORDER.indexOf(a.status);
  const hasVisual = ["still", "turnaround", "expressions"].some((k) => a.references?.[k]);
  const steps: [string, boolean][] = [["Referans", hasVisual], ["Durağan", rank >= 1], ["Hareket", rank >= 2], ["Kilit", rank >= 3]];
  return <ol className="pipeline" aria-label="Onay adımları">{steps.map(([l, d]) => <li key={l} className={d ? "done" : ""}>{l}</li>)}</ol>;
}

type Act = "approve-still" | "motion-test" | "approve-motion" | "lock" | "unlock" | "placeholder" | "still" | "turnaround";

/** Her eylem kendi uç noktasını çağırır ve kullanıcıya gösterilecek notu döndürür. `pid`: açık bölüm; dizi varlığının
    üretim bedeli, ayarları ve stili bu bölümden gelir. */
const ASSET_ACTS: Record<Act, (id: string, pid: string) => Promise<string>> = {
  "approve-still": async (id) => { await api.post(`/assets/${id}/approve-still`); return "Durağan referans onaylandı. Sıradaki adım: hareket testi."; },
  "motion-test": async (id, pid) => { await api.post(`/assets/${id}/motion-test?project_id=${pid}`); return "Hareket testi kuyruğa alındı; bitince burada izleyebilirsin."; },
  "approve-motion": async (id) => { await api.post(`/assets/${id}/approve-motion`); return "Hareket testi onaylandı. Artık kilitleyebilirsin."; },
  lock: async (id, pid) => {
    const r = await api.post<{ unblocked: string[] }>(`/assets/${id}/lock?project_id=${pid}`);
    return r.unblocked.length ? `Kilitlendi. Engeli kalkan çekimler: ${r.unblocked.join(", ")}. Üretmek için "Engellenen çekimleri yeniden dene"ye bas.` : "Kilitlendi.";
  },
  unlock: async (id) => { await api.post(`/assets/${id}/unlock`); return "Kilit açıldı: referans değiştirilebilir."; },
  placeholder: async (id, pid) => {
    const r = await api.post<{ version: number }>(`/assets/${id}/references/placeholder?project_id=${pid}`);
    return `Yerel taslak referans üretildi (sürüm ${r.version}). Varlık taslakta; durağanı onayla.`;
  },
  still: async (id, pid) => {
    const r = await api.post<{ version: number }>(`/assets/${id}/references/placeholder?kind=still&project_id=${pid}`);
    return `Durağan referans üretildi (Qwen-Image, sürüm ${r.version}); eskisi geçmişte. Durağanı onayla.`;
  },
  turnaround: async (id, pid) => {
    const r = await api.post<{ version: number }>(`/assets/${id}/references/placeholder?kind=turnaround&project_id=${pid}`);
    return `Dönüş videosu üretildi (MiniMax H3, sürüm ${r.version}).`;
  },
};

/** Dizi varlığının bölüm bağı: kütüphanede ama bölüme bağlı değilse "Bu bölüme bağla", bağlıysa "Bağı kaldır". */
function SeriesLink({ a }: { a: Asset }) {
  const { busy, act, note, projectId } = usePanel();
  if (!a.series_id) return null;
  const linked = a.link !== null;
  const run = () => act(async () => {
    if (linked) await api.del(`/projects/${projectId}/series-assets/${a.id}`);
    else await api.post(`/projects/${projectId}/series-assets/${a.id}`);
    note("assets", linked ? `${a.name} bu bölümden çözüldü; dizi kütüphanesinde duruyor.` : `${a.name} bu bölüme bağlandı (kopyalanmadı); son sürümü izler.`, "ok");
  }, "assets");
  return (
    <div className="note small">
      Dizi kütüphanesi varlığı: tarif ve referans değişiklikleri bütün bölümlere yansır. Görüntüsü üretilmiş bölümler eski
      sürümde sabit kalır ve yeni sürüme geçişin etkisini önce görür.
      {a.link?.behind && <> Bu bölüm v{a.link.pinned} sürümünde sabit.</>}
      <div className="row mt8">
        <button className="btn" type="button" disabled={busy} onClick={run}>{linked ? "Bağı kaldır" : "Bu bölüme bağla"}</button>
      </div>
    </div>
  );
}

/** Kütüphanede yeni sürümü olan dizi varlıkları: bölüm eski sürüme sabit. Önce etkisi (yeniden üretilecek çekimler ve
    tahmini sanal maliyet) gösterilir, sonra yönetmen geçer; isterse etkilenen çekimler hemen yeniden üretilir. */
function SeriesUpdates() {
  const { ov, projectId, busy, frozen, act, note } = usePanel();
  const [impact, setImpact] = useState<Record<string, UpgradeImpact>>({});
  const ups = ov?.project.series_updates ?? [];
  const locked = Boolean(ov?.project.episode?.locked);
  if (!ups.length) return null;
  const preview = (id: string) => act(async () => {
    const x = await api.get<UpgradeImpact>(`/projects/${projectId}/series-assets/${id}/upgrade`);
    setImpact((m) => ({ ...m, [id]: x }));
  }, "assets");
  const apply = (id: string, render: boolean) => act(async () => {
    const r = await api.post<{ impact: UpgradeImpact; planned: Record<string, string[]> }>(`/projects/${projectId}/series-assets/${id}/upgrade`, { render });
    const n = r.impact.affected.length;
    note("assets", `${r.impact.name} v${r.impact.current} sürümüne geçildi. ` + (!n ? "Yeniden üretilecek çekim yok."
      : render ? `${Object.keys(r.planned).length} çekim kuyruğa alındı.` : `Etkilenen ${n} çekim yeniden üretilince yeni sürümü kullanır.`), "ok");
    setImpact((m) => { const c = { ...m }; delete c[id]; return c; });
  }, "assets");
  return (
    <div data-tour="series-upgrade">
      <div className="side-h">Yeni sürüm var</div>
      {ups.map((u) => {
        const x = impact[u.asset_id];
        return (
          <div className="series-up" key={u.asset_id}>
            <p className="small"><b>{u.name}</b>: dizi kütüphanesinde v{u.current}, bu bölüm v{u.pinned} sürümüne sabit. Geçilmeden bu varlıkla yeni görüntü üretilmez.</p>
            {x && <p className="small dim">{upgradeText(x)}</p>}
            {locked ? <p className="small dim">Bölüm teslim edildi (sürüm sabit): geçmek için önce yönetici kilidi açmalı.</p> : <div className="row">
              <button className="btn" type="button" disabled={busy} onClick={() => preview(u.asset_id)}>{x ? "Etkiyi yenile" : "Etkisini gör"}</button>
              {x && <button className="btn primary" type="button" disabled={busy} onClick={() => apply(u.asset_id, false)}>Yeni sürüme geç</button>}
              {x && x.affected.length > 0 && (
                <button className="btn" type="button" disabled={busy || frozen} onClick={() => apply(u.asset_id, true)}>Geç ve yeniden üret</button>
              )}
            </div>}
          </div>
        );
      })}
    </div>
  );
}

function Detail({ a }: { a: Asset }) {
  const { busy, frozen, act, note, meta, projectId } = usePanel();
  const [kind, setKind] = useState(REF_KINDS[a.type][0]);
  const [shown, setShown] = useState<Shown | null>(null);
  const hasVisual = ["still", "turnaround", "expressions"].some((k) => a.references?.[k]);
  const t = a.motion_test, locked = a.status === "locked";
  const testable = meta?.motion_test !== false;         // ücretsiz modda hareket testi yok: durağan onaydan kilit
  const running = Boolean(t && ["queued", "running"].includes(t.status));
  const run = (x: Act) => act(async () => note("assets", await ASSET_ACTS[x](a.id, projectId ?? ""), "ok"), "assets");
  const upload = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const form = e.currentTarget;
    const file = (form.elements.namedItem("file") as HTMLInputElement).files?.[0];
    if (!file) { note("assets", "Önce bir dosya seç.", "err"); return; }
    void act(async () => {
      const r = await api.upload<{ name: string; version: number; status: string }>(
        `/assets/${a.id}/references?kind=${encodeURIComponent(kind)}&filename=${encodeURIComponent(file.name)}`, file);
      form.reset();
      note("assets", `${REF_TR[kind]} yüklendi: ${r.name}, sürüm ${r.version}, ${lowerTr(ASTATUS_TR[r.status])}.`, "ok");
    }, "assets");
  };
  const refs = REF_KINDS[a.type].filter((k) => a.references[k]);
  const ch = a.character;
  const setKind2 = (character_kind: string) => act(async () => {
    await api.patch(`/assets/${a.id}`, { character_kind });
    note("assets", character_kind === "non_human"
      ? `${a.name}: insan değil. Kimlik yüzle değil görsel benzerlikle (OpenCLIP) ölçülür; "yüz görünmedi" eleme sebebi olmaz.`
      : `${a.name}: insan. Kimlik yüzle (SFace) ölçülür.`, "ok");
  }, "assets");
  const history = a.reference_history ?? [];
  const [inv, setInv] = useState((a.invariants ?? []).join("\n"));
  const saveInv = () => act(async () => {
    const r = await api.patch<{ version: number }>(`/assets/${a.id}`, { invariants: inv.split("\n").map((x) => x.trim()).filter(Boolean) });
    note("assets", `Değişmezler kaydedildi (sürüm ${r.version}); her kare ve video istemine eklenir. Çekimler yeniden üretilince uygulanır.`, "ok");
  }, "assets");
  const [scale, setScale] = useState(a.scale ?? "");
  const saveScale = () => act(async () => {
    const r = await api.patch<{ version: number }>(`/assets/${a.id}`, { scale: scale.trim() });
    note("assets", scale.trim() ? `Gerçek boyut kaydedildi (sürüm ${r.version}): istemlere girer, karede bu boyutta çizilir ve ölçek kapısıyla denetlenir.`
      : `Gerçek boyut kaldırıldı (sürüm ${r.version}).`, "ok");
  }, "assets");
  const bad = t && (t.status === "rejected" || t.status === "failed");
  return (
    <section className="pnl" aria-label={`${a.name} (${TYPE_TR[a.type]})`}>
      <header className="pnl-h">
        <h2>{a.name}</h2><span className="meta">{TYPE_TR[a.type]} · sürüm {a.version} · {a.effective_style}</span>
        <span className="grow" />{a.series_id && <span className="tag series" title="Dizi kütüphanesi varlığı">Dizi</span>}
        <span className={`tag ${TONE[a.status]}`}>{ASTATUS_TR[a.status]}</span>
      </header>
      <div className="pnl-b">
        <div className="pad stack">
          <Pipeline a={a} />
          <SeriesLink a={a} />
          {a.description && <p>{a.description}</p>}
          {a.type !== "location" && (
            <label className="field">Değişmez özellikler (her satır bir özellik; her kare ve video istemine eklenir)
              <textarea rows={3} value={inv} disabled={busy} aria-label="Değişmez özellikler" onChange={(e) => setInv(e.target.value)}
                placeholder="ör. exactly one bent antenna with a small bulb" />
              <div className="row mt8"><button className="btn" type="button" disabled={busy || inv === (a.invariants ?? []).join("\n")} onClick={saveInv}>Değişmezleri kaydet</button></div>
            </label>
          )}
          {a.type !== "location" && (
            <label className="field">Gerçek boyut (referans görseli yakın plan olsa da karede bu boyutta çizilir)
              <input value={scale} disabled={busy} aria-label="Gerçek boyut" onChange={(e) => setScale(e.target.value)}
                placeholder="ör. tiny, palm-sized; much smaller than a hen" maxLength={200} />
              <div className="row mt8"><button className="btn" type="button" disabled={busy || scale.trim() === (a.scale ?? "")} onClick={saveScale}>Boyutu kaydet</button></div>
            </label>
          )}
          {ch && (
            <label className="field">Karakter türü
              <select value={ch.kind} disabled={busy} onChange={(e) => setKind2(e.target.value)} aria-label="Karakter türü">
                <option value="human">İnsan: kimlik yüzle (SFace)</option>
                <option value="non_human">İnsan değil (robot, hayvan, yaratık): görsel benzerlik (OpenCLIP)</option>
              </select>
              {ch.kind !== ch.kind_suggested && (
                <small className="warn">Öneri: tarife göre {ch.kind_suggested === "non_human" ? "insan değil" : "insan"}.</small>
              )}
            </label>
          )}
          <div className="row">
            {a.status === "draft" && <button className="btn good" type="button" disabled={busy || !hasVisual} onClick={() => run("approve-still")}>Durağanı onayla</button>}
            {testable && (a.status === "still_approved" || a.status === "motion_tested") && (
              <button className="btn" type="button" disabled={busy || frozen || running} onClick={() => run("motion-test")}>{t ? "Hareket testini yenile" : "Hareket testi üret"}</button>
            )}
            {a.status === "still_approved" && t?.status === "passed" && t.current && (
              <button className="btn good" type="button" disabled={busy} onClick={() => run("approve-motion")}>Hareket testini onayla</button>
            )}
            {(a.status === "motion_tested" || (!testable && a.status === "still_approved")) && <button className="btn primary" type="button" disabled={busy} onClick={() => run("lock")}>Kilitle</button>}
            {locked && <button className="btn" type="button" disabled={busy} onClick={() => run("unlock")}>Kilidi aç</button>}
          </div>
          <NoteBox id="assets" />
        </div>
        <Section title="Referanslar" meta={refs.length ? `${refs.length}` : "yok"}>
          {refs.length ? (
            <div className="refs">
              {refs.map((k) => {
                const r = a.references[k];
                if (k === "turnaround") return <div className="ref" key={k}><span>{REF_TR[k]} · {r.frames?.length ?? 0} kare, ilki yüz referansı</span><div className="frames">{(r.frame_urls ?? []).map((u, i) => <Zoomable key={u} thumb={u} name={`${a.name} - ${REF_TR[k]} ${i + 1}`} onOpen={setShown} />)}</div></div>;
                if (k === "voice_sample") return <div className="ref" key={k}><span>{REF_TR[k]}</span><audio controls preload="none" src={r.url ?? undefined} /></div>;
                return <div className="ref" key={k}><span>{REF_TR[k]}</span>{r.thumb_url && <Zoomable className="big" thumb={r.thumb_url} full={r.url} name={`${a.name} - ${REF_TR[k]}`} onOpen={setShown} />}</div>;
              })}
            </div>
          ) : <p className="faint">Henüz referans yok.</p>}
          {history.length > 0 && (
            <details>
              <summary className="small">Referans geçmişi ({history.length})</summary>
              <div className="refs">
                {history.slice().reverse().map((h) => (
                  <div className="ref" key={`${h.kind}-${h.replaced_at}`}>
                    <span>{REF_TR[h.kind] ?? h.kind} · sürüm {h.version}</span>
                    {h.entry.thumb_url && <Zoomable thumb={h.entry.thumb_url} full={h.entry.url} name={`${a.name} - ${REF_TR[h.kind] ?? h.kind} sürüm ${h.version}`} onOpen={setShown} />}
                  </div>
                ))}
              </div>
            </details>
          )}
          {locked ? <p className="faint small">Kilitli varlık değiştirilemez; önce kilidi aç.</p> : (
            <form className="form-row" onSubmit={upload}>
              <label className="field">Referans türü
                <select value={kind} onChange={(e) => setKind(e.target.value)}>
                  {REF_KINDS[a.type].map((k) => <option key={k} value={k}>{REF_TR[k]}</option>)}
                </select>
              </label>
              <label className="field">Dosya<input type="file" name="file" accept={REF_ACCEPT[kind]} required /></label>
              <button className="btn" type="submit" disabled={busy}>Yükle</button>
              {meta && !meta.dry_run ? (
                <>
                  <button className="btn" type="button" disabled={busy || frozen} onClick={() => run("still")}
                    title="Qwen-Image-2512, RunPod: yüksek çözünürlüklü durağan görsel (karakter 1328x1328, düz arka plan, tam boy, yüz net)">Durağan referans üret (ücretli)</button>
                  {a.type === "character" && (
                    <button className="btn" type="button" disabled={busy || frozen || !a.references.still} onClick={() => run("turnaround")}
                      title="İsteğe bağlı ikinci adım: MiniMax H3 dönüş videosu, durağan görselden başlar">Dönüş videosu üret (ücretli)</button>
                  )}
                </>
              ) : (
                <button className="btn" type="button" disabled={busy || frozen} onClick={() => run("placeholder")}>Yerel taslak üret</button>
              )}
            </form>
          )}
        </Section>
        {testable && <Section title="Hareket testi" meta={!t ? "yok" : ({ passed: "temiz", rejected: "kusurlu", failed: "üretilemedi" } as Record<string, string>)[t.status] ?? "sürüyor"}>
          {!t ? <p className="faint">{a.status === "draft" ? "Önce durağanı onayla." : "Henüz hareket testi yok."}</p> : (
            <div className="motion stack">
              {t.video_url && <video controls preload="none" playsInline poster={t.thumb_url ?? undefined} src={t.video_url} />}
              <p className={bad ? "warn" : "dim"}>
                {(t.status === "rejected" ? `Kusurlu: ${t.anomalies.map((x) => ANOMALY_TR[x] || x).join(", ")}. Yeniden üret.` : TEST_TR[t.status] || t.status)
                  + (t.current ? "" : " Bu test eski bir referansa ait.")}{t.cost_usd ? ` · ${usd(t.cost_usd)}` : ""}
              </p>
            </div>
          )}
        </Section>}
        <Section title="Kullanım" meta={`${a.scenes.length} çekim`} open={false}>
          <p className="small">{a.scenes.length ? "Kullanıldığı çekimler: " + a.scenes.join(", ") : "Henüz bir çekimde kullanılmıyor."}</p>
          {a.character?.voice_id && <p className="small dim">Ses kimliği {a.character.voice_id}</p>}
        </Section>
      </div>
      {shown && <Lightbox shown={shown} onClose={() => setShown(null)} />}
    </section>
  );
}

export function AssetsPage() {
  const { ov, projectId, busy, frozen, act, note, assetId, selectAsset, flashAsset, clearFlash, gotoAsset } = usePanel();
  const { data: assets } = useSWR<Asset[]>(projectId ? `/projects/${projectId}/assets` : null, fetcher, { refreshInterval: POLL_MS });
  const episode = Boolean(ov?.project.episode?.series);
  const { data: library } = useSWR<{ assets: Asset[] }>(projectId && episode ? `/projects/${projectId}/series-library` : null, fetcher, { refreshInterval: POLL_MS });
  const [scope, setScope] = useState<AssetScope>("");
  const [type, setType] = useState("");
  const [status, setStatus] = useState("");
  const [newType, setNewType] = useState("prop");
  const [newName, setNewName] = useState("");
  const [view, setView] = useState<"cards" | "lineup">("cards");
  const cards = useRef<Record<string, HTMLButtonElement | null>>({});
  useEffect(() => { if (flashAsset) { setType(""); setStatus(""); setScope(""); } }, [flashAsset]);   // hedef kart görünsün
  useEffect(() => {
    if (!flashAsset || !assets?.some((a) => a.id === flashAsset)) return;
    cards.current[flashAsset]?.scrollIntoView({ block: "nearest", behavior: reducedMotion() ? "auto" : "smooth" });
    const t = setTimeout(clearFlash, 2400);
    return () => clearTimeout(t);
  }, [flashAsset, assets, clearFlash]);

  const add = (e: FormEvent) => {
    e.preventDefault();
    void act(async () => {
      const toSeries = scope === "series";
      const body = { type: newType, name: newName };
      const a = toSeries ? await api.post<Asset>(`/projects/${projectId}/series-library`, body) : await api.post<Asset>(`/projects/${projectId}/assets`, body);
      setNewName("");
      gotoAsset(a.id);
      note("assets", toSeries ? `${a.name} dizi kütüphanesine eklendi ve bu bölüme bağlandı (taslak).` : `${a.name} eklendi (taslak).`, "ok");
    }, "assets");
  };
  const retry = () => act(async () => {
    const r = await api.post<DispatchAll>(`/projects/${projectId}/render-all`);
    const x = dispatchText(r);
    note("assets", x.text, x.bad ? "err" : "ok");
  }, "assets");

  const all = scope === "series" ? (library?.assets ?? []) : (assets ?? []).filter((a) => inScope(a, scope));
  const need = ov?.project.settings.asset_min_status ?? "locked";
  const list = all.filter((a) => (!type || a.type === type) && (!status || a.status === status));
  const ready = all.filter((a) => ASTATUS_ORDER.indexOf(a.status) >= ASTATUS_ORDER.indexOf(need)).length;
  const selected = all.find((a) => a.id === assetId) ?? (assets ?? []).find((a) => a.id === assetId) ?? list[0];
  const blocked = ov?.scenes.filter((x) => x.status === "blocked").length ?? 0;
  const count = (f: (a: Asset) => boolean) => all.filter(f).length;
  return (
    <div className="page three" data-tour="assets">
      <Panel title="Kütüphane" meta={assets ? `${ready}/${all.length} hazır` : ""}>
        <p className="pad faint small">
          Çekimde geçen her karakter, mekân ve nesnenin onaylı referansı olmadan görüntü üretilmez. Gereken en az durum: {ASTATUS_TR[need]}.
        </p>
        {blocked > 0 && !ov?.project.episode?.locked && (
          <div className="pad" style={{ paddingTop: 0 }}>
            <button className="btn" type="button" style={{ width: "100%" }} disabled={busy || frozen} onClick={retry}>Engellenen çekimleri yeniden dene ({blocked})</button>
          </div>
        )}
        <SeriesUpdates />
        {episode && (
          <div data-tour="assets-scope">
            <div className="side-h">Kapsam</div>
            <ul className="list">
              {(Object.keys(SCOPE_TR) as AssetScope[]).map((k) => (
                <li key={k}><button type="button" aria-current={scope === k} onClick={() => setScope(k)}
                  title={k === "series" ? "Dizinin bütün bölümlerinde kullanılabilen varlıklar (bu bölüme bağlı olmayanlar dahil)" : k === "local" ? "Yalnızca bu bölümde görünen varlıklar" : "Bu bölümün varlıkları: bölüme özel olanlar ve bağlı dizi varlıkları"}>
                  {SCOPE_TR[k]}<span className="count">{k === "series" ? library?.assets.length ?? "" : (assets ?? []).filter((a) => inScope(a, k)).length}</span>
                </button></li>
              ))}
            </ul>
          </div>
        )}
        <div className="side-h">Tür</div>
        <ul className="list">
          {[["", "Tümü"], ...Object.entries(TYPE_TR)].map(([k, v]) => (
            <li key={k}><button type="button" aria-current={type === k} onClick={() => setType(k)}>{v}<span className="count">{k ? count((a) => a.type === k) : all.length}</span></button></li>
          ))}
        </ul>
        <div className="side-h">Durum</div>
        <ul className="list">
          {[["", "Tümü"], ...ASTATUS_ORDER.map((k) => [k, ASTATUS_TR[k]])].map(([k, v]) => (
            <li key={k}><button type="button" aria-current={status === k} onClick={() => setStatus(k)}>
              {k ? <span className={`st ${TONE[k]}`}>{v}</span> : v}<span className="count">{k ? count((a) => a.status === k) : all.length}</span>
            </button></li>
          ))}
        </ul>
        <div className="side-h">{scope === "series" ? "Dizi kütüphanesine ekle" : "Yeni varlık"}</div>
        <form className="pad stack" style={{ paddingTop: 4 }} onSubmit={add}>
          <select aria-label="Yeni varlık türü" value={newType} onChange={(e) => setNewType(e.target.value)}>
            <option value="prop">Nesne</option><option value="location">Mekân</option><option value="character">Karakter</option>
          </select>
          <input type="text" aria-label="Ad" placeholder="Ad" maxLength={100} required value={newName} onChange={(e) => setNewName(e.target.value)} />
          <button className="btn" type="submit" disabled={busy}>Ekle</button>
        </form>
      </Panel>

      <Panel title="Varlıklar" meta={assets ? (view === "lineup" ? `${all.filter((a) => a.type === "character").length} karakter` : `${list.length} gösteriliyor`) : ""}
        tools={<div className="seg" role="group" aria-label="Görünüm">
          <button type="button" aria-pressed={view === "cards"} onClick={() => setView("cards")}>Kartlar</button>
          <button type="button" aria-pressed={view === "lineup"} onClick={() => setView("lineup")} title="Karakterlerin boy sıralaması (line-up)">Boy sıralaması</button>
        </div>}>
        {!assets ? <p className="loading">Yükleniyor…</p> : view === "lineup" ? <LineUp assets={all} onPick={selectAsset} /> : list.length ? (
          <div className="grid-cards">
            {list.map((a) => {
              const th = thumbOf(a);
              return (
                <button key={a.id} ref={(el) => { cards.current[a.id] = el; }} type="button" className={`card${flashAsset === a.id ? " flash" : ""}`}
                  aria-current={selected?.id === a.id} onClick={() => selectAsset(a.id)} aria-label={`${a.name} (${TYPE_TR[a.type]})`}>
                  {th ? <img className="th fit" src={th} alt="" loading="lazy" /> : <span className="th">Referans yok</span>}
                  <span className="row"><h3 className="grow">{a.name}</h3>
                    {a.series_id && <span className={`tag series`} title={a.link === null ? "Dizi kütüphanesinde; bu bölüme bağlı değil" : "Dizi kütüphanesi varlığı"}>{a.link === null ? "Dizi · bağlı değil" : "Dizi"}</span>}
                    <span className={`tag ${TONE[a.status]}`}>{ASTATUS_TR[a.status]}</span></span>
                  <span className="faint small">{TYPE_TR[a.type]} · sürüm {a.version} · {a.scenes.length} çekim</span>
                  <Pipeline a={a} />
                </button>
              );
            })}
          </div>
        ) : <p className="empty">Bu filtreye uyan varlık yok.</p>}
      </Panel>

      {selected ? <Detail key={selected.id} a={selected} /> : <Panel title="Ayrıntı"><p className="empty">Varlık seçin.</p></Panel>}
    </div>
  );
}
