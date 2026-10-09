"use client";
/* Kurgu sayfasının denetçisi: seçili çekimin bilgisi, rafine havuz, ses, sekans, varlıklar, yöntem ve elle düzeltme;
   her bölüm açılıp kapanır. */
import { useState, type FormEvent } from "react";
import useSWR from "swr";
import { api, fetcher } from "@/lib/api";
import { candidates, defaultSelection, parseIds } from "@/lib/compare";
import { ASTATUS_TR, BOARD_SOURCE_TR, EMOTION_COLOR, EMOTION_TR, ENGINE_CHOICES, ENGINE_TR, H3_MODE_TR, KEYFRAME_MODEL_TR, METHOD_TR, TRANSITION_TR, MOVE_TR, SHOT_TR, TYPE_TR } from "@/lib/i18n";
import { usePanel } from "@/lib/panel";
import { resultText, usd } from "@/lib/text";
import type { RevisionList, Scene, SceneAssetRef, Sequence, StageResult, Variation } from "@/lib/types";
import { NoteBox, Section, StatusDot, UploadForm, useSynced } from "./ui";

export function MissingLinks({ list }: { list: SceneAssetRef[] }) {
  const { gotoAsset } = usePanel();
  return (
    <>
      {list.map((m, i) => (
        <span key={m.id ?? m.name}>
          {i > 0 && ", "}
          {m.id
            ? <a href="#" onClick={(e) => { e.preventDefault(); gotoAsset(m.id!); }}
                title={`${TYPE_TR[m.type]} · ${m.status ? ASTATUS_TR[m.status] : "kaydı yok"}`}>{m.name}</a>
            : m.name}
        </span>
      ))}
    </>
  );
}

const FROZEN_TITLE = "Üretim durduruldu, önce Üretime devam et";

