"use client";
/* Kurgu sayfasının denetçisi: seçili çekimin bilgisi, rafine havuz, ses, sekans, varlıklar, yöntem ve elle düzeltme;
   her bölüm açılıp kapanır. */
import { useEffect, useState, type FormEvent } from "react";
import useSWR, { useSWRConfig } from "swr";
import { ApiError, api, fetcher } from "@/lib/api";
import { candidates, defaultSelection, parseIds } from "@/lib/compare";
import { audioValue, defaultResolution, engineChoices, engineShort, isFal, modeValue, parseDuration, resolutionValue, resolutionsFor } from "@/lib/engines";
import { ANOMALY_TR, ASTATUS_TR, BOARD_SOURCE_TR, EMOTION_COLOR, EMOTION_TR, ENGINE_TR, FAL_MODE_CHOICES, FAL_MODE_TR, FAL_STATUS_TR, H3_MODE_TR, KEYFRAME_MODEL_TR, METHOD_TR, TRANSITION_TR, MOVE_TR, SHOT_TR, TYPE_TR } from "@/lib/i18n";
import { BOUNDARY_NOTE, SHOT_ONLY_LABEL, adoptText, boundarySources, cutsText, mixActions, promoteText, sceneEngineText, shotBoundaries, shotEngineText, wholeSceneSwitch } from "@/lib/mixing";
import { usePanel } from "@/lib/panel";
import { Icon } from "./icons";
import { needsUpscale, resolutionBadge } from "@/lib/finishing";
import { approveText, clock, dispatchedText, gateLines, planWarnings, promptSourceText, resultText, sec1, segmentText, stagesText, upgradeText, usd, usdExact, usdTr } from "@/lib/text";
import type { AdoptResult, FalPlan, PartSplit, PlanApproval, PromoteResult, RevisionList, Scene, SceneAssetRef, PromptRerender, PromptWriteResult, SceneEnginePlan, SceneEngineResult, Sequence, ShotEnginePlan, ShotEngineResult, StageResult, UpgradeFinal, Variation } from "@/lib/types";
import { Finishing, PlanFinishing, UpscaleConfirm } from "./Finishing";
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
                title={`${TYPE_TR[m.type]} · ${m.status ? ASTATUS_TR[m.status] ?? "referans görseli yok" : "kaydı yok"}`}>{m.name}</a>
            : m.name}
        </span>
      ))}
    </>
  );
}

const FROZEN_TITLE = "Üretim durduruldu, önce Üretime devam et";
const BLOCKED_TITLE = "Eksik referans görseli var: önce Varlıklar sayfasından görseli yükleyin";
/** Referans modu (Ayarlar: VIDEO_INPUT_MODE): kare üretilmez; sahne referanslar ve senaryoyla tek istekte üretilir. */
const useRefMode = (sc: Scene) => usePanel().meta?.video_input_mode === "reference" && isFal(sc.engine);
const CUT_TR: Record<string, string> = {
  cut_in: "kırp + iyileştir", angles: "açı LoRA'sı", wide: "geniş plandan çizim", anchor: "metinle kamera tarifi",
  compose: "sıfırdan kurulum", upload: "yüklenen kare",
};

/** fal.ai motorlu çekimin hafif planı (SHA hesaplanmaz): mod ve gerekçesi, referanslar, tahmin, yükseltme tahmini.
    Her eylemden sonra panel bütün okumaları yenilediği için ayrıca yoklanmaz. */
export function useFalPlan(sc: Scene) {
  const { data } = useSWR<FalPlan>(isFal(sc.engine) ? `/scenes/${sc.id}/fal-plan` : null, fetcher, { revalidateOnFocus: false, keepPreviousData: true });
  return data && data.scene_id === sc.id ? data : undefined;
}

/** fal.ai'nin reddettiği istek (422) olduğu gibi gösterilir: alan adları ve satır sonları korunur. */
const FAL_REJECTED = "fal.ai isteği reddetti";

function Shot({ sc }: { sc: Scene }) {
  const { busy, frozen, act, note, setVar, meta } = usePanel();
  const plan = useFalPlan(sc);
  const cam = sc.camera_effective || sc.camera || {};
  const pickEntry = (entry_mode: string) => act(async () => {
    await api.patch(`/scenes/${sc.id}`, { entry_mode });
    note("inspector", entry_mode === "walk_in"
      ? `${sc.scene_code}: devam çekimi; ${(sc.entering ?? []).join(", ")} sağdan girer (ilk karede görünmez).`
      : `${sc.scene_code}: kesme; kamera belirgin biçimde değişir (30° kuralı).`, "ok");
  }, "inspector");
  const pickCut = (cut_method: string) => act(async () => {
    await api.patch(`/scenes/${sc.id}`, { cut_method });
    note("inspector", `${sc.scene_code}: kesme karesi ${cut_method === "auto" ? "otomatik yöntemle" : CUT_TR[cut_method]} çizilecek. Uygulamak için karede "Yeniden çiz".`, "ok");
  }, "inspector");
  const rejected = Boolean(sc.last_error?.startsWith(FAL_REJECTED));
  const error = sc.last_error && (rejected || !["done", "blocked"].includes(sc.status)) ? (rejected ? sc.last_error : sc.last_error.split("\n")[0]) : "";
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
  const engines = engineChoices(meta?.video, sc.engine);
  // Shot çekim değişince yeniden kurulmaz: açık onay çekimin kimliğiyle tutulur.
  const [ask, setEngineAsk] = useState<{ id: string; engine: string } | null>(null);
  const engineAsk = ask?.id === sc.id ? ask.engine : null;
  const pickEngine = (engine: string) => {
    // Referans modu + whole_scene: model değişimi varsayılan olarak bütün sahneye (onaylı); çekime özel değişim ikincil.
    if (wholeSceneSwitch(meta, sc.engine, engine)) { setEngineAsk({ id: sc.id, engine }); return; }
    setEngineAsk(null);
    void act(async () => {
      await api.patch(`/scenes/${sc.id}`, { engine });
      note("inspector", `${sc.scene_code} bundan sonra ${ENGINE_TR[engine] ?? engine} ile üretilecek. Uygulamak için "Bu sahneyi baştan üret".`, "ok");
    }, "inspector");
  };
  const refMode = useRefMode(sc);
  const upgrade = () => act(async () => {
    const r = await api.post<{ upgrade_requested: boolean }>(`/scenes/${sc.id}/upgrade`, { requested: !sc.upgrade_requested });
    note("inspector", r.upgrade_requested ? `${sc.scene_code} iyileştirme geçişine alınacak.` : `${sc.scene_code} işareti kaldırıldı.`, "ok");
  }, "inspector");
  return (
    <Section title="Çekim" meta={<StatusDot status={sc.status} />}>
      <p>{sc.description}</p>
      <p className="dim small">{sc.heading}</p>
      {error && <p className="warn small" style={rejected ? { whiteSpace: "pre-wrap", overflowWrap: "anywhere" } : undefined}>{error}</p>}
      {sc.engine_disabled && (
        <div className="hint">
          Bu motor kapalı; H3 Max'e geç. {ENGINE_TR[sc.engine ?? ""] ?? sc.engine} RunPod'da çalışır ve RunPod kapalı (endpoint silindi): bu çekim üretilemez.
          <div className="row mt8">
            <button className="btn primary" type="button" disabled={busy || frozen} title={frozen ? FROZEN_TITLE : undefined} onClick={() => pickEngine("h3max")}>H3 Max'e geç</button>
          </div>
        </div>
      )}
      {sc.winner_engine && sc.engine && sc.winner_engine !== sc.engine && !busy && (
        <p className="warn small">
          Kazanan klip {ENGINE_TR[sc.winner_engine] ?? sc.winner_engine} ile üretilmiş; çekimin motoru {ENGINE_TR[sc.engine] ?? sc.engine}.
          Bu çekimde yükseltme tamamlanmadı: yeniden üretin ya da adaylardan birini seçin.
        </p>
      )}
      {sc.generation && <MixedBox key={`${sc.id}:${sc.generation.variation_id}`} sc={sc} />}
      {sc.status === "blocked" && sc.missing.length > 0 && (
        <p className="small"><span className="warn">Eksik referans:</span> <MissingLinks list={sc.missing} /></p>
      )}
      <dl className="kv">
        <dt>Duygu</dt><dd><span className="dot" style={{ background: EMOTION_COLOR[sc.emotion] || "#9ca0a9" }} />{EMOTION_TR[sc.emotion] || sc.emotion}</dd>
        <dt>Kamera</dt><dd>{SHOT_TR[cam.shot ?? ""] || cam.shot || "—"}, {MOVE_TR[cam.movement ?? ""] || cam.movement || "—"}, <span className="mono">{cam.lens_mm ?? "—"} mm</span>
          {cam.side ? <span className="mono">, {Math.abs(cam.side)}° {cam.side > 0 ? "sağdan" : "soldan"}</span> : null}
          {sc.camera_plan && <span className="tag info" style={{ marginLeft: 6 }} title="Kesmede kamera önceki çekimle aynıydı (jump cut): en az bir ölçek adımı ya da 30° açı değişimi otomatik atandı. Kamerayı elle seçerseniz atanmaz.">otomatik kesme kamerası</span>}</dd>
        {!refMode && sc.cut_method_effective && <>
          <dt>Kesme</dt>
          <dd>
            <select value={sc.cut_method ?? "auto"} disabled={busy || frozen} aria-label="Kesme karesi yöntemi"
              title={`Denemeler: ${(sc.cut_attempts ?? []).map((m) => CUT_TR[m] ?? m).join(" → ")}`}
              onChange={(e) => pickCut(e.target.value)}>
              <option value="auto">Otomatik: {CUT_TR[sc.cut_method_effective] ?? sc.cut_method_effective}</option>
              <option value="cut_in">Kırp + iyileştir (yaklaşma)</option>
              <option value="angles">Açı LoRA'sı (açı değişimi)</option>
              <option value="wide">Geniş plandan çizim</option>
              <option value="compose">Sıfırdan kurulum (poz, eylem, ölçek)</option>
            </select>
            {(sc.cut_attempts ?? []).length > 0 && <span className="dim small" style={{ marginLeft: 6 }}>{(sc.cut_attempts ?? []).map((m) => CUT_TR[m] ?? m).join(" → ")}</span>}
            {(sc.cut_attempts ?? []).includes("angles") && sc.angle_tokens && <div className="faint small mono">{sc.angle_tokens}</div>}
            {sc.cut_method_effective === "compose" && !sc.cut_method && <div className="faint small">
              Sahne sıfırdan kurulur: {[(sc.pose_words ?? []).length ? `yönetmen notunda poz/eylem (${(sc.pose_words ?? []).join(", ")})` : "", sc.scaled ? "gerçek boyutu yazılı karakter" : ""].filter(Boolean).join(" ve ")}.</div>}
            {sc.cut_method_effective !== "compose" && (sc.compose_hint ?? []).length > 0 && <div className="small">
              <span className="warn">Tarifte poz/eylem var ({(sc.compose_hint ?? []).join(", ")}):</span> önceki karede bu poz yoksa{" "}
              <button className="btn ghost" type="button" disabled={busy || frozen} onClick={() => pickCut("compose")}>sıfırdan kurulumu seç</button></div>}
          </dd>
        </>}
        {!refMode && !sc.cut_method_effective && sc.cut_method && <>
          <dt>Kesme</dt>
          <dd className="faint small">Çekim devam ediyor ({TRANSITION_TR[sc.transition ?? ""] ?? sc.transition}): seçili yöntem ({CUT_TR[sc.cut_method] ?? sc.cut_method}) uygulanmaz.</dd>
        </>}
        {!refMode && sc.board_method && <>
          <dt>Kare yolu</dt>
          <dd className="small">{CUT_TR[sc.board_method] ?? sc.board_method}<span className="faint"> (son çizim)</span></dd>
        </>}
        {sc.entry_options && <>
          <dt>Giriş</dt>
          <dd>
            <select value={sc.entry_mode ?? "cut"} disabled={busy || frozen} aria-label="Karakter girişi"
              title={`Önceki çekimde olmayan ${(sc.entering ?? []).join(", ")} bu çekimde aynı mekânda beliriyor`}
              onChange={(e) => pickEntry(e.target.value)}>
              <option value="cut">Kesme: kamera belirgin biçimde değişir</option>
              <option value="walk_in">Devam: {(sc.entering ?? []).join(", ")} sağdan girer</option>
            </select>
          </dd>
        </>}
        <dt>Süre</dt><dd className="mono">{Number(sc.duration_s).toFixed(1)} sn</dd>
        <dt>Aksiyon</dt><dd className="mono">{Number(sc.action_complexity).toFixed(2)}</dd>
        <dt>Motor</dt>
        <dd>
          <select value={sc.engine ?? meta?.video?.draft_engine ?? "h3max"} disabled={busy || frozen} aria-label="Video motoru"
            title={frozen ? FROZEN_TITLE : "H3 Max (fal.ai) taslak, Seedance 2.5 (fal.ai) final; Seedance 2.0 fast yalnızca seçtiğiniz çekimlerde. MiniMax H3 (RunPod) kapalı."}
            onChange={(e) => pickEngine(e.target.value)}>
            {engines.map((e) => <option key={e.id} value={e.id} disabled={e.disabled && e.id !== sc.engine}>{e.label}</option>)}
          </select>
        </dd>
        <dt>Karakterler</dt><dd>{sc.characters.filter((c) => !sc.chain_cast?.includes(c)).join(", ") || "—"}
          {(sc.chain_cast?.length ?? 0) > 0 && <span className="tag info" style={{ marginLeft: 6 }}
            title="Önceki çekimin karesinde kaldılar (senaryoda çıktıkları yazılmadı): referansları, değişmezleri ve kimlik kapısıyla çekime eklendi">
            zincirden gelen: {sc.chain_cast!.join(", ")}</span>}</dd>
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
      {engineAsk && <SceneEngineConfirm key={`${sc.id}:${engineAsk}`} sc={sc} engine={engineAsk} shotOnly onClose={() => setEngineAsk(null)} />}
      {sc.style_stale && (
        <p className="hint small" role="note">
          <span className="warn">Eski stil</span>{sc.style_stale.version != null ? ` (sürüm ${sc.style_stale.version})` : ""}: bu klip etkin stil
          kılavuzundan önce üretildi. Stil değişikliği çekimi yeniden üretmez; seçim, ses ve renk mevcut klip üzerinde çalışır.
          Yeni stille üretmek için "Bu sahneyi baştan üret".</p>
      )}
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
      {sc.upgrade_available && <UpgradeBox key={sc.id} sc={sc} plan={plan} />}
    </Section>
  );
}

