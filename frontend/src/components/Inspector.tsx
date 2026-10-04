"use client";
import { useEffect, useState, type FormEvent } from "react";
import useSWR from "swr";
import { api, fetcher } from "@/lib/api";
import { candidates, defaultSelection, parseIds } from "@/lib/compare";
import { ASTATUS_TR, EMOTION_COLOR, EMOTION_TR, GRADE_FIELDS, METHOD_TR, MOVE_TR, SHOT_TR, TYPE_TR } from "@/lib/i18n";
import { usePanel } from "@/lib/panel";
import { resultText, usd } from "@/lib/text";
import type { Grade, RevisionList, Scene, Sequence, StageResult, Variation } from "@/lib/types";
import { NoteBox, StatusPill, UploadForm, useSynced, Video } from "./ui";

function pickMedia(sc: Scene, varId: string | null) {
  const best = sc.variations.find((v) => v.rank === 1);
  const chosen = varId ? sc.variations.find((v) => v.id === varId) : undefined;
  if (chosen?.video_url) {
    const quarantine = chosen.video_url.includes("quarantine");
    return { src: chosen.video_url, poster: chosen.thumb_url, preview: true,
             label: `Varyasyon önizlemesi${quarantine ? " (karantinada)" : ""}${chosen.score != null ? " · skor " + chosen.score.toFixed(3) : ""}` };
  }
  if (sc.final_url) return { src: sc.final_url, poster: best?.thumb_url, preview: false, label: "Sonuç videosu" };
  if (best?.video_url) return { src: best.video_url, poster: best.thumb_url, preview: false, label: "Rafine video (montaj bekliyor)" };
  return null;
}

function Pool({ sc }: { sc: Scene }) {
  const { busy, act, note, setVar } = usePanel();
  const pool = sc.variations.filter((v) => v.rank).sort((a, b) => (a.rank ?? 0) - (b.rank ?? 0));
  if (!pool.length) return <p className="muted">Henüz rafine havuz yok.</p>;
  const promote = (v: Variation) => act(async () => {
    const r = await api.post<StageResult>(`/scenes/${sc.id}/promote/${v.id}`);
    setVar(null);
    note("inspector", resultText(r), r.blocked ? "err" : "ok");
  }, "inspector");
  return (
    <div className="pool mt8">
      {pool.map((v) => (
        <div key={v.id} className={`pool-item${v.rank === 1 ? " use" : ""}`}>
          {v.thumb_url ? <img src={v.thumb_url} alt="" loading="lazy" /> : <span className="ph" />}
          <div><b>{v.rank}. sıra</b><br /><span className="muted num">skor {v.score != null ? v.score.toFixed(3) : "—"}</span></div>
          {v.rank === 1 ? <span className="muted">Kullanımda</span>
            : <button type="button" className="btn small" disabled={busy} onClick={() => promote(v)}>Bunu kullan</button>}
        </div>
      ))}
    </div>
  );
}

function Info({ sc }: { sc: Scene }) {
  const cam = sc.camera || {};
  return (
    <dl className="kv mt8">
      <dt>Duygu</dt><dd><span className="dot" style={{ background: EMOTION_COLOR[sc.emotion] || "#A39E95" }} />{EMOTION_TR[sc.emotion] || sc.emotion}</dd>
      <dt>Kamera</dt><dd>{SHOT_TR[cam.shot ?? ""] || cam.shot || "—"}, {MOVE_TR[cam.movement ?? ""] || cam.movement || "—"}, {cam.lens_mm ?? "—"} mm</dd>
      <dt>Süre</dt><dd className="num">{Number(sc.duration_s).toFixed(1)} sn</dd>
      <dt>Aksiyon</dt><dd className="num">{Number(sc.action_complexity).toFixed(2)}</dd>
      <dt>Motor</dt><dd>{sc.engine || "—"}</dd>
      <dt>Karakterler</dt><dd>{sc.characters.join(", ") || "—"}</dd>
    </dl>
  );
}

