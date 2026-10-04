"use client";
import { useEffect, useRef, useState, type FormEvent } from "react";
import useSWR from "swr";
import { api, fetcher } from "@/lib/api";
import { ANOMALY_TR, ASTATUS_ORDER, ASTATUS_TR, REF_ACCEPT, REF_KINDS, REF_TR, TEST_TR, TYPE_TR } from "@/lib/i18n";
import { POLL_MS, usePanel } from "@/lib/panel";
import { lowerTr, usd } from "@/lib/text";
import type { Asset } from "@/lib/types";
import { RetryBlocked } from "./ScenesPane";
import { NoteBox, reducedMotion } from "./ui";

function MotionTestBlock({ a }: { a: Asset }) {
  const t = a.motion_test;
  if (!t) return a.status === "draft" ? null : <p className="muted">Henüz hareket testi yok.</p>;
  const bad = t.status === "rejected" || t.status === "failed";
  const label = (t.status === "rejected" ? `Kusurlu: ${t.anomalies.map((x) => ANOMALY_TR[x] || x).join(", ")}. Yeniden üret.` : TEST_TR[t.status] || t.status)
    + (t.current ? "" : " Bu test eski bir referansa ait.");
  return (
    <div className="motion">
      {t.video_url && <video controls preload="none" playsInline poster={t.thumb_url ?? undefined} src={t.video_url} />}
      <p className={`mt6 ${bad ? "warn" : "muted"}`}>{label}{t.cost_usd ? ` · ${usd(t.cost_usd)}` : ""}</p>
    </div>
  );
}

function References({ a }: { a: Asset }) {
  const refs = a.references || {};
  const kinds = REF_KINDS[a.type].filter((k) => refs[k]);
  if (!kinds.length) return <p className="muted">Henüz referans yok.</p>;
  return (
    <div className="refs">
      {kinds.map((k) => {
        const r = refs[k];
        if (k === "turnaround") {
          return (
            <div className="ref" key={k}>
              <span>{REF_TR[k]} · {r.frames?.length ?? 0} kare, ilki yüz referansı</span>
              <div className="frames">{(r.frame_urls ?? []).map((u) => <img key={u} src={u} alt="" loading="lazy" />)}</div>
            </div>
          );
        }
        if (k === "voice_sample") {
          return <div className="ref" key={k}><span>{REF_TR[k]}</span><audio controls preload="none" src={r.url ?? undefined} /></div>;
        }
        return (
          <div className="ref" key={k}>
            <span>{REF_TR[k]}</span>
            {r.thumb_url && <img className="thumb" src={r.thumb_url} alt={`${a.name}: ${REF_TR[k]}`} loading="lazy" />}
          </div>
        );
      })}
    </div>
  );
}

type Act = "approve-still" | "motion-test" | "approve-motion" | "lock" | "unlock" | "placeholder";

/** Kart düğmeleri: her eylem kendi uç noktasını çağırır ve kullanıcıya gösterilecek notu döndürür. */
const ASSET_ACTS: Record<Act, (id: string) => Promise<string>> = {
  "approve-still": async (id) => { await api.post(`/assets/${id}/approve-still`); return "Durağan referans onaylandı. Sıradaki adım: hareket testi."; },
  "motion-test": async (id) => { await api.post(`/assets/${id}/motion-test`); return "Hareket testi kuyruğa alındı; bitince bu kartta izleyebilirsin."; },
  "approve-motion": async (id) => { await api.post(`/assets/${id}/approve-motion`); return "Hareket testi onaylandı. Artık kilitleyebilirsin."; },
  lock: async (id) => {
    const r = await api.post<{ unblocked: string[] }>(`/assets/${id}/lock`);
    return r.unblocked.length ? `Kilitlendi. Engeli kalkan çekimler: ${r.unblocked.join(", ")}. Üretmek için "Engellenen çekimleri yeniden dene"ye bas.` : "Kilitlendi.";
  },
  unlock: async (id) => { await api.post(`/assets/${id}/unlock`); return "Kilit açıldı: referans değiştirilebilir."; },
  placeholder: async (id) => {
    const r = await api.post<{ version: number }>(`/assets/${id}/references/placeholder`);
    return `Yerel taslak referans üretildi (sürüm ${r.version}). Varlık taslakta; durağanı onayla.`;
  },
};