/** Karma üretim: kazanan klip parçanın kardeşlerinden başka bir üretimden. İki çıkış: bütün sahne bu üretime geçer (tek
    tık, yeniden üretim ve ücret yok; üretimin bir çekimde kesimi yoksa 409 ve sahneyi o motorla yeniden üretme onayı) ya
    da bu çekim ayrı üretilir (küçük onay: bu çekim ve sonraki çekim son karelerle bağlanıp yeniden üretilir). */
function MixedBox({ sc }: { sc: Scene }) {
  const { busy, frozen, act, note, setVar } = usePanel();
  const g = sc.generation!;
  const [ask, setAsk] = useState(false);
  const [conflict, setConflict] = useState<string | null>(null);
  const [rerender, setRerender] = useState(false);
  const name = ENGINE_TR[g.engine] ?? g.engine;
  const actions = mixActions(g);
  const adopt = (variationId: string) => act(async () => {
    setConflict(null);
    try {
      const r = await api.post<AdoptResult>(`/scenes/${sc.id}/generation/adopt?variation_id=${encodeURIComponent(variationId)}`);
      setVar(null);
      note("inspector", adoptText(r, ENGINE_TR[r.engine] ?? r.engine), "ok");
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 409)) throw e;
      setConflict(e.message);
      note("inspector", e.message, "err");
    }
  }, "inspector");
  return (
    <div className="hint stack" role="group" aria-label="Karma üretim">
      <span>{g.message}</span>
      {g.mismatched.length > 0 && <span className="small">Farklı üretimden: <span className="mono">{g.mismatched.join(", ")}</span>
        <span className="faint"> · bu çekimin klibi {name} üretiminden</span></span>}
      <div className="row">
        {actions.map((a) => a.action === "adopt"
          ? <button key="adopt" className="btn primary" type="button" disabled={busy || frozen}
              title={frozen ? FROZEN_TITLE : `Bu üretimin kesimleri parçanın bütün çekimlerinin kazananı olur ve çekimler ${name} motoruna geçer; yeniden üretim ve ücret yok`}
              onClick={() => adopt(a.variationId)}>{a.label}</button>
          : <button key="shot" className="btn" type="button" aria-expanded={ask} disabled={busy || frozen}
              title={frozen ? FROZEN_TITLE : "Bu çekim kendi parçası olur; onaydan önce ne olacağı gösterilir"}
              onClick={() => setAsk(!ask)}>{a.label}</button>)}
      </div>
      {conflict && (
        <div className="stack" style={{ gap: 4 }}>
          <span className="bad small">{conflict}</span>
          <div className="row">
            <button className="btn" type="button" aria-expanded={rerender} disabled={busy || frozen} onClick={() => setRerender(!rerender)}
              title="Bütün sahne bu motorla yeniden üretilir: parça başına tahmin ve kalan bütçe gösterilir">Sahneyi {name} ile yeniden üret</button>
          </div>
          {rerender && <SceneEngineConfirm sc={sc} engine={g.engine} onClose={() => setRerender(false)} />}
        </div>
      )}
      {ask && (() => {
        const a = actions.find((x) => x.action === "shot");
        return a?.action === "shot" ? <ShotEngineConfirm sc={sc} engine={a.engine} onClose={() => setAsk(false)} /> : null;
      })()}
    </div>
  );
}

/** Bütün sahnenin motorunu değiştirme onayı: açılınca kuru çalıştırma (scene-engine?dry_run=true; fal.ai'ye gidilmez)
    parçaları, toplamı ve kalan bütçeyi gösterir; onay gösterilen toplamla gider (tahmin yükselmişse arka uç reddeder) ve
    planları da onaylar. shotOnly: ikincil seçenek, yalnızca bu çekim (ayrı üretim, son kareyle bağlanır). */