function Lines({ sc }: { sc: Scene }) {
  const lines = sc.tracks.filter((t) => t.kind === "dialogue");
  return (
    <ul className="lines mt8">
      {lines.length ? lines.map((t) => (
        <li key={t.id}><span className="who">{t.character || "Anlatıcı"}</span> · {t.text}{" "}
          <span className="muted">({EMOTION_TR[t.emotion ?? ""] || "—"}, {Number(t.start_offset_s).toFixed(1)} sn)</span></li>
      )) : <li className="muted">Bu çekimde diyalog yok.</li>}
    </ul>
  );
}

function Assets({ sc }: { sc: Scene }) {
  const { busy, act, note, gotoAsset } = usePanel();
  const [name, setName] = useState("");
  const link = (e: FormEvent) => {
    e.preventDefault();
    void act(async () => {
      const r = await api.post<{ asset: { name: string; status: string }; hint: string }>(`/scenes/${sc.id}/assets`, { name });
      setName("");
      note("inspector", `${r.asset.name} bağlandı (${ASTATUS_TR[r.asset.status]}). ${r.hint}`, "ok");
    }, "inspector");
  };
  const unlink = (id: string) => act(async () => {
    const r = await api.del<{ hint: string }>(`/scenes/${sc.id}/assets/${id}`);
    note("inspector", "Bağ kaldırıldı. " + r.hint, "ok");
  }, "inspector");
  return (
    <>
      <div className="chips mt8">
        {sc.assets.length ? sc.assets.map((a) => (
          <span key={`${a.type}:${a.name}`} className={`astatus ${a.status || "draft"}`}>
            {a.id ? <button type="button" className="linkbtn" style={{ color: "inherit" }} onClick={() => gotoAsset(a.id!)}>{a.name}</button> : a.name}
            {" "}<span className="muted">{TYPE_TR[a.type]}</span>
            {a.type !== "character" && a.id && (
              <button type="button" aria-label={`${a.name} bağını kaldır`} title="Bağı kaldır" disabled={busy} onClick={() => unlink(a.id!)}>×</button>
            )}
          </span>
        )) : <span className="muted">Bu çekime bağlı varlık yok.</span>}
      </div>
      <form className="form-row mt8" onSubmit={link}>
        <label>Nesne bağla<input type="text" maxLength={100} placeholder="ör. Taksi" required value={name} onChange={(e) => setName(e.target.value)} /></label>
        <button className="btn small" type="submit" disabled={busy}>Bağla</button>
      </form>
    </>
  );
}

function Music({ sc, q }: { sc: Scene; q: Sequence | undefined }) {
  const { busy, frozen, act, note } = usePanel();
  const [scopePick, setScope] = useState<"sequence" | "scene">("sequence");
  const scope = q ? scopePick : "scene";
  const music = scope === "sequence" ? q?.tracks.find((t) => t.kind === "music" && t.source === "procedural")
                                     : sc.tracks.find((t) => t.kind === "music");
  const sig = `${sc.id}:${scope}:${music ? music.id + ":" + music.gain_db : "-"}`;
  const [emotion, setEmotion] = useSynced(music?.mood || sc.emotion, sig);
  const [gain, setGain] = useSynced(String(music ? music.gain_db : -8), sig);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const g = Number(gain);
    if (!Number.isFinite(g) || g < -40 || g > 0) { note("inspector", "Seviye -40 ile 0 dB arasında olmalı.", "err"); return; }
    if (q && scope === "sequence") {
      void act(async () => {
        const r = await api.post<{ stages: string[] }>(`/sequences/${q.id}/tracks`, { mood: emotion, gain_db: g });
        note("inspector", `${q.code} sekansının müziği değişti; hiçbir çekim yeniden üretilmedi.` + (r.stages.length ? " Film yeniden birleştiriliyor." : ""), "ok");
      }, "inspector");
      return;
    }
    void act(async () => {
      const r = await api.put<StageResult>(`/scenes/${sc.id}/audio`, { kind: "music", emotion, gain_db: g, params: { mood: emotion } });
      note("inspector", resultText(r), r.blocked ? "err" : "ok");
    }, "inspector");
  };
  return (
    <form className="form-row mt8" onSubmit={submit} noValidate>
      <label>Kapsam
        <select value={scope} disabled={!q} onChange={(e) => setScope(e.target.value as "sequence" | "scene")}>
          <option value="sequence">Sekansın tamamı</option><option value="scene">Yalnızca bu çekim</option>
        </select>
      </label>
      <label>Duygu
        <select value={emotion} onChange={(e) => setEmotion(e.target.value)}>
          {Object.entries(EMOTION_TR).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </label>
      <label>Seviye (dB)<input type="number" min={-40} max={0} step={1} value={gain} onChange={(e) => setGain(e.target.value)} /></label>
      <button className="btn" type="submit" disabled={busy || frozen}>Müziği değiştir</button>
    </form>
  );
}