function Shot({ sc }: { sc: Scene }) {
  const { busy, frozen, act, note, setVar } = usePanel();
  const cam = sc.camera || {};
  const error = sc.last_error && !["done", "blocked"].includes(sc.status) ? sc.last_error.split("\n")[0] : "";
  const regen = (thaw = false) => act(async () => {
    const r = thaw ? await api.post<StageResult>(`/scenes/${sc.id}/render?force_visual=true&thaw=true`)
      : await api.post<StageResult>(`/scenes/${sc.id}/render?force_visual=true`);
    setVar(null);
    note("inspector", (thaw ? "Donma bu çekim için kaldırıldı; iş bitince proje yeniden donacak. " : "") + resultText(r), r.blocked ? "err" : "ok");
  }, "inspector");
  const pickFace = (face_expected: string) => act(async () => {
    await api.patch(`/scenes/${sc.id}`, { face_expected });
    note("inspector", `${sc.scene_code}: yüz beklentisi ${face_expected === "auto" ? "otomatik" : face_expected === "true" ? "evet" : "hayır"}. Uygulamak için "Bu sahneyi baştan üret".`, "ok");
  }, "inspector");
  const engines: string[] = [...ENGINE_CHOICES, ...(sc.engine && !(ENGINE_CHOICES as readonly string[]).includes(sc.engine) ? [sc.engine] : [])];
  const pickEngine = (engine: string) => act(async () => {
    await api.patch(`/scenes/${sc.id}`, { engine });
    note("inspector", `${sc.scene_code} bundan sonra ${ENGINE_TR[engine] ?? engine} ile üretilecek. Uygulamak için "Bu sahneyi baştan üret".`, "ok");
  }, "inspector");
  const upgrade = () => act(async () => {
    const r = await api.post<{ upgrade_requested: boolean }>(`/scenes/${sc.id}/upgrade`, { requested: !sc.upgrade_requested });
    note("inspector", r.upgrade_requested ? `${sc.scene_code} iyileştirme geçişine alınacak.` : `${sc.scene_code} işareti kaldırıldı.`, "ok");
  }, "inspector");
  return (
    <Section title="Çekim" meta={<StatusDot status={sc.status} />}>
      <p>{sc.description}</p>
      <p className="dim small">{sc.heading}</p>
      {error && <p className="warn small">{error}</p>}
      {sc.status === "blocked" && sc.missing.length > 0 && (
        <p className="small"><span className="warn">Eksik referans:</span> <MissingLinks list={sc.missing} /></p>
      )}
      <dl className="kv">
        <dt>Duygu</dt><dd><span className="dot" style={{ background: EMOTION_COLOR[sc.emotion] || "#9ca0a9" }} />{EMOTION_TR[sc.emotion] || sc.emotion}</dd>
        <dt>Kamera</dt><dd>{SHOT_TR[cam.shot ?? ""] || cam.shot || "—"}, {MOVE_TR[cam.movement ?? ""] || cam.movement || "—"}, <span className="mono">{cam.lens_mm ?? "—"} mm</span></dd>
        <dt>Süre</dt><dd className="mono">{Number(sc.duration_s).toFixed(1)} sn</dd>
        <dt>Aksiyon</dt><dd className="mono">{Number(sc.action_complexity).toFixed(2)}</dd>
        <dt>Motor</dt>
        <dd>
          <select value={sc.engine ?? ENGINE_CHOICES[0]} disabled={busy || frozen} aria-label="Video motoru"
            title={frozen ? FROZEN_TITLE : "MiniMax H3 RunPod'da (varsayılan) çalışır; Seedance fal.ai'de, yalnızca seçtiğiniz çekimlerde"}
            onChange={(e) => pickEngine(e.target.value)}>
            {engines.map((e) => <option key={e} value={e}>{ENGINE_TR[e] ?? e}</option>)}
          </select>
        </dd>
        <dt>Karakterler</dt><dd>{sc.characters.join(", ") || "—"}</dd>
        {sc.characters.length > 0 && <>
          <dt>Yüz görünmeli</dt>
          <dd>
            <select value={sc.face_expected_explicit == null ? "auto" : String(sc.face_expected_explicit)} disabled={busy}
              aria-label="Yüz görünmeli" title="Kimlik kapısı: yüz beklenen çekimde yüzü görünmeyen ya da kimlik eşiğinin altındaki aday havuza giremez. Sırt çekimi, silüet, uzak planda Hayır seçin."
              onChange={(e) => pickFace(e.target.value)}>
              <option value="auto">Otomatik ({sc.face_expected ? "evet" : "hayır"})</option>
              <option value="true">Evet</option>
              <option value="false">Hayır (sırt, silüet, uzak plan)</option>
            </select>
          </dd>
        </>}
        <dt>Kesim</dt><dd>{sc.cut_tier === "draft" ? "Taslak (kaba kurgu)" : sc.cut_tier === "final" ? "Final" : "—"}</dd>
        <dt>Harcama</dt><dd className="mono">{usd(sc.spent_usd)}</dd>
      </dl>
      <div className="row">
        <button className="btn" type="button" disabled={busy || frozen} onClick={() => regen()} title={frozen ? FROZEN_TITLE : undefined}>Bu sahneyi baştan üret</button>
        {frozen && (
          <button className="btn" type="button" disabled={busy} onClick={() => regen(true)}
            title="Donma yalnızca bu çekim için kaldırılır; çekimin işi bitince (ya da hata verirse) proje yeniden donar">
            Yalnızca bu çekimi üret (donmayı geçici kaldır)</button>
        )}
        {(sc.cut_tier === "draft" || sc.upgrade_requested) && (
          <button className="btn" type="button" aria-pressed={sc.upgrade_requested} disabled={busy} onClick={upgrade}>
            {sc.upgrade_requested ? "İyileştirilecek" : "İyileştir"}
          </button>
        )}
      </div>
    </Section>
  );
}