function SceneEngineConfirm({ sc, engine, onClose, shotOnly = false }: { sc: Scene; engine: string; onClose: () => void; shotOnly?: boolean }) {
  const { busy, frozen, act, note, setVar } = usePanel();
  const [plan, setPlan] = useState<SceneEnginePlan | null>(null);
  const [err, setErr] = useState("");
  const name = ENGINE_TR[engine] ?? engine;
  useEffect(() => {
    let live = true;
    setErr("");
    api.post<SceneEnginePlan>(`/scenes/${sc.id}/scene-engine?engine=${encodeURIComponent(engine)}&dry_run=true`)
      .then((r) => { if (live) setPlan(r); })
      .catch((e: Error) => { if (live) setErr(e.message); });
    return () => { live = false; };
  }, [sc.id, engine]);
  const confirm = () => act(async () => {
    if (!plan) return;
    const r = await api.post<SceneEngineResult>(`/scenes/${sc.id}/scene-engine?engine=${encodeURIComponent(engine)}&expected_usd=${plan.total_usd}`);
    onClose();
    setVar(null);
    note("inspector", sceneEngineText(r, ENGINE_TR[r.engine] ?? r.engine), "ok");
  }, "inspector");
  const [single, setSingle] = useState(false);
  return (
    <div className="hint stack mt8" role="group" aria-label={`Sahnenin motorunu ${name} yapma onayı`} aria-busy={!plan && !err}>
      <span>Model değişimi bütün sahneye uygulanır: sahnenin çekimleri {name} ile yeniden üretilir.</span>
      {err && <p className="bad small">{err}</p>}
      {!plan && !err && <span className="faint small">Tahmin hesaplanıyor…</span>}
      {plan && (
        <>
          <span className="small">Çekimler: <span className="mono">{plan.shots.join(", ")}</span></span>
          {plan.parts.length > 0 && (
            <ul className="lines">
              {plan.parts.map((x) => (
                <li key={x.part.join("+")}><span className="mono">~{usdTr(x.usd)}</span> · <span className="mono">{x.part.join(" + ")}</span>
                  <span className="faint"> · tek istek</span>
                  {x.endpoint && <div className="mono small faint" style={{ overflowWrap: "anywhere" }}>{x.endpoint}</div>}</li>
              ))}
            </ul>
          )}
          <dl className="kv">
            <dt>Toplam</dt><dd className="mono">~{usdTr(plan.total_usd)}</dd>
            <dt>Kalan bütçe</dt><dd className="mono">{usdTr(plan.remaining_usd)}</dd>
          </dl>
          {!plan.budget_ok && <p className="warn small">Bütçe yetmiyor: tahmini {usdTr(plan.total_usd)}, kalan {usdTr(plan.remaining_usd)}.</p>}
          <span className="faint small">Bu onay yeni planları da onaylar; eski klipler varyasyon olarak kalır.</span>
        </>
      )}
      <div className="row">
        <button className="btn primary" type="button" disabled={busy || frozen || !plan || !plan.budget_ok}
          title={frozen ? FROZEN_TITLE : undefined} onClick={confirm}>
          Onayla: bütün sahne {name}{plan ? ` (~${usdTr(plan.total_usd)})` : ""}</button>
        {shotOnly && (
          <button className="btn" type="button" aria-expanded={single} disabled={busy || frozen} onClick={() => setSingle(!single)}
            title="Bu çekim kendi parçası olur ve önceki çekimin son karesiyle başlar; sonraki çekim bu çekimin son karesiyle yeniden bağlanır (ikisi yeniden üretilir). Onaydan önce maliyet gösterilir">
            {SHOT_ONLY_LABEL}</button>
        )}
        <button className="btn ghost" type="button" onClick={onClose}>Vazgeç</button>
      </div>
      {shotOnly && !single && <span className="faint small">{BOUNDARY_NOTE}</span>}
      {shotOnly && single && <ShotEngineConfirm sc={sc} engine={engine} onClose={() => { setSingle(false); onClose(); }} />}
    </div>
  );
}

/** "Bu çekimi ayrı üret" onayı: açılınca kuru çalıştırma (shot-engine?dry_run=true; fal.ai'ye gidilmez) yeniden
    üretilecek parçaları, toplamı ve kalan bütçeyi gösterir; onay gösterilen toplamla gider (tahmin yükselmişse arka uç
    reddeder) ve parçaların planını da onaylar. */
function ShotEngineConfirm({ sc, engine, onClose }: { sc: Scene; engine: string; onClose: () => void }) {
  const { busy, frozen, act, note, setVar } = usePanel();
  const [plan, setPlan] = useState<ShotEnginePlan | null>(null);
  const [err, setErr] = useState("");
  const name = ENGINE_TR[engine] ?? engine;
  useEffect(() => {
    let live = true;
    setErr("");
    api.post<ShotEnginePlan>(`/scenes/${sc.id}/shot-engine?engine=${encodeURIComponent(engine)}&dry_run=true`)
      .then((r) => { if (live) setPlan(r); })
      .catch((e: Error) => { if (live) setErr(e.message); });
    return () => { live = false; };
  }, [sc.id, engine]);
  const confirm = () => act(async () => {
    if (!plan) return;
    const r = await api.post<ShotEngineResult>(`/scenes/${sc.id}/shot-engine?engine=${encodeURIComponent(engine)}&expected_usd=${plan.total_usd}`);
    onClose();
    setVar(null);
    note("inspector", shotEngineText(r, ENGINE_TR[r.engine] ?? r.engine, sc.scene_code), "ok");
  }, "inspector");
  const others = plan ? plan.shots.filter((c) => c !== sc.scene_code) : [];
  return (
    <div className="stack" role="group" aria-label="Çekimi ayrı üretme onayı" aria-busy={!plan && !err} style={{ gap: 4 }}>
      <span className="small"><span className="mono">{sc.scene_code}</span> {name} ile ayrı üretilir: önceki çekimin gerçek son karesiyle başlar.
        Sonraki çekim de bu çekimin son karesiyle yeniden bağlanır (ücretli).</span>
      <span className="faint small">{BOUNDARY_NOTE}</span>
      {err && <p className="bad small">{err}</p>}
      {!plan && !err && <span className="faint small">Tahmin hesaplanıyor…</span>}
      {plan && (
        <>
          {others.length > 0 && <span className="small">Yeniden üretilecek diğer çekimler: <span className="mono">{others.join(", ")}</span></span>}
          {plan.parts.length > 0 && (
            <ul className="lines">
              {plan.parts.map((x) => (
                <li key={x.part.join("+")}><span className="mono">~{usdTr(x.usd)}</span> · <span className="mono">{x.part.join(" + ")}</span>
                  <span className="faint"> · tek istek</span></li>
              ))}
            </ul>
          )}
          <dl className="kv">
            <dt>Toplam</dt><dd className="mono">~{usdTr(plan.total_usd)}</dd>
            <dt>Kalan bütçe</dt><dd className="mono">{usdTr(plan.remaining_usd)}</dd>
          </dl>
          {!plan.budget_ok && <p className="warn small">Bütçe yetmiyor: tahmini {usdTr(plan.total_usd)}, kalan {usdTr(plan.remaining_usd)}.</p>}
        </>
      )}
      <div className="row">
        <button className="btn primary" type="button" disabled={busy || frozen || !plan || !plan.budget_ok}
          title={frozen ? FROZEN_TITLE : undefined} onClick={confirm}>
          Onayla ve ayrı üret{plan ? ` (~${usdTr(plan.total_usd)})` : ""}</button>
        <button className="btn ghost" type="button" onClick={onClose}>Vazgeç</button>
      </div>
    </div>
  );
}

/** "Seedance 2.5 ile yükselt": tek adımlı onay. Kapsam (referans modunda bütün sahne), parça başına tahmin, toplam ve
    kalan bütçe gösterilir; onay upgrade-final'e gösterilen toplamla gider (tahmin yükselmişse arka uç reddeder). */
function UpgradeBox({ sc, plan }: { sc: Scene; plan: FalPlan | undefined }) {
  const { busy, frozen, act, note, setVar, meta, ov } = usePanel();
  const [open, setOpen] = useState(false);
  const up = plan?.upgrade;
  const finalEngine = up?.engine ?? (ov?.project.settings.final_engine as string | null | undefined) ?? meta?.video?.final_engine ?? "seedance25";
  const finalName = engineShort(finalEngine, up?.engine_label);
  const total = up?.total_usd ?? up?.cost?.reserve_usd;
  const why = !up ? "" : !up.available ? up.reason || "Yükseltme şu an uygun değil." : up.budget_ok === false ? up.reason || "Bütçe yetmiyor." : "";
  const shots = up?.shots?.length ? up.shots : [sc.scene_code];
  const confirm = () => act(async () => {
    const r = await api.post<UpgradeFinal>(`/scenes/${sc.id}/upgrade-final?expected_usd=${total}`);
    setOpen(false);
    setVar(null);
    note("inspector", r.blocked ? resultText(r) : upgradeText(r, ENGINE_TR[r.engine] ?? r.engine, sc.scene_code), r.blocked ? "err" : "ok");
  }, "inspector");
  return (
    <>
      <div className="row">
        <button className="btn good" type="button" aria-expanded={open} disabled={busy || frozen || !up || total == null || Boolean(why)}
          title={frozen ? FROZEN_TITLE : why || `Kapsam, parça başına tahmin ve kalan bütçe gösterilir; ${ENGINE_TR[finalEngine] ?? finalEngine} ile üretim onaydan sonra başlar`}
          onClick={() => setOpen(!open)}>{finalName} ile yükselt</button>
        <span className="faint small">{total != null ? <>tahmini <span className="mono">~{usdTr(total)}</span>{up?.scope === "scene" ? ` · sahnenin ${shots.length} çekimi` : ""}</> : up ? "" : "tahmin hesaplanıyor…"}</span>
      </div>
      {why && <p className="warn small">{why}</p>}
      {open && up?.available && total != null && (
        <div className="hint stack" role="group" aria-label={`${finalName} ile yükseltme onayı`}>
          <span>{up.scope === "scene" ? <>Sahnenin tamamı yükseltilir ({shots.length} çekim): <span className="mono">{shots.join(", ")}</span></>
            : <>Yalnızca <span className="mono">{sc.scene_code}</span> yükseltilir.</>}</span>
          {(up.parts?.length ?? 0) > 0 && (
            <ul className="lines">
              {up.parts!.map((x) => (
                <li key={x.part.join("+")}><span className="mono">~{usdTr(x.usd)}</span> · <span className="mono">{x.part.join(" + ")}</span>
                  <span className="faint"> · {sec1(x.duration_s)} sn, tek istek</span></li>
              ))}
            </ul>
          )}
          <dl className="kv">
            <dt>Toplam</dt><dd className="mono">~{usdTr(total)}</dd>
            <dt>Kalan bütçe</dt><dd className="mono">{usdTr(up.remaining_usd)}</dd>
          </dl>
          <span className="faint">Bu onay final motorunun planlarını da onaylar; ayrıca plan onayı istenmez. Taslak klipler varyasyon olarak kalır.</span>
          <div className="row">
            <button className="btn good" type="button" disabled={busy || frozen} onClick={confirm}>Onayla ve yükselt (~{usdTr(total)})</button>
            <button className="btn ghost" type="button" onClick={() => setOpen(false)}>Vazgeç</button>
          </div>
        </div>
      )}
    </>
  );
}