function SequenceBox({ q }: { q: Sequence }) {
  const { busy, act, note } = usePanel();
  const [cap, setCap] = useSynced(q.budget_cap_usd != null ? String(q.budget_cap_usd) : "", `${q.id}:${q.budget_cap_usd}`);
  const saveCap = (e: FormEvent) => {
    e.preventDefault();
    const raw = cap.trim(), value = raw === "" ? null : Number(raw);
    if (value !== null && !(value > 0)) { note("inspector", "Tavan sıfırdan büyük olmalı; kaldırmak için boş bırak.", "err"); return; }
    void act(async () => {
      const r = await api.patch<{ code: string; budget_cap_usd: number | null; status: string }>(`/sequences/${q.id}`, { budget_cap_usd: value });
      note("inspector", `${r.code} tavanı ${r.budget_cap_usd != null ? usd(r.budget_cap_usd) : "kaldırıldı"}${r.status === "capped" ? "; harcama tavanda, sekans durdu." : "."}`,
        r.status === "capped" ? "err" : "ok");
    }, "inspector");
  };
  const remove = (id: string) => act(async () => {
    const r = await api.del<{ stages: string[] }>(`/timeline-tracks/${id}`);
    note("inspector", "Kanal kaldırıldı." + (r.stages.length ? " Film yeniden birleştiriliyor." : ""), "ok");
  }, "inspector");
  return (
    <div>
      <h3>Sekans {q.code} · {q.title}</h3>
      <p className="muted mt4">
        {q.shots} çekim · {usd(q.spent_usd)} harcandı · {q.budget_cap_usd != null ? "tavan " + usd(q.budget_cap_usd) : "tavan yok"}
        {q.status === "capped" ? " · tavan doldu: bu sekansta yeni üretim yapılmaz" : ""}
      </p>
      <form className="form-row mt8" onSubmit={saveCap}>
        <label>Bütçe tavanı (USD)<input type="number" min={0.01} step={0.01} placeholder="tavan yok" value={cap} onChange={(e) => setCap(e.target.value)} /></label>
        <button className="btn small" type="submit" disabled={busy}>Tavanı kaydet</button>
      </form>
      <ul className="lines mt8">
        {q.tracks.length ? q.tracks.map((t) => (
          <li key={t.id}>
            {t.source === "procedural" ? `Müzik · ${EMOTION_TR[t.mood ?? ""] || t.mood}` : `${t.kind === "music" ? "Stem" : t.kind} · ${t.filename}`}{" "}
            <span className="muted">({Number(t.start_s).toFixed(1)} sn{t.end_s != null ? `–${Number(t.end_s).toFixed(1)} sn` : "'den sekans sonuna"}, {Number(t.gain_db).toFixed(0)} dB)</span>
            <button type="button" className="btn small" style={{ marginLeft: 6 }} disabled={busy} onClick={() => remove(t.id)}>Kaldır</button>
          </li>
        )) : <li className="muted">Bu sekansta film düzeyinde kanal yok.</li>}
      </ul>
      <UploadForm className="form-row mt8" label="Stem yükle" accept="audio/*" button="Yükle"
        onUpload={(file, field) => act(async () => {
          const params = new URLSearchParams({ filename: file.name, start_s: field("start_s") || "0", gain_db: field("gain_db") || "0" });
          const r = await api.upload<{ stages: string[] }>(`/sequences/${q.id}/tracks?${params}`, file);
          note("inspector", `${q.code} sekansına stem eklendi; hiçbir çekim yeniden üretilmedi.` + (r.stages.length ? " Film yeniden birleştiriliyor." : ""), "ok");
        }, "inspector")}>
        <label>Başlangıç (sn)<input type="number" name="start_s" min={0} step={0.1} defaultValue={0} /></label>
        <label>Seviye (dB)<input type="number" name="gain_db" min={-40} max={0} step={1} defaultValue={-6} /></label>
      </UploadForm>
    </div>
  );
}