function AssetCard({ a, flash }: { a: Asset; flash: boolean }) {
  const { busy, frozen, act, note, meta } = usePanel();
  const [kind, setKind] = useState(REF_KINDS[a.type][0]);
  const ref = useRef<HTMLElement>(null);
  const [lit, setLit] = useState(false);
  useEffect(() => {
    if (!flash || !ref.current) return;
    ref.current.scrollIntoView({ block: "start", behavior: reducedMotion() ? "auto" : "smooth" });
    ref.current.querySelector<HTMLButtonElement>("button:not([disabled])")?.focus({ preventScroll: true });
    setLit(true);
    const t = setTimeout(() => setLit(false), 2400);
    return () => clearTimeout(t);
  }, [flash]);

  const rank = ASTATUS_ORDER.indexOf(a.status);
  const hasVisual = ["still", "turnaround", "expressions"].some((k) => a.references?.[k]);
  const t = a.motion_test, locked = a.status === "locked";
  const run = (x: Act) => act(async () => note("assets", await ASSET_ACTS[x](a.id), "ok"), "assets");
  const steps: [string, boolean][] = [["Referans", hasVisual], ["Durağan", rank >= 1], ["Hareket", rank >= 2], ["Kilit", rank >= 3]];
  const running = Boolean(t && ["queued", "running"].includes(t.status));

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

  return (
    <article ref={ref} className={`card${lit ? " flash" : ""}`} id={`asset-${a.id}`} aria-label={`${a.name} (${TYPE_TR[a.type]})`}>
      <div className="card-head">
        <h3>{a.name}</h3><span className="muted">{TYPE_TR[a.type]} · sürüm {a.version}</span>
        <span className="spacer" /><span className={`astatus ${a.status}`}>{ASTATUS_TR[a.status]}</span>
      </div>
      <ol className="steps" aria-label="Onay adımları">
        {steps.map(([label, done]) => <li key={label} className={done ? "done" : ""}>{label}</li>)}
      </ol>
      {a.description && <p>{a.description}</p>}
      <References a={a} />
      <MotionTestBlock a={a} />
      <div className="actions-row">
        {a.status === "draft" && <button className="btn small good" type="button" disabled={busy || !hasVisual} onClick={() => run("approve-still")}>Durağanı onayla</button>}
        {(a.status === "still_approved" || a.status === "motion_tested") && (
          <button className="btn small" type="button" disabled={busy || frozen || running} onClick={() => run("motion-test")}>
            {t ? "Hareket testini yenile" : "Hareket testi üret"}
          </button>
        )}
        {a.status === "still_approved" && t?.status === "passed" && t.current && (
          <button className="btn small good" type="button" disabled={busy} onClick={() => run("approve-motion")}>Hareket testini onayla</button>
        )}
        {a.status === "motion_tested" && <button className="btn small primary" type="button" disabled={busy} onClick={() => run("lock")}>Kilitle</button>}
        {locked && <button className="btn small" type="button" disabled={busy} onClick={() => run("unlock")}>Kilidi aç</button>}
      </div>
      {locked ? <p className="muted">Kilitli varlık değiştirilemez; önce kilidi aç.</p> : (
        <form className="upload" onSubmit={upload}>
          <label>Referans türü
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              {REF_KINDS[a.type].map((k) => <option key={k} value={k}>{REF_TR[k]}</option>)}
            </select>
          </label>
          <label>Dosya<input type="file" name="file" accept={REF_ACCEPT[kind]} required /></label>
          <button className="btn small" type="submit" disabled={busy}>Yükle</button>
          <button className="btn small" type="button" disabled={busy || frozen} onClick={() => run("placeholder")}>
            {meta && !meta.dry_run ? "Referans üret (ücretli)" : "Yerel taslak üret"}
          </button>
        </form>
      )}
      <p className="muted small">
        {a.scenes.length ? "Kullanıldığı çekimler: " + a.scenes.join(", ") : "Henüz bir çekimde kullanılmıyor."}
        {a.character?.voice_id ? " · ses kimliği " + a.character.voice_id : ""}
      </p>
    </article>
  );
}