function H3({ sc, fal = false }: { sc: Scene; fal?: boolean }) {
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
  const impactOk = async (what: string) => {
    const i = await api.get<{ frames: number; clips: number; estimate_usd: number; clip_codes: string[] }>(`/scenes/${sc.id}/chain-impact`);
    if (i.frames <= 1 && i.clips === 0) return true;
    return window.confirm(`${what}: ${i.frames} kare ve ${i.clips} klip yeniden üretilecek${i.clips ? ` (${i.clip_codes.join(", ")})` : ""}, tahmini $${i.estimate_usd.toFixed(2)}. Devam edilsin mi?`);
  };
  const redraw = () => act(async () => {
    if (!(await impactOk("Kare yeniden çizilirse"))) return;
    await api.post(`/scenes/${sc.id}/board/draw?paid=true`);
    note("inspector", `${sc.scene_code}: ilk kare yeni seed'lerle yeniden çiziliyor (her deneme ücretli).`, "ok");
  }, "inspector");
  const accept = () => act(async () => {
    if (!(await impactOk("Kare onaylanıp zincir devam ederse"))) return;
    const r = await api.post<{ stages: string[]; frozen: boolean; chain_continues?: string[] }>(`/scenes/${sc.id}/board/accept`);
    note("inspector", r.frozen ? `${sc.scene_code}: kare elle onaylandı. Proje donmuş: üretime devam edince video üretilecek.`
      : r.chain_continues ? `${sc.scene_code}: kare elle onaylandı; zincir sürüyor${r.chain_continues.length ? ` (${r.chain_continues.join(", ")} çiziliyor)` : ""}. Videolar şerit onayıyla başlar.`
      : `${sc.scene_code}: kare elle onaylandı; ${r.stages.length ? "video aşamasına geçildi." : "üretilecek aşama yok."}`, "ok");
  }, "inspector");
  const setTransition = (transition_in: string) => act(async () => {
    await api.patch(`/scenes/${sc.id}`, { transition_in });
    note("inspector", `${sc.scene_code}: geçiş ${transition_in === "auto" ? "senaryodaki gibi" : TRANSITION_TR[transition_in]?.split(":")[0]}. Kare zinciri buna göre yeniden çizilir.`, "ok");
  }, "inspector");
  const redrawEnd = () => act(async () => {
    await api.post(`/scenes/${sc.id}/board/end/redraw`);
    note("inspector", `${sc.scene_code}: son kare yeni seed'le yeniden çiziliyor.`, "ok");
  }, "inspector");
  const requested = sc.board_status === "changes_requested" && Boolean(sc.board_url);
  const redrawChain = () => act(async () => {
    if (requested && !(await impactOk("Bu kare ve zincirde ondan türeyenler yeniden çizilirse"))) return;
    const r = await api.post<{ codes: string[] }>(`/scenes/${sc.id}/board/redraw-chain`);
    note("inspector", r.codes.length ? `Sonraki ${r.codes.length} kare sırayla yeniden çiziliyor: ${r.codes.join(", ")}.` : "Yeniden çizilecek kare yok.", "ok");
  }, "inspector");
  const source = sc.keyframe_source === "upload" ? "upload" : sc.board_source;
  const review = sc.board_status === "changes_requested" && (sc.board_source === "qwen_edit" || sc.board_source === "comfyui" || sc.board_source === "flux");
  const editRefs = (fal || sc.h3_mode === "fl2va") && sc.keyframe_source !== "upload" && sc.keyframe_model === "qwen_edit" ? sc.edit_refs ?? [] : [];
  return (
    <Section title={fal ? "Başlangıç karesi ve zincir" : "H3 koşulu"} meta={fal ? (sc.keyframe_url ? (sc.board_status === "approved" || sc.keyframe_source === "upload" ? "onaylı" : "onay bekliyor") : "kare yok") : sc.h3_mode === "ref2va" ? "Ref2VA" : "FL2VA"}
      open={!fal || sc.board_status === "changes_requested"}>
      {!fal && <label className="field">Kompozisyon nereden gelsin
        <select value={sc.h3_mode_explicit ?? "auto"} disabled={busy || frozen} aria-label="H3 koşulu" title={frozen ? FROZEN_TITLE : undefined}
          onChange={(e) => pickMode(e.target.value)}>
          <option value="auto">Otomatik (şu an: {sc.h3_mode === "ref2va" ? "Ref2VA" : "FL2VA"})</option>
          {Object.entries(H3_MODE_TR).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </label>}
      {!fal && sc.h3_mode_warning && <p className="warn small">{sc.h3_mode_warning}</p>}
      <label className="field">Çekime geçiş
        <select value={sc.transition_explicit ?? "auto"} disabled={busy} aria-label="Çekime geçiş" onChange={(e) => setTransition(e.target.value)}>
          <option value="auto">Senaryodaki gibi (şu an: {TRANSITION_TR[sc.transition ?? "continuous"]?.split(":")[0]})</option>
          {Object.entries(TRANSITION_TR).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </label>
      {sc.transition_reason && !sc.transition_explicit && (
        <p className="faint small">Otomatik sınıflama: {sc.transition_reason}.</p>
      )}
      {sc.arrival && (
        <p className="small">Varış (kazanan klip): <span className="mono">{sc.arrival.score.toFixed(2)}</span>
          <span className="faint"> · son kare {sc.arrival.target === "end" ? "çekimin son karesine" : "sonraki çekimin karesine"} (DINOv2 {sc.arrival.dino.toFixed(2)}{sc.arrival.identity != null ? `, kimlik ${sc.arrival.identity.toFixed(2)}` : ""})</span></p>
      )}
      {sc.chain_parent && (
        <p className={sc.chain_stale ? "warn small" : "faint small"}>
          {sc.chain_stale ? `Kare eski: zincirdeki önceki kare (${sc.chain_parent}) değişti; videodan önce yeniden çizilecek.`
            : `Kare ${sc.chain_parent} karesinden devam eder (anahtar kare zinciri).`}
        </p>
      )}
      {((sc.chain_redraw?.length ?? 0) > 0 || requested) && (
        <div className="row">
          <button className="btn" type="button" disabled={busy || frozen} title={frozen ? FROZEN_TITLE : requested
            ? "Değişiklik istenen kare yeni seed'lerle ve yönetmen notuyla, ardından zincirde ondan türeyen kareler sırayla çizilir (ücretli)"
            : "Zincirde bu kareden türeyen eski ya da eksik kareler sırayla çizilir (ücretli)"}
            onClick={redrawChain}>{requested ? "Bu kareyi ve sonrakileri yeniden çiz" : `Sonraki ${sc.chain_redraw!.length} kareyi yeniden çiz`}</button>
        </div>
      )}
      <p className="faint small">{fal ? "Onaylı başlangıç karesi varsa image-to-video bu kareden başlar; yoksa reference-to-video referans görsellerini kullanır."
        : "Aynı seed farklı çözünürlükte farklı kompozisyon verir; test turu ve tam tur bu ilk kareden başlar."}</p>
      {sc.end_board_url && (
        <div style={{ display: "grid", gridTemplateColumns: "120px 1fr", gap: 10, alignItems: "start" }}>
          <img src={sc.end_board_url} alt="Çekimin son karesi" style={{ width: 120, aspectRatio: "16 / 9", objectFit: "cover", borderRadius: 2, opacity: sc.end_board_current ? 1 : 0.5 }} />
          <div className="stack">
            <span className="small">Son kare: video bu karede biter (sonraki çekim kesmeyle gelir; eylem tamamlanmış hâli).</span>
            {!sc.end_board_current && <span className="warn small">Eski: ilk kare değişti, videodan önce yeniden çizilecek.</span>}
            {sc.end_board_note && <span className={sc.end_board_note.startsWith("İnceleme") ? "warn small" : "bad small"}>{sc.end_board_note}</span>}
            <div className="row">
              <button className="btn" type="button" disabled={busy || frozen} title={frozen ? FROZEN_TITLE : "Qwen-Image-Edit Lightning, yeni seed (~0,007 $); bu çekimin klipleri eski son kareden üretildiyse yeniden üretilir"}
                onClick={redrawEnd}>Son kareyi yeniden çiz</button>
            </div>
          </div>
        </div>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "120px 1fr", gap: 10, alignItems: "start" }}>
        {sc.keyframe_url
          ? <img src={sc.keyframe_url} alt="İlk kare" style={{ width: 120, aspectRatio: "16 / 9", objectFit: "cover", borderRadius: 2 }} />
          : <span className="faint small" style={{ width: 120 }}>İlk kare yok</span>}
        <dl className="kv">
          <dt>İlk kare</dt><dd>{source ? BOARD_SOURCE_TR[source] ?? source : "storyboard karesi bekleniyor"}</dd>
          <dt>Skor</dt><dd className="mono">{sc.keyframe_source === "upload" ? "yönetmen" : sc.board_score != null ? sc.board_score.toFixed(3) : "—"}</dd>
          <dt>Kare kararı</dt><dd>{sc.board_status === "changes_requested" ? <span className="warn">{sc.board_note || "değişiklik istendi"}</span> : sc.board_status === "approved" ? "onaylı" : "onay bekliyor"}</dd>
          {(fal || sc.h3_mode === "fl2va") && sc.keyframe_source !== "upload" && sc.keyframe_model && (
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
      {!fal && sc.h3_mode === "ref2va" && (
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

/** fal.ai video (H3 Max, Seedance 2.5): koşul (image-to-video / reference-to-video), çözünürlük, süre, model sesi,
    tahmini maliyet, referans numaralandırması, bekleyen iş ve kuru çalıştırma (istek gövdesi; fal.ai'ye gidilmez). */
function FalVideo({ sc }: { sc: Scene }) {
  const { busy, frozen, act, note, meta, ov } = usePanel();
  const plan = useFalPlan(sc);
  const refMode = useRefMode(sc);
  const engine = sc.engine ?? "h3max";
  const [dry, setDry] = useState<FalPlan | null>(null);
  const [dur, setDur] = useSynced(String(sc.duration_s).replace(".", ","), `${sc.id}:${sc.duration_s}`);
  const later = refMode ? "Plan değişti: yeniden inceleyip onaylayın." : "Uygulamak için \"Bu sahneyi baştan üret\".";
  const patch = (body: Record<string, unknown>, text: string) => act(async () => {
    await api.patch(`/scenes/${sc.id}`, body);
    setDry(null);
    note("inspector", `${sc.scene_code}: ${text}. ${later}`, "ok");
  }, "inspector");
  const saveDuration = () => {
    const x = parseDuration(dur);
    if (x == null) { note("inspector", "Süre 2 ile 15 sn arasında olmalı.", "err"); return; }
    if (x === Number(sc.duration_s)) return;
    void patch({ duration_s: x }, `süre ${String(x).replace(".", ",")} sn`);
  };
  const dryRun = () => act(async () => {
    setDry(await api.post<FalPlan>(`/scenes/${sc.id}/render?dry_run=true`));
  }, "inspector");
  const res = resolutionsFor(engine, meta?.video);
  const cfg = (ov?.project.settings ?? {}) as Record<string, unknown>;
  const draftOn = engine === "seedance25" && Boolean(plan?.draft ?? (cfg.seedance25_draft as boolean | null | undefined) ?? meta?.video?.seedance25_draft);
  const projectRes = (engine === "h3max" ? cfg.h3max_resolution : engine === "seedance25" ? cfg.seedance25_resolution : null) as string | null | undefined;
  const autoRes = draftOn ? "taslak 480p" : projectRes || defaultResolution(engine, meta?.video);
  const mode = plan?.mode ? FAL_MODE_TR[plan.mode] : "…";
  const req = sc.fal_request;
  const refs = plan?.references ?? [];
  return (
    <Section title="fal.ai video" meta={`${engineShort(engine, sc.engine_label ?? undefined)} · ${refMode ? (plan?.part ? `${plan.part.length} çekim, tek istek` : "plan") : mode}`}>
      {!refMode && <><label className="field">Koşul
        <select value={modeValue(sc)} disabled={busy || frozen} aria-label="fal.ai video koşulu" title={frozen ? FROZEN_TITLE : undefined}
          onChange={(e) => patch({ h3_mode: e.target.value }, e.target.value === "auto" ? "koşul otomatik" : `koşul ${FAL_MODE_CHOICES[e.target.value]}`)}>
          <option value="auto">Otomatik (şu an: {mode})</option>
          {Object.entries(FAL_MODE_CHOICES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </label>
      {plan?.reason && <p className="faint small">{plan.reason}</p>}</>}
      <div className="form-row">
        <label className="field">Çözünürlük
          <select value={resolutionValue(sc)} disabled={busy || frozen} aria-label="Video çözünürlüğü"
            onChange={(e) => patch({ video_resolution: e.target.value }, e.target.value === "auto" ? "çözünürlük Ayarlar'daki gibi" : `çözünürlük ${e.target.value}`)}>
            <option value="auto">Otomatik ({autoRes})</option>
            {res.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
        </label>
        <label className="field">Süre (sn)
          <input type="text" inputMode="decimal" className="mono" style={{ width: 70 }} aria-label="Çekim süresi (sn), 2-15" disabled={busy || frozen}
            value={dur} onChange={(e) => setDur(e.target.value)} onBlur={saveDuration}
            onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); saveDuration(); } }} />
        </label>
        <label className="field">Model sesi
          <select value={audioValue(sc)} disabled={busy || frozen} aria-label="Modelin ürettiği ses"
            title="Modelin kendi sesi filmde kullanılmaz (diyalog ve müzik ayrı üretilir)"
            onChange={(e) => patch({ video_audio: e.target.value }, e.target.value === "auto" ? "model sesi Ayarlar'daki gibi" : `model sesi ${e.target.value === "true" ? "açık" : "kapalı"}`)}>
            <option value="auto">Otomatik{plan ? ` (${plan.audio ? "açık" : "kapalı"})` : ""}</option>
            <option value="true">Açık</option>
            <option value="false">Kapalı</option>
          </select>
        </label>
      </div>
      {draftOn && <p className="faint small">Seedance 2.5 taslağı açık: 480p önizleme üretilir (çözünürlük seçimi uygulanmaz); 7 gün içinde Bitirme bölümünden 1080p'ye tamamlanır ya da upscale edilir.</p>}
      {refMode && <PartPlan sc={sc} plan={plan} />}
      <dl className="kv">
        {!refMode && <>
          <dt>Tahmini maliyet</dt>
          <dd>{plan?.cost ? <><span className="mono">~{usdTr(plan.cost.reserve_usd)}</span><span className="faint small"> klip başına (bütçe kapısı bunu ayırır)</span></> : plan?.note || plan?.message || "hesaplanıyor…"}</dd>
          {plan?.endpoint && <><dt>Uç nokta</dt><dd className="mono small" style={{ overflowWrap: "anywhere" }}>{plan.endpoint}</dd></>}
          {plan && <PlanFinishing plan={plan} />}
        </>}
        {req && <>
          <dt>fal.ai işi</dt>
          <dd>
            <span className={`st ${req.status === "failed" ? "bad" : req.status === "completed" ? "ok" : "work"}`}>{FAL_STATUS_TR[req.status ?? ""] ?? req.status ?? "—"}</span>
            {req.request_id && <div className="mono small faint" style={{ overflowWrap: "anywhere" }}>{req.request_id}</div>}
            {req.status === "failed" && req.error && <div className="bad small" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{req.error}</div>}
          </dd>
        </>}
      </dl>
      {!refMode && refs.length > 0 && <>
        <h4 className="small dim">Referans numaraları ({refs.length})</h4>
        <ol className="lines">
          {refs.map((r) => <li key={`${r.token}-${r.key}`}><span className="mono">{r.token}</span> → {r.name} <span className="faint small">({TYPE_TR[r.kind] ?? r.kind})</span></li>)}
        </ol>
      </>}
      {(plan?.unmapped_negatives?.length ?? 0) > 0 && <p className="warn small">Kısıta çevrilemeyen negatifler: {plan!.unmapped_negatives!.join(", ")}</p>}
      {!refMode && (plan?.notes ?? []).map((n) => <p key={n} className="faint small">{n}</p>)}
      <div className="row">
        <button className="btn" type="button" disabled={busy} onClick={dryRun}
          title="İstek gövdesi, yüklenecek dosyalar (SHA-256), referanslar ve tahmin hesaplanır; hiçbir şey kuyruğa verilmez">Kuru çalıştır</button>
        {dry && <button className="btn ghost" type="button" onClick={() => setDry(null)}>Kapat</button>}
      </div>
      {dry && <DryRun r={dry} />}
    </Section>
  );
}

/** Referans modunun planı: parça (aynı sahnenin çekimleri, tek istek) ve zaman çizelgesi, motor, süre, tahmin,
    numaralı referanslar (süreklilik karesi dahil), düşen ve eksik referanslar, süreklilik, tam istem, uyarılar ve onay.
    Eksik referans varsa plan onaylanamaz ve üretilemez. */
function PartPlan({ sc, plan }: { sc: Scene; plan: FalPlan | undefined }) {
  const { busy, frozen, act, note, setVar } = usePanel();
  const setTransition = (transition_in: string) => act(async () => {
    await api.patch(`/scenes/${sc.id}`, { transition_in });
    note("inspector", `${sc.scene_code}: geçiş ${transition_in === "auto" ? "senaryodaki gibi" : TRANSITION_TR[transition_in]?.split(":")[0]}. Süreklilik buna göre yeniden planlanır; planı yeniden onaylayın.`, "ok");
  }, "inspector");
  if (!plan) return <p className="faint small">Plan hesaplanıyor…</p>;
  if (!plan.part) return <p className="faint small">{plan.message || plan.note || "Bu çekim için referans planı yok."}</p>;
  const approve = (render: boolean) => act(async () => {
    const r = await api.post<PlanApproval>(`/scenes/${sc.id}/plan/approve?render=${render}`, { plan_hash: plan.plan_hash });
    if (render) setVar(null);
    note("inspector", approveText(r, render), Object.values(r.dispatched).some((d) => d.blocked) ? "err" : "ok");
  }, "inspector");
  const missing: SceneAssetRef[] = (plan.missing ?? []).map((m) => ({ id: m.asset_id, type: m.kind, name: m.name, status: "no_image" }));
  const refs = plan.references ?? [];
  const c = plan.continuity;
  const approved = Boolean(plan.approved ?? plan.approval?.approved);
  const required = plan.approval?.required ?? true;
  const given = sc.plan_approval;
  const prompt = typeof plan.body?.prompt === "string" ? plan.body.prompt : null;
  const warnings = planWarnings(plan);
  const blocked = Boolean(plan.blocked);
  return (
    <>
      <p className="faint small">{plan.reason}</p>
      {missing.length > 0 && (
        <p className="warn small">Eksik referans görseli, video üretilemez: <MissingLinks list={missing} />. Varlıklar sayfasından görsel yükleyin.</p>
      )}
      <dl className="kv">
        <dt>Parça</dt>
        <dd>
          <ol className="lines">
            {(plan.timeline?.length ? plan.timeline : plan.part.map((code) => ({ code, start: NaN, end: NaN }))).map((x) => (
              <li key={x.code} aria-current={x.code === sc.scene_code ? "true" : undefined}
                style={x.code === sc.scene_code ? { borderColor: "var(--accent)" } : undefined}>
                <span className="mono">{x.code}</span>
                {Number.isFinite(x.start) && <span className="faint mono small"> {sec1(x.start)}–{sec1(x.end)} sn</span>}
                {x.code === sc.scene_code && <span className="faint small"> · bu çekim</span>}
              </li>
            ))}
          </ol>
          <span className="faint small">{plan.part.length > 1 ? `${plan.part.length} çekim tek istekte üretilir, klip çekimlere bölünür` : "tek çekim, tek istek"}{plan.render_mode === "per_shot" ? " (çekim başına mod)" : ""}</span>
        </dd>
        <dt>Motor</dt><dd>{plan.engine_label}</dd>
        <dt>Video</dt><dd className="mono">{[plan.resolution, plan.duration_s != null ? `${sec1(plan.duration_s)} sn` : "", plan.audio ? "sesli" : "sessiz"].filter(Boolean).join(" · ")}</dd>
        <dt>Tahmini maliyet</dt>
        <dd>{plan.cost ? <><span className="mono">~{usdTr(plan.cost.reserve_usd)}</span><span className="faint small"> tek istek (bütçe kapısı bunu ayırır)</span></> : "—"}</dd>
        {plan.endpoint && <><dt>Uç nokta</dt><dd className="mono small" style={{ overflowWrap: "anywhere" }}>{plan.endpoint}</dd></>}
        <PlanFinishing plan={plan} />
        <dt>Onay</dt>
        <dd>
          {approved ? <><span className="tag ok">Onaylı</span>{given && <span className="faint small"> {clock(given.at)} · ~{usdTr(given.usd)}</span>}</>
            : given ? <span className="warn small">Plan değişti: önceki onay ({clock(given.at)}, ~{usdTr(given.usd)}) geçersiz; yeniden onaylayın.</span>
            : required ? <><span className="tag warn">Onay bekliyor</span><span className="faint small"> üretim plan onaylanmadan başlamaz</span></>
            : <span className="faint small">Onay zorunlu değil (proje ayarı); yine de onaylanabilir.</span>}
        </dd>
      </dl>
      {(plan.dropped?.length ?? 0) > 0 && (
        <div className="hint">Referans sınırı: istekten düşenler {plan.dropped!.map((d) => `${d.name} (${TYPE_TR[d.kind] ?? d.kind})`).join(", ")}. Videoda bu varlıklar referanssız kalır.</div>
      )}
      {refs.length > 0 && <>
        <h4 className="small dim">Referans numaraları ({refs.length})</h4>
        <ol className="lines">
          {refs.map((r) => (
            <li key={`${r.token}-${r.key}`}><span className="mono">{r.token}</span> → {r.name}{" "}
              <span className="faint small">({r.kind === "frame" ? "süreklilik karesi" : TYPE_TR[r.kind] ?? r.kind})</span></li>
          ))}
        </ol>
      </>}
      {c && (
        <p className="faint small">
          Süreklilik: {TRANSITION_TR[c.transition]?.split(":")[0] ?? c.transition}
          {c.expected && c.previous ? `; ${c.previous} klibinin son karesi ilk referans olarak verilir` : ""}{c.note ? `. ${c.note}` : ""}
          {c.model_switch && <span className="tag warn" style={{ marginLeft: 6 }} title="Önceki çekim başka motorla üretildi; model değişimi sahne sınırında önerilir">sahne içi model değişimi</span>}
        </p>
      )}
      {plan.split && <SplitView split={plan.split} codes={plan.part} code={sc.scene_code} planned={plan.split.segment_planned} segment={plan.split.segment} />}
      <label className="field">Çekime geçiş
        <select value={sc.transition_explicit ?? "auto"} disabled={busy} aria-label="Çekime geçiş" onChange={(e) => setTransition(e.target.value)}
          title="Parçanın ilk çekimi devam ediyorsa önceki klibin son karesi ilk referanstır; kesmede yalnızca referanslar">
          <option value="auto">Senaryodaki gibi (şu an: {TRANSITION_TR[sc.transition ?? "continuous"]?.split(":")[0]})</option>
          {Object.entries(TRANSITION_TR).map(([k, v]) => <option key={k} value={k}>{v.split(":")[0]}</option>)}
        </select>
      </label>
      {prompt && <PromptBox sc={sc} plan={plan} prompt={prompt} />}
      {warnings.map((w) => <div key={w} className="hint">{w}</div>)}
      {plan.prompt_writer?.writing && <p className="faint small">LLM istemi yazılıyor; onaylamadan önce bekleyin (onay yazılan isteme bağlıdır).</p>}
      <div className="row">
        <button className="btn" type="button" disabled={busy || blocked || approved || Boolean(plan.prompt_writer?.writing)} onClick={() => approve(false)}
          title={blocked ? BLOCKED_TITLE : approved ? "Plan zaten onaylı" : "Plan parçanın bütün çekimleri için onaylanır; üretim başlamaz"}>Planı onayla</button>
        <button className="btn primary" type="button" disabled={busy || frozen || blocked} onClick={() => approve(true)}
          title={frozen ? FROZEN_TITLE : blocked ? BLOCKED_TITLE : `Plan onaylanır ve ${plan.part.join(", ")} tek istekte üretilir`}>Onayla ve üret</button>
      </div>
    </>
  );
}

/** Parçanın istemi: kaynağı (yönetmen istemi > LLM istemi > kurallar istemi), uzunluğu ve bedeli; "İstemi LLM ile
    yaz" (arka planda, ücretli; ücretsiz modda reddedilir) ve "Yönetmen istemi" düzenleyicisi. Kaydedilen istem olduğu
    gibi gönderilir, yalnızca referans yazımı motora göre çevrilir; kaydetmek planı (ve onayı) değiştirir, klibi yeniden
    üretmez. */
function PromptBox({ sc, plan, prompt }: { sc: Scene; plan: FalPlan; prompt: string }) {
  const { busy, frozen, act, note } = usePanel();
  const { mutate } = useSWRConfig();
  const [edit, setEdit] = useState<string | null>(null);
  const [rerender, setRerender] = useState(false);
  const key = `/scenes/${sc.id}/fal-plan`;
  const w = plan.prompt_writer;
  const source = plan.prompt_source ?? "rules";
  useEffect(() => {                                   // yazılırken plan birkaç saniyede bir yenilenir
    if (!w?.writing) return;
    const t = setInterval(() => void mutate(key), 3000);
    return () => clearInterval(t);
  }, [w?.writing, key, mutate]);
  const write = (force: boolean) => act(async () => {
    const r = await api.post<PromptWriteResult>(`/scenes/${sc.id}/prompt/write${force ? "?force=true" : ""}`);
    note("inspector", r.note ?? "LLM istemi yazılıyor.", "ok");
    await mutate(key);
  }, "inspector");
  const save = (text: string) => act(async () => {
    const r = await api.patch<{ hint: string }>(`/scenes/${sc.id}`, { prompt_override: text });
    setEdit(null);
    note("inspector", r.hint, "ok");
    await mutate(key);
  }, "inspector");
  return (
    <div className="stack" style={{ gap: 4 }} aria-label="Parçanın istemi">
      <span className="small">
        <span className={`tag ${source === "override" ? "info" : source === "llm" ? "ok" : ""}`}>{plan.prompt_source_label ?? "Kurallar istemi"}</span>{" "}
        <span className="faint">{promptSourceText(plan).split(" · ").slice(1).join(" · ")}</span>
      </span>
      {w?.override?.stale?.length ? <span className="caution small">Yönetmen istemi eski olabilir: {w.override.stale.join("; ")}.</span> : null}
      {edit === null ? (
        <details>
          <summary className="small">İstem (tam metin)</summary>
          <pre className="prompt" style={{ maxHeight: 320, overflow: "auto", margin: "6px 0 0" }}>{prompt}</pre>
        </details>
      ) : (
        <label className="field">Yönetmen istemi
          <textarea value={edit} rows={14} onChange={(e) => setEdit(e.target.value)} spellCheck={false}
            aria-describedby={`prompt-help-${sc.id}`} />
          <span id={`prompt-help-${sc.id}`} className="faint small">Olduğu gibi gönderilir; referansları [Image1], @Image1 ya da Image 1 yazabilirsiniz, motora göre çevrilir. Kaydetmek planı değiştirir (yeniden onay), klibi yeniden üretmez.</span>
        </label>
      )}
      <div className="row">
        {edit === null ? <>
          <button className="btn" type="button" disabled={busy} onClick={() => setEdit(prompt)}
            title="Gösterilen istemi düzenleyip bu parçanın istemi olarak kaydedin">Yönetmen istemi yaz</button>
          {source === "override" && (
            <button className="btn primary" type="button" aria-expanded={rerender} disabled={busy || frozen} onClick={() => setRerender(!rerender)}
              title={frozen ? FROZEN_TITLE : "Bütün parça bu istemle yeniden üretilir; onaydan önce tahmin ve kalan bütçe gösterilir"}>
              Bu istemle yeniden üret{plan.cost ? ` (≈${usdTr(plan.cost.reserve_usd)})` : ""}</button>
          )}
          {source === "override" && (
            <button className="btn ghost" type="button" disabled={busy} onClick={() => save("")}
              title="Yönetmen istemi silinir; plan LLM ya da kurallar istemine döner">Otomatik isteme dön</button>
          )}
          {w?.writer === "llm" && source !== "override" && (
            <button className="btn" type="button" disabled={busy || frozen || Boolean(w.writing)} onClick={() => write(Boolean(w.llm?.ok))}
              title={`Hermes modeli parçanın istemini yönetmen tarzında yazar (ücretli${w.estimate ? `, ~${usdExact(w.estimate.usd)}` : ""}); girdi değişmedikçe yeniden yazılmaz`}>
              {w.llm?.ok ? "LLM ile yeniden yaz" : "İstemi LLM ile yaz"}</button>
          )}
        </> : <>
          <button className="btn primary" type="button" disabled={busy || !edit.trim()} onClick={() => save(edit)}>Yönetmen istemini kaydet</button>
          <button className="btn ghost" type="button" onClick={() => setEdit(null)}>Vazgeç</button>
          {plan.prompt_rules && <button className="btn ghost" type="button" onClick={() => setEdit(plan.prompt_rules!)}
            title="Düzenlemeye kurallar isteminden başlayın">Kurallar isteminden başla</button>}
        </>}
      </div>
      {rerender && <PromptRerenderConfirm sc={sc} onClose={() => setRerender(false)} />}
    </div>
  );
}

/** "Bu istemle yeniden üret" onayı: açılınca kuru çalıştırma (prompt/rerender?dry_run=true) parçayı, istemin kaynağını,
    tahmini ve kalan bütçeyi gösterir; onay gösterilen tutarla gider (tahmin yükselmişse arka uç reddeder), planı onaylar
    ve bütün parçayı yeniden üretir. */
function PromptRerenderConfirm({ sc, onClose }: { sc: Scene; onClose: () => void }) {
  const { busy, frozen, act, note, setVar } = usePanel();
  const { mutate } = useSWRConfig();
  const [plan, setPlan] = useState<PromptRerender | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let live = true;
    api.post<PromptRerender>(`/scenes/${sc.id}/prompt/rerender?dry_run=true`)
      .then((r) => { if (live) setPlan(r); })
      .catch((e: Error) => { if (live) setErr(e.message); });
    return () => { live = false; };
  }, [sc.id]);
  const confirm = () => act(async () => {
    if (!plan) return;
    const r = await api.post<PromptRerender>(`/scenes/${sc.id}/prompt/rerender?expected_usd=${plan.total_usd}`);
    onClose();
    setVar(null);
    note("inspector", `${r.part.join(", ")} ${r.source_label.toLocaleLowerCase("tr")} ile yeniden üretiliyor (tahmini ~${usdTr(r.total_usd)}). ${dispatchedText(r.dispatched ?? {})}`.trim(), "ok");
    await mutate(`/scenes/${sc.id}/fal-plan`);
  }, "inspector");
  return (
    <div className="hint stack" role="group" aria-label="Bu istemle yeniden üretme onayı" aria-busy={!plan && !err}>
      {err && <p className="bad small">{err}</p>}
      {!plan && !err && <span className="faint small">Tahmin hesaplanıyor…</span>}
      {plan && <>
        <span className="small">Bütün parça yeniden üretilir: <span className="mono">{plan.part.join(" + ")}</span> ({plan.source_label}, {plan.prompt_chars.toLocaleString("tr-TR")} karakter). Eski klipler varyasyon olarak kalır.</span>
        <dl className="kv">
          <dt>Tahmin</dt><dd className="mono">~{usdTr(plan.total_usd)}</dd>
          <dt>Kalan bütçe</dt><dd className="mono">{usdTr(plan.remaining_usd)}</dd>
        </dl>
        {plan.blocked && <p className="warn small">Eksik ya da onaysız referans var: önce varlıkları tamamlayın.</p>}
        {!plan.budget_ok && <p className="warn small">Bütçe yetmiyor: tahmini {usdTr(plan.total_usd)}, kalan {usdTr(plan.remaining_usd)}.</p>}
      </>}
      <div className="row">
        <button className="btn primary" type="button" disabled={busy || frozen || !plan || !plan.budget_ok || plan.blocked}
          title={frozen ? FROZEN_TITLE : undefined} onClick={confirm}>
          Onayla ve yeniden üret{plan ? ` (~${usdTr(plan.total_usd)})` : ""}</button>
        <button className="btn ghost" type="button" onClick={onClose}>Vazgeç</button>
      </div>
    </div>
  );
}

/** Kazanan parça klibinin bölünmesi: bu çekimin kesmeleri (gerçek ve planlı), her iç sınırın kaynağı ve uyarı. */
export function SplitView({ split, codes, code, segment, planned }: {
  split: PartSplit; codes: string[] | null | undefined; code: string;
  segment: [number, number] | null | undefined; planned: [number, number] | null | undefined;
}) {
  const edges = shotBoundaries(split, segment, planned, codes, code);
  const sources = boundarySources(split, codes);
  return (
    <div className="stack" style={{ gap: 2 }} aria-label="Klibin bölünmesi">
      {edges.map((b) => <span key={`${b.from}-${b.to}`} className="small mono">{b.text}</span>)}
      {segment && <span className="faint small mono">Bu çekim {sec1(segment[0])}–{sec1(segment[1])} sn{planned ? ` · planlı ${sec1(planned[0])}–${sec1(planned[1])} sn` : ""}</span>}
      <span className="faint small">{cutsText(split)}{sources.length ? ` · ${sources.join("; ")}` : ""}</span>
      {split.warning && <span className="caution small">{split.warning}</span>}
    </div>
  );
}

function DryRun({ r }: { r: FalPlan }) {
  return (
    <div className="stack">
      <div className="note ok">Kuru çalıştırma: fal.ai'ye hiçbir istek gönderilmedi; hiçbir şey kuyruğa verilmedi ve hiçbir dosya yüklenmedi.</div>
      {(r.disabled || r.note || r.message) && <p className="warn small">{r.message || r.note}</p>}
      <dl className="kv">
        {r.endpoint && <><dt>Uç nokta</dt><dd className="mono small" style={{ overflowWrap: "anywhere" }}>{r.endpoint}</dd></>}
        {r.mode && <><dt>Koşul</dt><dd>{r.mode_label ?? FAL_MODE_TR[r.mode]}{r.reason && <div className="faint small">{r.reason}</div>}</dd></>}
        {r.part && <><dt>Parça</dt><dd className="mono small">{r.part.join(" + ")}{r.plan_hash && <div className="faint" title="Plan özeti: onay bu özete bağlıdır">plan {r.plan_hash.slice(0, 12)}</div>}</dd></>}
        {r.cost && <><dt>Tahmini maliyet</dt><dd className="mono">~{usdTr(r.cost.reserve_usd)}</dd></>}
        {(r.resolution || r.duration != null || r.duration_s != null) && <><dt>Video</dt><dd className="mono">{[r.resolution, r.duration_s != null ? `${sec1(r.duration_s)} sn` : r.duration != null ? `${r.duration} sn` : "", r.audio ? "sesli" : "sessiz"].filter(Boolean).join(" · ")}</dd></>}
        <PlanFinishing plan={r} />
        <dt>Aşamalar</dt><dd className="small">{stagesText(r.stages)}</dd>
      </dl>
      {(r.references?.length ?? 0) > 0 && (
        <ol className="lines">
          {r.references!.map((x) => <li key={`${x.token}-${x.key}`}><span className="mono">{x.token}</span> → {x.name}{x.description ? <span className="faint small"> · {x.description}</span> : null}</li>)}
        </ol>
      )}
      {(r.uploads?.length ?? 0) > 0 && (
        <ul className="lines">
          {r.uploads!.map((u) => (
            <li key={u.field}><span className="mono small">{u.field}</span> ← <span className="mono small faint" style={{ overflowWrap: "anywhere" }}>{u.key}</span>
              {u.sha256 && <div className="mono small faint" title={u.sha256}>sha256 {u.sha256.slice(0, 16)}…</div>}</li>
          ))}
        </ul>
      )}
      {r.body && (
        <details>
          <summary className="small">İstek gövdesi (JSON)</summary>
          <pre className="prompt" style={{ maxHeight: 320, overflow: "auto", margin: "6px 0 0" }}>{JSON.stringify(r.body, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}

/** Referans modu: klibin video kapıları (0,5 sn'de bir örnek kare: kimlik, ölçek, yüz sayısı) ve parçadaki yeri. */
function Gates({ v }: { v: Variation }) {
  if (!v.gates) return null;
  const seg = segmentText(v);
  return (
    <ul aria-label="Video kapıları" style={{ margin: "2px 0 0", padding: 0, listStyle: "none" }}>
      {seg && <li className="faint small mono">{seg}</li>}
      {gateLines(v.gates).map((x) => <li key={x.t} className={`small ${x.bad ? "warn" : "faint"}`}>{x.t}</li>)}
    </ul>
  );
}

/** Yönetmen seçimi: kapıdan kalan ama bugünkü kareden üretilmiş aday da kullanılabilir (isteğe bağlı aynı seed ile tam tur). */
function Override({ sc, v }: { sc: Scene; v: Variation }) {
  const { busy, frozen, act, note, setVar } = usePanel();
  const pick = (full: boolean) => act(async () => {
    const r = await api.post<PromoteResult>(`/scenes/${sc.id}/promote/${v.id}?override=true${full ? "&full=true" : ""}`);
    setVar(null);
    const x = promoteText(r, full ? `Aynı seed (${v.seed}) ile tam tur klibi üretilecek ve 1. sıraya girecek. ` : "Kapının kararı geçersiz kılındı: aday 1. sırada. ");
    note("inspector", x.text, x.kind);
  }, "inspector");
  const why = v.anomalies.map((a) => ANOMALY_TR[a] || a).join(", ");
  return (
    <Section title="Seçili aday" meta={`seed ${v.seed}`}>
      <p className="small">{v.status === "purged" ? `Kalite kapısından kaldı${why ? ` (${why})` : ""}.` : "Rafine havuzda değil."} Gözle iyiyse yönetmen seçimiyle kullanılabilir; kapının ölçümleri kayıtta kalır.</p>
      <Gates v={v} />
      <div className="row">
        <button className="btn good" type="button" disabled={busy || frozen} title={frozen ? FROZEN_TITLE : undefined} onClick={() => pick(false)}>Yine de kullan</button>
        {v.tier === "probe" && <button className="btn" type="button" disabled={busy || frozen} title="Test turu klibi düşük çözünürlüklü: aynı seed ile tam tur çözünürlüğünde (720p) yeniden üretilir" onClick={() => pick(true)}>Aynı seed ile 720p üret</button>}
      </div>
    </Section>
  );
}

function Pool({ sc }: { sc: Scene }) {
  const { busy, frozen, act, note, setVar, compareRaw, openCompare } = usePanel();
  const [upFor, setUpFor] = useState<string | null>(null);
  const pool = sc.variations.filter((v) => v.rank).sort((a, b) => (a.rank ?? 0) - (b.rank ?? 0));
  const picked = parseIds(compareRaw, sc);
  const n = candidates(sc).length;
  const promote = (v: Variation) => act(async () => {
    const r = await api.post<PromoteResult>(`/scenes/${sc.id}/promote/${v.id}`);
    setVar(null);
    const x = promoteText(r);
    note("inspector", x.text, x.kind);
  }, "inspector");
  return (
    <Section title="Rafine havuz" meta={`${pool.length} aday`}>
      {pool.length ? (
        <ul className="lines">
          {pool.map((v) => {
            const badge = resolutionBadge(v);
            return (
            <li key={v.id} style={{ display: "grid", gridTemplateColumns: "56px 1fr auto", gap: 8, alignItems: "center" }}>
              {v.thumb_url ? <img src={v.thumb_url} alt="" loading="lazy" style={{ width: 56, height: 32, objectFit: "cover", borderRadius: 2 }} /> : <span />}
              <div style={{ minWidth: 0 }}><b className="mono">#{v.rank}</b> <span className="dim mono">skor {v.score != null ? v.score.toFixed(3) : "—"}</span>
                {v.stale && <small className="warn"> · eski kareden ({v.stale}); kurguda kullanılmaz</small>}
                {v.arrival && <small className="dim mono" title={`Son kare ${v.arrival.target === "end" ? "çekimin son karesine" : "sonraki çekimin karesine"} ne kadar vardı (DINOv2 ${v.arrival.dino.toFixed(2)}${v.arrival.identity != null ? `, kimlik ${v.arrival.identity.toFixed(2)}` : ""})`}> · varış {v.arrival.score.toFixed(2)}</small>}
                {!v.stale && v.review?.length ? <small className="warn"> · inceleme önerilir: {v.review.join("; ")}</small> : null}
                {badge ? <> <span className="tag info" title={badge.title}>{badge.long}</span></> : v.resolution ? <small className="faint mono"> · {v.resolution}</small> : null}
                <Gates v={v} /></div>
              <div className="stack" style={{ gap: 4, justifyItems: "end" }}>
                {v.rank === 1 ? <span className="tag ok">Kullanımda</span>
                  : <button type="button" className="btn" disabled={busy} onClick={() => promote(v)}>Bunu kullan</button>}
                {needsUpscale(v) && v.video_url && (
                  <button type="button" className="btn ghost" aria-expanded={upFor === v.id} disabled={busy || frozen}
                    title={frozen ? FROZEN_TITLE : "Bu klibi 1080p'ye çıkar: uç nokta, tahmin ve kalan bütçe gösterilir; orijinal varyasyon olarak kalır"}
                    onClick={() => setUpFor(upFor === v.id ? null : v.id)}>Upscale (1080p)</button>
                )}
              </div>
              {upFor === v.id && <div style={{ gridColumn: "1 / -1" }}><UpscaleConfirm scope="variation" id={v.id} onClose={() => setUpFor(null)} /></div>}
            </li>
            );
          })}
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

/** Ses-önce: sekansın ses kilidi, çekim süresinin kaynağı ("sesten") ve (yönetici) video planındaki ses referansı ile
    dudak senkronunun durumu. Ses referansı istek gövdesine girer; yalnızca planda ve kuru çalıştırmada görünür. */
function VoiceStatus({ sc, q }: { sc: Scene; q: Sequence | undefined }) {
  const { busy, frozen, act, note, admin, ov, setPage } = usePanel();
  const plan = useFalPlan(sc);
  const refMode = useRefMode(sc);
  const on = Boolean(sc.video_opts?.audio_ref);
  const ref = plan?.audio_ref;
  const lip = ov?.project.lipsync;
  return (
    <div className="stack" style={{ gap: 6 }}>
      <div className="row">
        {q?.voice_locked_at ? <span className="tag ok" title={`Kilit: ${clock(q.voice_locked_at)}`}><Icon.lock />Ses kilitli</span>
          : <span className="tag" title="Ses sayfasından kilitlenir">Ses kilitli değil</span>}
        <span className="mono small">{Number(sc.duration_s).toFixed(1).replace(".", ",")} sn</span>
        {sc.duration_source === "voice" && <span className="tag info" title="Süre sekansın ses kilidinde ölçülen sesten hesaplandı">sesten</span>}
        <span className="grow" />
        <button type="button" className="btn ghost" onClick={() => setPage("voice")}>Ses sayfasında aç</button>
      </div>
      {admin && (
        <dl className="kv">
          <dt>Ses referansı</dt>
          <dd>
            <select value={on ? "on" : "off"} disabled={busy || frozen || !refMode} aria-label="Ses referansı"
              title={refMode ? "Diyalog karışımı video isteğine ses referansı olarak eklenir (yalnızca plan ve kuru çalıştırmada görünür)" : "Yalnızca referans modunda (fal.ai, tüm sahne)"}
              onChange={(e) => void act(async () => {
                await api.patch(`/scenes/${sc.id}`, { video_audio_ref: e.target.value });
                note("inspector", `${sc.scene_code}: ses referansı ${e.target.value === "on" ? "açık" : "kapalı"}. Plan değişti: yeniden inceleyip onaylayın.`, "ok");
              }, "inspector")}>
              <option value="off">Kapalı</option><option value="on">Açık</option>
            </select>
            {ref && <div className="faint small">
              <span className="mono">{ref.field}</span> · {ref.lines} replik · {String(ref.duration_s).replace(".", ",")} sn{ref.key ? "" : " · gönderilmez"}
              {ref.warnings.map((w) => <div key={w} className="warn">{w}</div>)}
            </div>}
            {!refMode && <div className="faint small">Yalnızca referans modunda (tüm sahne tek istek).</div>}
          </dd>
          <dt>Dudak senkronu</dt>
          <dd>{lip?.text ?? "—"}{sc.lipsync_ready && <span className="faint small"> · dudak senkronlu video hazır</span>}</dd>
        </dl>
      )}
    </div>
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
      <VoiceStatus sc={sc} q={q} />
      <h4 className="small dim">Diyalog</h4>
      <ul className="lines">
        {lines.length ? lines.map((t) => (
          <li key={t.id}><span className="who">{t.character || "Anlatıcı"}</span> · {t.text}{" "}
            <span className={`tag ${t.source === "upload" ? "ok" : "warn"}`}>{t.source === "upload" ? "Final" : "Geçici"}</span>{" "}
            <span className="faint mono small">({EMOTION_TR[t.emotion ?? ""] || "—"}, {Number(t.start_offset_s).toFixed(1)} sn{t.duration_s ? ` · ${Number(t.duration_s).toFixed(1)} sn ses` : ""})</span></li>
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
  const { setPage, ov } = usePanel();
  const g = sc.grade;
  const neutral = g.exposure === 0 && g.contrast === 1 && g.saturation === 1 && g.gamma === 1 && g.temperature === 6500;
  const twos = Boolean(ov?.project.settings.on_twos);
  return (
    <Section title="Renk" meta={`${neutral ? "nötr" : "ayarlı"}${twos ? " · on twos" : ""}`} open={false}>
      <p className="dim small mono">pozlama {g.exposure} · kontrast {g.contrast} · doygunluk {g.saturation} · gama {g.gamma} · {g.temperature} K</p>
      {twos && <p className="faint small">Kare tutma açık: her çizim iki kare (12 çizim/sn). Etkisi Renk sayfasının Kareler görünümünde.</p>}
      <div className="row"><button className="btn" type="button" onClick={() => setPage("color")}>Renk sayfasında aç</button></div>
    </Section>
  );
}

type InspTab = "shot" | "sound" | "gen";
const TAB_TR: Record<InspTab, string> = { shot: "Çekim", sound: "Ses", gen: "Üretim" };

/** Denetçi: seçili çekimin bölümleri üç sekmede. Çekim (durum, varlıklar, adaylar), Ses (replikler, sekans), Üretim
    (motor ve plan, yöntem; yönetici modunda yükseltme ve elle düzeltme de). */
/** Kazanan parça klibinin bu çekime düşen kesimi: gerçek segment ile planlı segment, sınır kaynakları ve bölme uyarısı.
    Planlı segment fal-plan'dan (denetçiyle aynı SWR anahtarı; ayrıca istek yok), yoksa yalnızca gerçek segment. */
export function ClipSplit({ sc }: { sc: Scene }) {
  const plan = useFalPlan(sc);
  const win = sc.variations.find((v) => v.rank === 1);
  const split = sc.split ?? plan?.split ?? null;
  if (!split || !win?.segment) return null;
  const planned = plan?.split?.segment_planned
    ?? (() => { const x = plan?.timeline?.find((y) => y.code === sc.scene_code); return x ? [x.start, x.end] as [number, number] : null; })();
  return (
    <Section title="Kazanan klibin kesimi" meta={`${win.segment[0].toFixed(1)}–${win.segment[1].toFixed(1)} sn`} open={false}>
      <SplitView split={split} codes={win.part ?? plan?.part} code={sc.scene_code} segment={win.segment} planned={planned} />
    </Section>
  );
}


export function Inspector({ className = "", onClose }: { className?: string; onClose?: () => void }) {
  const { scene: sc, sequence: q, varId, meta, admin } = usePanel();
  const [tab, setTab] = useState<InspTab>("shot");
  const closeBtn = onClose && <button type="button" className="btn ghost icon-btn" aria-label="Denetçiyi kapat" title="Denetçiyi kapat" onClick={onClose}><Icon.chevron /></button>;
  if (!sc) return <section className={`pnl ${className}`}><header className="pnl-h"><h2>Denetçi</h2><span className="grow" />{closeBtn}</header><p className="empty">Çekim seçin.</p></section>;
  const chosen = varId ? sc.variations.find((v) => v.id === varId) : undefined;
  const revTarget = chosen ?? sc.variations.find((v) => v.rank === 1);
  const fal = isFal(sc.engine);
  const badge: Partial<Record<InspTab, { text: string; tone: string }>> = {};
  if (sc.status === "blocked") badge.shot = { text: "engelli", tone: "bad" };
  if (sc.status === "plan_review") badge.gen = { text: "onay", tone: "warn" };
  else if (sc.method_hint) badge.gen = { text: "öneri", tone: "info" };
  return (
    <section className={`pnl ${className}`} aria-label="Denetçi" data-tour="inspector">
      <header className="pnl-h"><h2 className="mono">{sc.scene_code}</h2><span className="meta">Denetçi</span><span className="grow" />{closeBtn}</header>
      <div className="pnl-tabs" role="tablist" aria-label="Denetçi bölümleri">
        {(Object.keys(TAB_TR) as InspTab[]).map((t) => (
          <button key={t} type="button" role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
            {TAB_TR[t]}{badge[t] && <span className={`tag ${badge[t]!.tone}`}>{badge[t]!.text}</span>}
          </button>
        ))}
      </div>
      <div className="pnl-b">
        {tab === "shot" && (
          <>
            <Shot sc={sc} />
            <Assets key={`a${sc.id}`} sc={sc} />
            {chosen?.overridable && <Override key={`o${chosen.id}`} sc={sc} v={chosen} />}
            <Pool key={`p${sc.id}`} sc={sc} />
            <ColorLink sc={sc} />
          </>
        )}
        {tab === "sound" && (
          <>
            <Sound key={`s${sc.id}`} sc={sc} q={q} />
            {q && <SequenceBox q={q} />}
          </>
        )}
        {tab === "gen" && (
          <>
            {fal && <FalVideo key={`f${sc.id}`} sc={sc} />}
            {(sc.engine ?? "h3") === "h3" && <H3 key={`h${sc.id}`} sc={sc} />}
            {fal && meta?.video_input_mode !== "reference" && <H3 key={`hf${sc.id}`} sc={sc} fal />}
            <Method key={`m${sc.id}`} sc={sc} />
            {admin && (
              <>
                <ClipSplit key={`cs${sc.id}`} sc={sc} />
                <Finishing key={`fin${sc.id}`} sc={sc} />
                <Revisions key={revTarget?.id ?? sc.id} target={revTarget} />
              </>
            )}
          </>
        )}
        <div style={{ padding: 10 }}><NoteBox id="inspector" /></div>
      </div>
    </section>
  );
}