const GRADE_LABEL: Record<string, [string, number, number, number]> = {
  exposure: ["Pozlama (EV)", -3, 3, 0.05], contrast: ["Kontrast", 0.5, 2, 0.05], saturation: ["Doygunluk", 0, 3, 0.05],
  gamma: ["Gama", 0.3, 3, 0.05], temperature: ["Sıcaklık (K)", 2000, 12000, 100],
};

function GradeBox({ sc }: { sc: Scene }) {
  const { busy, frozen, act, note } = usePanel();
  const sig = sc.id + ":" + JSON.stringify(sc.grade);
  const [values, setValues] = useSynced<Record<string, string>>(
    Object.fromEntries(GRADE_FIELDS.map((k) => [k, String(sc.grade[k as keyof Grade])])), sig);
  const numeric = () => {
    const out: Record<string, number> = {};
    for (const k of GRADE_FIELDS) { const v = Number(values[k]); if (values[k] !== "" && Number.isFinite(v)) out[k] = k === "temperature" ? Math.round(v) : v; }
    return out;
  };
  const query = new URLSearchParams(Object.entries(numeric()).map(([k, v]) => [k, String(v)])).toString();
  const [preview, setPreview] = useState(query);
  useEffect(() => { const t = setTimeout(() => setPreview(query), 400); return () => clearTimeout(t); }, [query]);   // yazmayı bitirince
  if (!sc.lipsync_ready) return <p className="muted mt8">Renk ayarı, çekimin dudak senkronlu videosu hazır olunca yapılır.</p>;
  const apply = (body: Record<string, number>, prefix: string) => act(async () => {
    const r = await api.post<StageResult>(`/scenes/${sc.id}/grade`, body);
    note("inspector", prefix + resultText(r), r.blocked ? "err" : "ok");
  }, "inspector");
  return (
    <form className="mt8" onSubmit={(e) => { e.preventDefault(); void apply(numeric(), "Renk uygulandı. "); }}>
      <div className="grade-grid">
        {GRADE_FIELDS.map((k) => {
          const [label, min, max, step] = GRADE_LABEL[k];
          return <label key={k}>{label}<input type="number" min={min} max={max} step={step} value={values[k]}
            onChange={(e) => setValues({ ...values, [k]: e.target.value })} /></label>;
        })}
      </div>
      <div className="compare">
        <figure><img src={`/api/scenes/${sc.id}/grade/preview.jpg?before=true`} alt="Renk ayarından önce" /><figcaption>Önce</figcaption></figure>
        <figure><img src={`/api/scenes/${sc.id}/grade/preview.jpg?${preview}`} alt="Renk ayarından sonra" /><figcaption>Sonra (önizleme)</figcaption></figure>
      </div>
      <div className="form-row mt8">
        <button className="btn small" type="submit" disabled={busy || frozen}>Rengi uygula</button>
        <button className="btn small" type="button" disabled={busy || frozen} onClick={() => apply({}, "Nötr renk. ")}>Nötre dön</button>
      </div>
    </form>
  );
}