function H3({ sc }: { sc: Scene }) {
  const { busy, frozen, act, note } = usePanel();
  const pickMode = (h3_mode: string) => act(async () => {
    await api.patch(`/scenes/${sc.id}`, { h3_mode });
    note("inspector", `${sc.scene_code}: ${H3_MODE_TR[h3_mode] ?? "otomatik koşul"}. Uygulamak için "Bu sahneyi baştan üret".`, "ok");
  }, "inspector");
  const upload = (file: File) => act(async () => {
    const r = await api.upload<{ hint: string }>(`/scenes/${sc.id}/keyframe?filename=${encodeURIComponent(file.name)}`, file);
    note("inspector", r.hint, "ok");
  }, "inspector");
  const clear = () => act(async () => {
    const r = await api.del<{ hint: string }>(`/scenes/${sc.id}/keyframe`);
    note("inspector", r.hint, "ok");
  }, "inspector");
  const redraw = () => act(async () => {
    await api.post(`/scenes/${sc.id}/board/draw?paid=true`);
    note("inspector", `${sc.scene_code}: ilk kare yeni seed'lerle yeniden çiziliyor (her deneme ücretli).`, "ok");
  }, "inspector");
  const accept = () => act(async () => {
    const r = await api.post<{ stages: string[]; frozen: boolean }>(`/scenes/${sc.id}/board/accept`);
    note("inspector", r.frozen ? `${sc.scene_code}: kare elle onaylandı. Proje donmuş: üretime devam edince video üretilecek.`
      : `${sc.scene_code}: kare elle onaylandı; ${r.stages.length ? "video aşamasına geçildi." : "üretilecek aşama yok."}`, "ok");
  }, "inspector");
  const setTransition = (transition_in: string) => act(async () => {
    await api.patch(`/scenes/${sc.id}`, { transition_in });
    note("inspector", `${sc.scene_code}: geçiş ${transition_in === "auto" ? "senaryodaki gibi" : TRANSITION_TR[transition_in]?.split(":")[0]}. Kare zinciri buna göre yeniden çizilir.`, "ok");
  }, "inspector");
  const redrawChain = () => act(async () => {
    const r = await api.post<{ codes: string[] }>(`/scenes/${sc.id}/board/redraw-chain`);
    note("inspector", r.codes.length ? `Sonraki ${r.codes.length} kare sırayla yeniden çiziliyor: ${r.codes.join(", ")}.` : "Yeniden çizilecek kare yok.", "ok");
  }, "inspector");
  const source = sc.keyframe_source === "upload" ? "upload" : sc.board_source;
  const review = sc.board_status === "changes_requested" && (sc.board_source === "qwen_edit" || sc.board_source === "comfyui" || sc.board_source === "flux");
  const editRefs = sc.h3_mode === "fl2va" && sc.keyframe_source !== "upload" && sc.keyframe_model === "qwen_edit" ? sc.edit_refs ?? [] : [];
  return (
    <Section title="H3 koşulu" meta={sc.h3_mode === "ref2va" ? "Ref2VA" : "FL2VA"}>
      <label className="field">Kompozisyon nereden gelsin
        <select value={sc.h3_mode_explicit ?? "auto"} disabled={busy || frozen} aria-label="H3 koşulu" title={frozen ? FROZEN_TITLE : undefined}
          onChange={(e) => pickMode(e.target.value)}>
          <option value="auto">Otomatik (şu an: {sc.h3_mode === "ref2va" ? "Ref2VA" : "FL2VA"})</option>
          {Object.entries(H3_MODE_TR).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </label>
      {sc.h3_mode_warning && <p className="warn small">{sc.h3_mode_warning}</p>}
      <label className="field">Çekime geçiş
        <select value={sc.transition_explicit ?? "auto"} disabled={busy} aria-label="Çekime geçiş" onChange={(e) => setTransition(e.target.value)}>
          <option value="auto">Senaryodaki gibi (şu an: {TRANSITION_TR[sc.transition ?? "continuous"]?.split(":")[0]})</option>
          {Object.entries(TRANSITION_TR).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </label>
      {sc.chain_parent && (
        <p className={sc.chain_stale ? "warn small" : "faint small"}>
          {sc.chain_stale ? `Kare eski: zincirdeki önceki kare (${sc.chain_parent}) değişti; videodan önce yeniden çizilecek.`
            : `Kare ${sc.chain_parent} karesinden devam eder (anahtar kare zinciri).`}
        </p>
      )}
      {(sc.chain_redraw?.length ?? 0) > 0 && (
        <div className="row">
          <button className="btn" type="button" disabled={busy || frozen} title={frozen ? FROZEN_TITLE : "Zincirde bu kareden türeyen eski ya da eksik kareler sırayla çizilir (ücretli)"}
            onClick={redrawChain}>Sonraki {sc.chain_redraw!.length} kareyi yeniden çiz</button>
        </div>
      )}
      <p className="faint small">Aynı seed farklı çözünürlükte farklı kompozisyon verir; test turu ve tam tur bu ilk kareden başlar.</p>
      <div style={{ display: "grid", gridTemplateColumns: "120px 1fr", gap: 10, alignItems: "start" }}>
        {sc.keyframe_url
          ? <img src={sc.keyframe_url} alt="İlk kare" style={{ width: 120, aspectRatio: "16 / 9", objectFit: "cover", borderRadius: 2 }} />
          : <span className="faint small" style={{ width: 120 }}>İlk kare yok</span>}
        <dl className="kv">
          <dt>İlk kare</dt><dd>{source ? BOARD_SOURCE_TR[source] ?? source : "storyboard karesi bekleniyor"}</dd>
          <dt>Skor</dt><dd className="mono">{sc.keyframe_source === "upload" ? "yönetmen" : sc.board_score != null ? sc.board_score.toFixed(3) : "—"}</dd>
          <dt>Kare kararı</dt><dd>{sc.board_status === "changes_requested" ? <span className="warn">{sc.board_note || "değişiklik istendi"}</span> : sc.board_status === "approved" ? "onaylı" : "onay bekliyor"}</dd>
          {sc.h3_mode === "fl2va" && sc.keyframe_source !== "upload" && sc.keyframe_model && (
            <><dt>Kare modeli</dt><dd>{KEYFRAME_MODEL_TR[sc.keyframe_model] ?? sc.keyframe_model}</dd></>
          )}
        </dl>
      </div>
      {review && (
        <div className="row">
          <button className="btn good" type="button" disabled={busy} onClick={accept}
            title="Kontrolü geçemeyen kareyi gözle kabul et; video bu kareden üretilir">Kareyi elle onayla</button>
          <button className="btn" type="button" disabled={busy || frozen} title={frozen ? FROZEN_TITLE : undefined} onClick={redraw}>Kareyi yeniden çiz</button>
        </div>
      )}
      {editRefs.length > 0 && (
        <>
          <h4 className="small dim">İlk karenin referansları (Qwen-Image-Edit)</h4>
          <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
            {editRefs.map((r, i) => (
              <figure key={`${r.name}-${i}`} style={{ margin: 0, textAlign: "center" }}>
                {r.url && <img src={r.url} alt={r.name} loading="lazy" style={{ width: 56, height: 56, objectFit: "cover", borderRadius: 2 }} />}
                <figcaption className="faint small">image {i + 1}: {r.name}</figcaption>
              </figure>
            ))}
          </div>
        </>
      )}
      <UploadForm label="Kendi ilk kareni yükle" accept="image/png,image/jpeg,image/webp" button="Yükle" onUpload={upload} />
      {sc.keyframe_source === "upload" && <div className="row"><button className="btn ghost" type="button" disabled={busy} onClick={clear}>Yüklenen kareyi kaldır</button></div>}
      {sc.h3_mode === "ref2va" && (
        <>
          <h4 className="small dim">Karakter referans seti ({sc.h3_refs.length}/9)</h4>
          {sc.h3_refs.length ? (
            <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
              {sc.h3_refs.map((r, i) => (
                <figure key={`${r.name}-${i}`} style={{ margin: 0, textAlign: "center" }}>
                  {r.url && <img src={r.url} alt={r.name} loading="lazy" style={{ width: 56, height: 56, objectFit: "cover", borderRadius: 2 }} />}
                  <figcaption className="faint small">{r.name}</figcaption>
                </figure>
              ))}
            </div>
          ) : <p className="warn small">Çekimdeki karakterlerin referansı yok: Varlıklar'dan durağan görsel, ifade sayfası ya da dönüş videosu yükleyin.</p>}
        </>
      )}
    </Section>
  );
}

function Pool({ sc }: { sc: Scene }) {
  const { busy, act, note, setVar, compareRaw, openCompare } = usePanel();
  const pool = sc.variations.filter((v) => v.rank).sort((a, b) => (a.rank ?? 0) - (b.rank ?? 0));
  const picked = parseIds(compareRaw, sc);
  const n = candidates(sc).length;
  const promote = (v: Variation) => act(async () => {
    const r = await api.post<StageResult>(`/scenes/${sc.id}/promote/${v.id}`);
    setVar(null);
    note("inspector", resultText(r), r.blocked ? "err" : "ok");
  }, "inspector");
  return (
    <Section title="Rafine havuz" meta={`${pool.length} aday`}>
      {pool.length ? (
        <ul className="lines">
          {pool.map((v) => (
            <li key={v.id} style={{ display: "grid", gridTemplateColumns: "56px 1fr auto", gap: 8, alignItems: "center" }}>
              {v.thumb_url ? <img src={v.thumb_url} alt="" loading="lazy" style={{ width: 56, height: 32, objectFit: "cover", borderRadius: 2 }} /> : <span />}
              <span><b className="mono">#{v.rank}</b> <span className="dim mono">skor {v.score != null ? v.score.toFixed(3) : "—"}</span></span>
              {v.rank === 1 ? <span className="tag ok">Kullanımda</span>
                : <button type="button" className="btn" disabled={busy} onClick={() => promote(v)}>Bunu kullan</button>}
            </li>
          ))}
        </ul>
      ) : <p className="faint">Henüz rafine havuz yok.</p>}
      {n >= 2 && (
        <div className="row">
          <button className="btn" type="button" onClick={() => openCompare(sc.id, picked.length >= 2 ? picked : defaultSelection(sc))}>
            {picked.length >= 2 ? `Seçilen ${picked.length} klibi karşılaştır` : "Adayları karşılaştır"}
          </button>
          <span className="faint small">{picked.length === 1 ? "Bir klip daha seç (Shift+tık)." : `${n} izlenebilir aday`}</span>
        </div>
      )}
    </Section>
  );
}

function Sound({ sc, q }: { sc: Scene; q: Sequence | undefined }) {
  const { busy, frozen, act, note } = usePanel();
  const lines = sc.tracks.filter((t) => t.kind === "dialogue");
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
    <Section title="Ses" meta={`${lines.length} replik`}>
      <h4 className="small dim">Diyalog</h4>
      <ul className="lines">
        {lines.length ? lines.map((t) => (
          <li key={t.id}><span className="who">{t.character || "Anlatıcı"}</span> · {t.text}{" "}
            <span className="faint mono small">({EMOTION_TR[t.emotion ?? ""] || "—"}, {Number(t.start_offset_s).toFixed(1)} sn)</span></li>
        )) : <li className="faint">Bu çekimde diyalog yok.</li>}
      </ul>
      <h4 className="small dim">Müziği değiştir</h4>
      <form className="form-row" onSubmit={submit} noValidate>
        <label className="field">Kapsam
          <select value={scope} disabled={!q} onChange={(e) => setScope(e.target.value as "sequence" | "scene")}>
            <option value="sequence">Sekansın tamamı</option><option value="scene">Yalnızca bu çekim</option>
          </select>
        </label>
        <label className="field">Duygu
          <select value={emotion} onChange={(e) => setEmotion(e.target.value)}>
            {Object.entries(EMOTION_TR).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <label className="field">Seviye (dB)<input type="number" min={-40} max={0} step={1} value={gain} onChange={(e) => setGain(e.target.value)} /></label>
        <button className="btn" type="submit" disabled={busy || frozen}>Müziği değiştir</button>
      </form>
    </Section>
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
    <Section title={`Sekans ${q.code} · ${q.title}`} meta={q.status === "capped" ? <span className="tag bad">Tavan doldu</span> : usd(q.spent_usd)} open={false}>
      <p className="dim small">
        {q.shots} çekim · {q.done} hazır · {usd(q.spent_usd)} harcandı · {q.budget_cap_usd != null ? "tavan " + usd(q.budget_cap_usd) : "tavan yok"}
        {q.status === "capped" ? " · tavan doldu: bu sekansta yeni üretim yapılmaz" : ""}
      </p>
      <form className="form-row" onSubmit={saveCap}>
        <label className="field">Bütçe tavanı (USD)<input type="number" min={0.01} step={0.01} placeholder="tavan yok" value={cap} onChange={(e) => setCap(e.target.value)} /></label>
        <button className="btn" type="submit" disabled={busy}>Tavanı kaydet</button>
      </form>
      <h4 className="small dim">Film düzeyinde kanallar</h4>
      <ul className="lines">
        {q.tracks.length ? q.tracks.map((t) => (
          <li key={t.id}>
            <button type="button" className="btn ghost" disabled={busy} onClick={() => remove(t.id)}>Kaldır</button>
            {t.source === "procedural" ? `Müzik · ${EMOTION_TR[t.mood ?? ""] || t.mood}` : `${t.kind === "music" ? "Stem" : t.kind} · ${t.filename}`}{" "}
            <span className="faint mono small">({Number(t.start_s).toFixed(1)} sn{t.end_s != null ? `–${Number(t.end_s).toFixed(1)} sn` : "'den sekans sonuna"}, {Number(t.gain_db).toFixed(0)} dB)</span>
          </li>
        )) : <li className="faint">Bu sekansta film düzeyinde kanal yok.</li>}
      </ul>
      <UploadForm label="Stem yükle" accept="audio/*" button="Yükle"
        onUpload={(file, field) => act(async () => {
          const params = new URLSearchParams({ filename: file.name, start_s: field("start_s") || "0", gain_db: field("gain_db") || "0" });
          const r = await api.upload<{ stages: string[] }>(`/sequences/${q.id}/tracks?${params}`, file);
          note("inspector", `${q.code} sekansına stem eklendi; hiçbir çekim yeniden üretilmedi.` + (r.stages.length ? " Film yeniden birleştiriliyor." : ""), "ok");
        }, "inspector")}>
        <label className="field">Başlangıç (sn)<input type="number" name="start_s" min={0} step={0.1} defaultValue={0} /></label>
        <label className="field">Seviye (dB)<input type="number" name="gain_db" min={-40} max={0} step={1} defaultValue={-6} /></label>
      </UploadForm>
    </Section>
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
  const tone = (s: string | null) => (s === "locked" ? "ok" : s === "motion_tested" ? "info" : s === "still_approved" ? "warn" : "bad");
  return (
    <Section title="Varlıklar" meta={`${sc.assets.length}`} open={sc.status === "blocked"}>
      <div className="row">
        {sc.assets.length ? sc.assets.map((a) => (
          <span key={`${a.type}:${a.name}`} className={`tag ${tone(a.status)}`} title={`${TYPE_TR[a.type]} · ${a.status ? ASTATUS_TR[a.status] : "kaydı yok"}`}>
            {a.id ? <a href="#" style={{ color: "inherit" }} onClick={(e) => { e.preventDefault(); gotoAsset(a.id!); }}>{a.name}</a> : a.name}
            {a.type !== "character" && a.id && (
              <button type="button" aria-label={`${a.name} bağını kaldır`} title="Bağı kaldır" disabled={busy} onClick={() => unlink(a.id!)}>×</button>
            )}
          </span>
        )) : <span className="faint">Bu çekime bağlı varlık yok.</span>}
      </div>
      <form className="form-row" onSubmit={link}>
        <label className="field grow">Nesne bağla<input type="text" maxLength={100} placeholder="ör. Taksi" required value={name} onChange={(e) => setName(e.target.value)} /></label>
        <button className="btn" type="submit" disabled={busy}>Bağla</button>
      </form>
    </Section>
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
    <Section title="Yöntem" meta={METHOD_TR[sc.method] || sc.method} open={sc.method_hint}>
      {sc.method_hint && <p className="hint">Bu çekimde aksiyon çok yoğun ve hareket özel: tarif ederek üretmek zor. Previs rehberi önerilir; karar senin.</p>}
      <form className="form-row" onSubmit={(e) => {
        e.preventDefault();
        void act(async () => {
          const r = await api.post<StageResult & { method: string }>(`/scenes/${sc.id}/method`, { method });
          note("inspector", `Yöntem: ${METHOD_TR[r.method]}. ` + resultText(r), r.blocked ? "err" : "ok");
        }, "inspector");
      }}>
        <label className="field">Üretim yöntemi
          <select value={method} onChange={(e) => setMethod(e.target.value)}>
            {Object.entries(METHOD_TR).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <button className="btn" type="submit" disabled={busy}>Uygula</button>
      </form>
      {(sc.guide_url || sc.external_url) && (
        <p className="small dim">
          {sc.guide_url && <a href={sc.guide_url} target="_blank" rel="noopener">Rehber video</a>}
          {sc.guide_url && sc.external_url && " · "}
          {sc.external_url && <a href={sc.external_url} target="_blank" rel="noopener">Dış çekim videosu</a>}
        </p>
      )}
      <UploadForm label="Previs rehberi yükle" accept="video/*" button="Yükle" onUpload={upload("guide")} />
      <UploadForm label="Dış çekim yükle" accept="video/*" button="Yükle" onUpload={upload("external")} />
    </Section>
  );
}

function Revisions({ target }: { target: Variation | undefined }) {
  const { busy, act, note } = usePanel();
  const [kind, setKind] = useState("video");
  const sig = target ? `${target.revisions}:${target.revision}` : "";
  // Revizyon sayısı ya da etkin revizyon değişince liste yeniden okunur (sig, arka ucun yok saydığı bir sorgu değeridir).
  const { data } = useSWR<RevisionList>(target ? `/variations/${target.id}/revisions?sig=${encodeURIComponent(sig)}` : null, fetcher);
  if (!target) return <Section title="Elle düzeltme" open={false}><p className="faint">Düzeltilecek bir klip yok.</p></Section>;
  const activate = (rid: string | null) => act(async () => {
    const r = await api.post<StageResult>(`/variations/${target.id}/revisions/active`, { revision_id: rid });
    note("inspector", (rid ? "Revizyon kullanılıyor. " : "Orijinale dönüldü. ") + (r.hint || resultText(r)), "ok");
  }, "inspector");
  const canUpload = Boolean(target.video_url) && !target.video_url!.includes("quarantine");
  return (
    <Section title="Elle düzeltme" meta={target.rank ? `#${target.rank} klip` : "seçili klip"} open={false}>
      <ul className="lines">
        {!data ? <li className="faint">Yükleniyor…</li> : (
          <>
            {data.revisions.map((r) => (
              <li key={r.id} style={r.id === data.active ? { borderColor: "var(--ok)" } : undefined}>
                {r.id === data.active ? <span className="tag ok" style={{ float: "right" }}>kullanımda</span>
                  : <button className="btn" type="button" disabled={busy} onClick={() => activate(r.id)}>Bunu kullan</button>}
                <b>Revizyon {r.version}</b> <span className="dim">{r.kind === "video" ? "video" : "ilk kare"}{r.note ? " · " + r.note : ""}</span>{" "}
                <a href={r.video_url} target="_blank" rel="noopener">izle</a>
              </li>
            ))}
            <li style={!data.active ? { borderColor: "var(--ok)" } : undefined}>
              {data.active ? <button className="btn" type="button" disabled={busy} onClick={() => activate(null)}>Orijinale dön</button>
                : <span className="tag ok" style={{ float: "right" }}>kullanımda</span>}
              <b>Orijinal</b>{data.original_url && <> <a href={data.original_url} target="_blank" rel="noopener">izle</a></>}
            </li>
          </>
        )}
      </ul>
      {canUpload && (
        <UploadForm label="Dosya" accept={kind === "video" ? "video/*" : "image/*"} button="Revizyon yükle"
          onUpload={(file, field) => act(async () => {
            const params = new URLSearchParams({ kind, filename: file.name });
            const n = field("note").trim();
            if (n) params.set("note", n);
            const r = await api.upload<{ revision: { version: number }; hint?: string } & StageResult>(`/variations/${target.id}/revisions?${params}`, file);
            note("inspector", `Revizyon ${r.revision.version} yüklendi, orijinal korunuyor. ` + (r.hint || resultText(r)), "ok");
          }, "inspector")}>
          <label className="field">Tür
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="video">Düzenlenmiş video</option><option value="first_frame">Düzenlenmiş ilk kare</option>
            </select>
          </label>
          <label className="field">Not<input type="text" name="note" maxLength={2000} placeholder="ör. renk ve kalem çizgileri" /></label>
        </UploadForm>
      )}
    </Section>
  );
}

function ColorLink({ sc }: { sc: Scene }) {
  const { setPage } = usePanel();
  const g = sc.grade;
  const neutral = g.exposure === 0 && g.contrast === 1 && g.saturation === 1 && g.gamma === 1 && g.temperature === 6500;
  return (
    <Section title="Renk" meta={neutral ? "nötr" : "ayarlı"} open={false}>
      <p className="dim small mono">pozlama {g.exposure} · kontrast {g.contrast} · doygunluk {g.saturation} · gama {g.gamma} · {g.temperature} K</p>
      <div className="row"><button className="btn" type="button" onClick={() => setPage("color")}>Renk sayfasında aç</button></div>
    </Section>
  );
}

export function Inspector({ className = "" }: { className?: string }) {
  const { scene: sc, sequence: q, varId } = usePanel();
  if (!sc) return <section className={`pnl ${className}`}><header className="pnl-h"><h2>Denetçi</h2></header><p className="empty">Çekim seçin.</p></section>;
  const revTarget = (varId ? sc.variations.find((v) => v.id === varId) : undefined) ?? sc.variations.find((v) => v.rank === 1);
  return (
    <section className={`pnl ${className}`} aria-label="Denetçi">
      <header className="pnl-h"><h2 className="mono">{sc.scene_code}</h2><span className="meta">Denetçi</span></header>
      <div className="pnl-b">
        <Shot sc={sc} />
        {(sc.engine ?? "h3") === "h3" && <H3 key={`h${sc.id}`} sc={sc} />}
        <Pool sc={sc} />
        <Sound key={`s${sc.id}`} sc={sc} q={q} />
        {q && <SequenceBox q={q} />}
        <Assets key={`a${sc.id}`} sc={sc} />
        <Method key={`m${sc.id}`} sc={sc} />
        <Revisions key={revTarget?.id ?? sc.id} target={revTarget} />
        <ColorLink sc={sc} />
        <div style={{ padding: 10 }}><NoteBox id="inspector" /></div>
      </div>
    </section>
  );
}