export function AssetsPane() {
  const { ov, projectId, busy, act, note, flashAsset, clearFlash, gotoAsset } = usePanel();
  const { data: assets } = useSWR<Asset[]>(projectId ? `/projects/${projectId}/assets` : null, fetcher, { refreshInterval: POLL_MS });
  const [type, setType] = useState("");
  const [status, setStatus] = useState("");
  const [newType, setNewType] = useState("prop");
  const [newName, setNewName] = useState("");
  // Eksik referans bağlantısından gelindiyse süzgeçler temizlenir ki hedef kart görünsün.
  useEffect(() => { if (flashAsset) { setType(""); setStatus(""); } }, [flashAsset]);
  useEffect(() => {
    if (!flashAsset || !assets?.some((a) => a.id === flashAsset)) return;
    const t = setTimeout(clearFlash, 2600);
    return () => clearTimeout(t);
  }, [flashAsset, assets, clearFlash]);

  const add = (e: FormEvent) => {
    e.preventDefault();
    void act(async () => {
      const a = await api.post<Asset>(`/projects/${projectId}/assets`, { type: newType, name: newName });
      setNewName("");
      gotoAsset(a.id);
      note("assets", `${a.name} eklendi (taslak).`, "ok");
    }, "assets");
  };

  const need = ov?.project.settings.asset_min_status ?? "locked";
  const list = (assets ?? []).filter((a) => (!type || a.type === type) && (!status || a.status === status));
  const ready = (assets ?? []).filter((a) => ASTATUS_ORDER.indexOf(a.status) >= ASTATUS_ORDER.indexOf(need)).length;
  return (
    <section className="panel" aria-labelledby="assets-title">
      <header>
        <h2 id="assets-title">Varlıklar</h2>
        {assets && <span className="muted">{ready}/{assets.length} hazır · gereken en az durum: {ASTATUS_TR[need]}</span>}
        <span className="spacer" />
        <RetryBlocked noteKey="assets" />
      </header>
      <div className="body">
        <p className="muted">
          Çekimde geçen her karakter, mekân ve nesnenin onaylı referansı olmadan görüntü üretilmez. Akış: referans yükle,
          durağanı onayla, hareket testi üret, testi onayla, kilitle.
        </p>
        <div className="filters mt12">
          <label>Tür
            <select value={type} onChange={(e) => setType(e.target.value)}>
              <option value="">Tümü</option>
              {Object.entries(TYPE_TR).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </label>
          <label>Durum
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">Tümü</option>
              {ASTATUS_ORDER.map((k) => <option key={k} value={k}>{ASTATUS_TR[k]}</option>)}
            </select>
          </label>
          <span className="spacer" />
          <form className="filters" onSubmit={add}>
            <label>Yeni varlık
              <select value={newType} onChange={(e) => setNewType(e.target.value)}>
                <option value="prop">Nesne</option><option value="location">Mekân</option><option value="character">Karakter</option>
              </select>
            </label>
            <label>Ad<input type="text" maxLength={100} required value={newName} onChange={(e) => setNewName(e.target.value)} /></label>
            <button className="btn" type="submit" disabled={busy}>Ekle</button>
          </form>
        </div>
        <NoteBox id="assets" className="mt12" />
        {!assets ? <p className="loading">Yükleniyor…</p> : (
          <div className="cards">
            {list.length ? list.map((a) => <AssetCard key={a.id} a={a} flash={flashAsset === a.id} />)
              : <p className="empty">Bu filtreye uyan varlık yok.</p>}
          </div>
        )}
      </div>
    </section>
  );
}