function Method({ sc }: { sc: Scene }) {
  const { busy, act, note } = usePanel();
  const [method, setMethod] = useSynced(sc.method, `${sc.id}:${sc.method}`);
  const upload = (target: "guide" | "external") => (file: File) => act(async () => {
    const q = `filename=${encodeURIComponent(file.name)}`;
    const r = target === "external"
      ? await api.upload<StageResult>(`/scenes/${sc.id}/external?${q}`, file)
      : await api.upload<StageResult>(`/scenes/${sc.id}/guide?${q}`, file);
    note("inspector", (target === "external" ? "Dış çekim yüklendi; görüntü üretilmeyecek. " : "Previs rehberi yüklendi. ") + resultText(r), r.blocked ? "err" : "ok");
  }, "inspector");
  return (
    <>
      {sc.method_hint && (
        <p className="hint mt8">Bu çekimde aksiyon çok yoğun ve hareket özel: tarif ederek üretmek zor. Previs rehberi önerilir; karar senin.</p>
      )}
      <form className="form-row mt8" onSubmit={(e) => {
        e.preventDefault();
        void act(async () => {
          const r = await api.post<StageResult & { method: string }>(`/scenes/${sc.id}/method`, { method });
          note("inspector", `Yöntem: ${METHOD_TR[r.method]}. ` + resultText(r), r.blocked ? "err" : "ok");
        }, "inspector");
      }}>
        <label>Üretim yöntemi
          <select value={method} onChange={(e) => setMethod(e.target.value)}>
            {Object.entries(METHOD_TR).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <button className="btn small" type="submit" disabled={busy}>Uygula</button>
      </form>
      <p className="muted mt6 small">
        Şu an: {METHOD_TR[sc.method] || sc.method}
        {sc.guide_url && <> · <a href={sc.guide_url} target="_blank" rel="noopener">rehber video</a></>}
        {sc.external_url && <> · <a href={sc.external_url} target="_blank" rel="noopener">dış çekim videosu</a></>}
      </p>
      <UploadForm className="form-row mt8" label="Previs rehberi yükle" accept="video/*" button="Yükle" onUpload={upload("guide")} />
      <UploadForm className="form-row mt8" label="Dış çekim yükle" accept="video/*" button="Yükle" onUpload={upload("external")} />
    </>
  );
}

function Revisions({ target }: { target: Variation | undefined }) {
  const { busy, act, note } = usePanel();
  const [kind, setKind] = useState("video");
  const sig = target ? `${target.revisions}:${target.revision}` : "";
  // Revizyon sayısı ya da etkin revizyon değişince liste yeniden okunur (sig, arka ucun yok saydığı bir sorgu değeridir).
  const { data } = useSWR<RevisionList>(target ? `/variations/${target.id}/revisions?sig=${encodeURIComponent(sig)}` : null, fetcher);
  if (!target) return <><h3>Elle düzeltme</h3><ul className="revs"><li className="muted">Düzeltilecek bir klip yok.</li></ul></>;
  const activate = (rid: string | null) => act(async () => {
    const r = await api.post<StageResult>(`/variations/${target.id}/revisions/active`, { revision_id: rid });
    note("inspector", (rid ? "Revizyon kullanılıyor. " : "Orijinale dönüldü. ") + (r.hint || resultText(r)), "ok");
  }, "inspector");
  const canUpload = Boolean(target.video_url) && !target.video_url!.includes("quarantine");
  return (
    <>
      <h3>Elle düzeltme · {target.rank ? `${target.rank}. sıradaki klip` : "seçili klip"}</h3>
      <ul className="revs">
        {!data ? <li className="muted">Yükleniyor…</li> : (
          <>
            {data.revisions.map((r) => (
              <li key={r.id} className={r.id === data.active ? "active" : ""}>
                <b>Revizyon {r.version}</b>
                <span className="muted">{r.kind === "video" ? "video" : "ilk kare"}{r.note ? " · " + r.note : ""}</span>
                <a href={r.video_url} target="_blank" rel="noopener">izle</a>
                {r.id === data.active ? <span className="muted">kullanımda</span>
                  : <button className="btn small" type="button" disabled={busy} onClick={() => activate(r.id)}>Bunu kullan</button>}
              </li>
            ))}
            <li className={data.active ? "" : "active"}>
              <b>Orijinal</b>{data.original_url && <> <a href={data.original_url} target="_blank" rel="noopener">izle</a></>}
              {data.active ? <button className="btn small" type="button" disabled={busy} onClick={() => activate(null)}>Orijinale dön</button>
                : <span className="muted">kullanımda</span>}
            </li>
          </>
        )}
      </ul>
      {canUpload && (
        <UploadForm className="form-row mt8" label="Dosya" accept={kind === "video" ? "video/*" : "image/*"} button="Revizyon yükle"
          onUpload={(file, field) => act(async () => {
            const params = new URLSearchParams({ kind, filename: file.name });
            const n = field("note").trim();
            if (n) params.set("note", n);
            const r = await api.upload<{ revision: { version: number }; hint?: string } & StageResult>(`/variations/${target.id}/revisions?${params}`, file);
            note("inspector", `Revizyon ${r.revision.version} yüklendi, orijinal korunuyor. ` + (r.hint || resultText(r)), "ok");
          }, "inspector")}>
          <label>Tür
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="video">Düzenlenmiş video</option><option value="first_frame">Düzenlenmiş ilk kare</option>
            </select>
          </label>
          <label>Not<input type="text" name="note" maxLength={2000} placeholder="ör. renk ve kalem çizgileri" /></label>
        </UploadForm>
      )}
    </>
  );
}

function CompareEntry({ sc }: { sc: Scene }) {
  const { compareRaw, openCompare } = usePanel();
  const picked = parseIds(compareRaw, sc);
  const n = candidates(sc).length;
  if (n < 2) return null;
  return (
    <div className="form-row row-center">
      <button className="btn small" type="button" onClick={() => openCompare(sc.id, picked.length >= 2 ? picked : defaultSelection(sc))}>
        {picked.length >= 2 ? `Seçilen ${picked.length} klibi karşılaştır` : "Adayları karşılaştır"}
      </button>
      <span className="muted small">{picked.length === 1 ? "Bir klip daha seç (Shift+tık)." : `${n} izlenebilir aday`}</span>
    </div>
  );
}

export function Inspector() {
  const { scene: sc, sequence: q, varId, setVar, busy, frozen, act, note } = usePanel();
  if (!sc) {
    return <aside className="panel inspector" aria-label="Sahne denetçisi"><header><h2>Sahne seçin</h2></header></aside>;
  }
  const media = pickMedia(sc, varId);
  const revTarget = (varId ? sc.variations.find((v) => v.id === varId) : undefined) ?? sc.variations.find((v) => v.rank === 1);
  const regen = () => act(async () => {
    const r = await api.post<StageResult>(`/scenes/${sc.id}/render?force_visual=true`);
    setVar(null);
    note("inspector", resultText(r), r.blocked ? "err" : "ok");
  }, "inspector");
  return (
    <aside className="panel inspector" aria-label="Sahne denetçisi">
      <header><h2>{sc.scene_code} · {sc.heading}</h2><StatusPill status={sc.status} /></header>
      <div className="body">
        <div>
          <Video src={media?.src} poster={media?.poster} empty="Henüz video yok." />
          <div className="caption">
            <span>{media?.label ?? ""}</span>
            {media?.preview && <button className="btn small" type="button" onClick={() => setVar(null)}>Sonuca dön</button>}
          </div>
        </div>
        <CompareEntry sc={sc} />
        <div><h3>Rafine havuz</h3><Pool sc={sc} /></div>
        <div><h3>Çekim bilgileri</h3><Info sc={sc} /></div>
        <div><h3>Diyalog</h3><Lines sc={sc} /></div>
        <div><h3>Varlıklar</h3><Assets key={sc.id} sc={sc} /></div>
        <div><h3>Müziği değiştir</h3><Music key={sc.id} sc={sc} q={q} /></div>
        {q && <SequenceBox q={q} />}
        <div><h3>Renk</h3><GradeBox key={sc.id} sc={sc} /></div>
        <div><h3>Yöntem</h3><Method key={sc.id} sc={sc} /></div>
        <div><Revisions key={revTarget?.id ?? sc.id} target={revTarget} /></div>
        <div className="form-row"><button className="btn" type="button" disabled={busy || frozen} onClick={regen}>Bu sahneyi baştan üret</button></div>
        <NoteBox id="inspector" />
      </div>
    </aside>
  );
}
