/* Panelde gösterilen metinleri üreten saf fonksiyonlar (DOM yok; tests/text.test.ts ile sınanır). */
import { ANOMALY_TR, ALIGN_TR, ASTATUS_TR, BOARD_TR } from "./i18n";
import type { Blocked, DispatchAll, FalPlan, Measure, PanelEvent, PlanApproval, StageResult, StripApproval, StyleImpact, UpgradeFinal, Variation, VideoGates } from "./types";

export const usd = (n: number | null | undefined) => "$" + Number(n || 0).toFixed(2);
/** Kuruşun altını da gösteren tutar (tek çağrılar 0,035 USD gibi küçüktür): en az 2, en çok 4 ondalık. */
export const usdExact = (n: number | null | undefined) => "$" + Number(n || 0).toFixed(4).replace(/(\.\d\d\d*?)0+$/, "$1");
/** Türkçe ondalıklı tahmin: "0,042 $", "1,25 $" (1 doların altında 3 ondalık). */
export const usdTr = (n: number | null | undefined) =>
  n == null || !Number.isFinite(n) ? "—" : `${n.toFixed(Math.abs(n) >= 1 ? 2 : 3).replace(".", ",")} $`;
export const num2 = (x: number) => x.toFixed(2).replace(".", ",");
/** Saniye: "4,5" (tek ondalık). */
export const sec1 = (x: number) => Number(x || 0).toFixed(1).replace(".", ",");
export const clock = (ts: number) => new Date(ts * 1000).toLocaleTimeString("tr-TR", { hour12: false });
export const pct = (x: number | null | undefined) => (x == null ? "—" : `%${Math.round(x * 100)}`);
export const ratio = (x: number | null | undefined, unit = "") => (x == null ? "—" : `${String(x).replace(".", ",")}${unit}`);
export const lowerTr = (s: string) => s.toLocaleLowerCase("tr");
/** Süre: "42 sn", "27 dk 05 sn", "1 sa 03 dk". */
export function durText(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} sn`;
  if (s < 3600) return `${Math.floor(s / 60)} dk ${String(s % 60).padStart(2, "0")} sn`;
  return `${Math.floor(s / 3600)} sa ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")} dk`;
}
/** Bayt: "512 KB", "38,4 MB", "1,2 GB". */
export function bytesText(n: number | null | undefined): string {
  const b = Number(n || 0);
  if (b < 1024 ** 2) return `${Math.round(b / 1024)} KB`;
  if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(1).replace(".", ",")} MB`;
  return `${(b / 1024 ** 3).toFixed(1).replace(".", ",")} GB`;
}
/** Kiralama işinin aşama süreleri: her aşamadan bir sonrakine (son aşama şimdiye ya da bitişe) geçen süre. */
export function stepDurations(steps: Record<string, number> | undefined, now: number, end?: string[]): [string, number][] {
  const order = Object.entries(steps ?? {}).sort((a, b) => a[1] - b[1]);
  return order.map(([name, at], i) => {
    const next = order[i + 1]?.[1] ?? ((end ?? ["ready", "failed"]).includes(name) ? at : now);
    return [name, next - at] as [string, number];
  });
}

/** Bir eylemden sonra hangi aşamaların çalışacağını yönetmenin dilinde söyler. */
const BATCH_GROUP_TR: Record<string, string> = {
  board_qwen: "Kareler (Qwen-Image)", board_edit: "Karakterli kareler (Qwen-Image-Edit)", fl2va: "Kareler bitti, FL2VA videoları",
  ref2va: "Ref2VA videoları",
};

/** Kare denemesinin sebebi ve ölçümleri: " (yüz görünmedi)", " (kimlik Mila 0,47)", " (benzerlik Bolt 0,62)". */
const CUT_METHOD_TR: Record<string, string> = {
  cut_in: "kırp + iyileştir", angles: "açı LoRA'sı", wide: "geniş plandan", anchor: "metinle kamera tarifi", compose: "sıfırdan kurulum",
};
const attemptText = (x: Record<string, unknown>) => {
  const reasons = (x.reasons as string[] | undefined) ?? (x.faces_seen === 0 ? ["no_face"] : []);
  const parts: string[] = [];
  const method = CUT_METHOD_TR[String(x.cut_method ?? "")];
  if (method) parts.push(method);
  const cam = x.camera_change as { cut_in?: boolean; zoom?: number } | undefined;
  if (cam?.cut_in) parts.push(`yakınlaşma ${num2(Number(cam.zoom ?? 0))}×`);
  if (reasons.includes("no_face")) parts.push("yüz görünmedi");
  const id = scores(x.identity), look = scores(x.look);
  if (id) parts.push(`kimlik ${id}`);
  if (look) parts.push(`benzerlik ${look}`);
  if (x.faces_seen && x.faces_measured === 0) parts.push("yüz küçük, kimlik ölçülemedi");
  const dino = scores(x.look_dino);
  if (dino) parts.push(`DINOv2 ${dino}`);
  const state = scores(x.state);
  if (state) parts.push(`durum ${state}`);
  if ((x.review as string[] | undefined)?.length) parts.push(`inceleme önerilir`);
  if (reasons.includes("low_clip")) parts.push("istem uyumu düşük");
  if (reasons.includes("continuity_jump")) parts.push(`süreklilik ${num2(Number(x.continuity ?? 0))}: önceki kareyle mekân farklı`);
  else if (x.chain && x.continuity != null) parts.push(`süreklilik ${num2(Number(x.continuity))}`);
  if (reasons.some((r) => ["black", "freeze", "flat"].includes(r))) parts.push("kusurlu kare");
  return parts.length ? ` (${parts.join("; ")})` : "";
};

/** Karakter başına ölçüm: "Elif 0,47, Kerem 0,81". */
const scores = (values: unknown) =>
  Object.entries((values ?? {}) as Record<string, number>).map(([n, v]) => `${n} ${num2(Number(v))}`).join(", ");

const STAGE_TR: Record<string, string> = {
  STORYBOARD: "storyboard karesi", LIPSYNC: "dudak senkronu", GRADE: "renk", MUX: "montaj", ASSEMBLE: "film birleştirme",
};
const joinTr = (xs: string[]) => (xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} ve ${xs[xs.length - 1]}`);

export function stagesText(stages: string[]): string {
  if (!stages.length) return "Yapılacak iş yok: girdiler değişmedi.";
  const unknown = stages.filter((s) => s !== "VISUAL" && !STAGE_TR[s]);
  if (unknown.length) return `Çalışacak aşamalar: ${stages.join(", ")}`;
  if (stages.includes("VISUAL")) {
    const after = stages.filter((s) => s !== "VISUAL" && s !== "STORYBOARD")
      .map((s) => (s === "LIPSYNC" ? "ses" : STAGE_TR[s]));
    const board = stages.includes("STORYBOARD") ? "Storyboard karesi yeniden çizilecek, görüntü" : "Görüntü";
    return `${board} baştan üretilecek${after.length ? `, ardından ${joinTr(after)}` : ""}.`;
  }
  return `Yalnızca ${joinTr(stages.map((s) => STAGE_TR[s]))}; görüntü motoru çağrılmadı.`;
}

export const blockedText = (b: Blocked) => b.missing.some((m) => m.status === "no_image")
  ? `Engellendi: ${b.missing.map((m) => m.name).join(", ")} için referans görseli yok. Varlıklar sayfasından görsel yükleyin; görseli olmayan varlıkla video üretilmez.`
  : `Engellendi: ${b.missing.map((m) => m.name).join(", ")} için onaylı referans yok. Varlıklar sekmesinden kilitle, sonra yeniden üret.`;

/** Referans modu: planı onay bekleyen çekimler (render, render-all, senaryo). */
export const awaitingPlanText = (codes: string[], usd?: number) =>
  `Plan onayı bekliyor: ${codes.join(", ")}${usd != null ? ` (tahmini ~${usdTr(usd)})` : ""}. Denetçideki fal.ai video bölümünden planı inceleyip onaylayın.`;

export const resultText = (r: StageResult) => (r.blocked ? blockedText(r.blocked)
  : r.awaiting_plan ? awaitingPlanText(r.plan?.part ?? [], r.plan?.usd) : stagesText(r.stages));

/** Çekim başına sonuçların özeti (plan onayı ve yükseltme): üretime alınan, işlendiği için atlanan, engellenen. */
export function dispatchedText(d: Record<string, StageResult & { busy?: string }>): string {
  const rows = Object.entries(d);
  const started = rows.filter(([, r]) => r.stages.length && !r.blocked && !r.awaiting_plan).map(([c]) => c);
  const busy = rows.filter(([, r]) => r.busy).map(([c]) => c);
  const blocked = rows.filter(([, r]) => r.blocked).map(([c]) => c);
  const waiting = rows.filter(([, r]) => r.awaiting_plan).map(([c]) => c);
  return [started.length ? `Üretime alındı: ${started.join(", ")}.` : "",
    busy.length ? `Şu an işlendiği için atlandı: ${busy.join(", ")}.` : "",
    blocked.length ? `Eksik referans: ${blocked.join(", ")}.` : "",
    waiting.length ? `Plan onayı bekliyor: ${waiting.join(", ")}.` : ""].filter(Boolean).join(" ");
}

/** "Planı onayla" / "Onayla ve üret" sonucu. */
export function approveText(r: PlanApproval, render: boolean): string {
  const head = `Plan onaylandı: ${r.part.join(" + ")} tek istekte (tahmini ~${usdTr(r.total_usd)}).`;
  if (!render) return `${head} Üretim için "Onayla ve üret" ya da "Bu sahneyi baştan üret".`;
  return `${head} ${dispatchedText(r.dispatched) || "Üretilecek aşama yok: girdiler değişmedi."}`;
}

/** "Seedance 2.5 ile yükselt" onayının sonucu. */
export function upgradeText(r: UpgradeFinal, engineName: string, code: string): string {
  const shots = r.shots?.length ? r.shots : [code];
  const head = `${shots.join(", ")} ${engineName} ile yeniden üretiliyor${r.total_usd != null ? ` (tahmini ~${usdTr(r.total_usd)})` : ""}; taslak klipler varyasyon olarak kaldı (Bunu kullan ile dönülebilir).`;
  if (r.dispatched) return `${head} ${dispatchedText(r.dispatched)}`.trim();
  return `${head} ${r.awaiting_board ? "Kare onayı bekleniyor." : resultText(r)}`;
}

/** Planın uyarıları; eksik ve düşen referanslar ile süreklilik notu ayrı gösterildiği için tekrarlanmaz. */
export function planWarnings(p: Pick<FalPlan, "warnings" | "missing" | "dropped" | "continuity">): string[] {
  return (p.warnings ?? []).filter((w) => w !== p.continuity?.note
    && !((p.missing?.length ?? 0) > 0 && w.startsWith("Eksik referans"))
    && !((p.dropped?.length ?? 0) > 0 && w.startsWith("Referans sınırı")));
}

/** Klibin video kapıları, satır satır: örnekleme, kimlik, ölçek, yüz sayısı ve sonuç. bad: kapıyı düşüren satır. */
export function gateLines(g: VideoGates): { t: string; bad?: boolean }[] {
  const out: { t: string; bad?: boolean }[] = [];
  out.push({ t: g.scorer === "dry" || g.frames == null
    ? "Örnek kare yok: ücretsiz modun sahte skoru (kimlik ve ölçek ölçülmedi)"
    : `${g.frames} örnek kare, ${sec1(g.step)} sn'de bir` });
  const id = scores(g.identity?.identity), look = scores(g.identity?.look);
  if (id) out.push({ t: `Kimlik: ${id}`, bad: g.anomalies.includes("low_identity") });
  if (look) out.push({ t: `Benzerlik: ${look}`, bad: g.anomalies.includes("low_likeness") });
  if (g.identity?.faces_seen === 0) out.push({ t: "Örnek karelerde yüz görünmedi", bad: g.anomalies.includes("no_face") });
  for (const [name, s] of Object.entries(g.scale ?? {})) {
    out.push(s.ok
      ? { t: `Ölçek ${name}: tutuyor (tepe ${num2(s.peak)}, sınır ${num2(s.max)}; ${s.measured} karede ölçüldü)` }
      : { t: `Ölçek ${name}: tutmuyor, ${s.over.map(sec1).join(", ")} sn'de büyük (tepe ${num2(s.peak)} > ${num2(s.max)})`, bad: true });
  }
  if (g.face_counts?.length) out.push({ t: `Yüz sayısı: en çok ${Math.max(...g.face_counts)}`, bad: g.anomalies.includes("duplicate_character") });
  out.push(g.passed ? { t: "Video kapılarından geçti" }
    : { t: `Video kapılarında kaldı${g.anomalies.length ? `: ${g.anomalies.map((a) => ANOMALY_TR[a] || a).join(", ")}` : ""}`, bad: true });
  return out;
}

/** Klibin parçadaki yeri: "S001_SH01 + S001_SH02 parçasından 4,0–8,5 sn". */
export const segmentText = (v: Pick<Variation, "part" | "segment">) =>
  v.part?.length && v.segment ? `${v.part.join(" + ")} parçasından ${sec1(v.segment[0])}–${sec1(v.segment[1])} sn` : "";

export function measureText(m: Measure | null): string {
  if (!m) return "";
  const parts = Object.entries(m.alignment).map(([k, x]) => `${ALIGN_TR[k] || k} ${num2(x)}`);
  parts.push(...Object.entries(m.identity).map(([who, x]) => `${who} yüzü ${num2(x)}`));
  if (m.faces_seen === 0) parts.push("yüz görünmedi");
  else if (m.faces_seen != null) parts.push(`${m.faces_seen} yüz görüldü`);
  return " (" + parts.join(", ") + ")";
}

export type TileState = "refined" | "rejected" | "retired" | "failed" | "working" | "passed";

/** Film şeridindeki bir karenin durumu ve erişilebilir açıklaması. */
export function tileSpec(v: Variation, prev?: Variation) {
  const working = ["queued", "running", "generated"].includes(v.status);
  let state: TileState, fate: string;
  if (v.rank) { state = "refined"; fate = `rafine havuzda, ${v.rank}. sıra`; }
  else if (v.status === "purged" && v.reason === "quality_gate") {
    state = "rejected";
    fate = (v.stage === "first_frame" ? "ilk karede elendi, video üretilmedi: " : "kalite kontrolünde elendi: ")
      + (v.anomalies.map((a) => ANOMALY_TR[a] || a).join(", ") || "eşik altı");
  } else if (v.status === "purged") { state = "retired"; fate = v.reason === "superseded" ? "yeni üretimle yerini kaybetti" : "havuza giremedi"; }
  else if (v.status === "failed") { state = "failed"; fate = "üretilemedi"; }
  else if (working) { state = "working"; fate = "üretiliyor"; }
  else { state = "passed"; fate = "kalite kontrolünü geçti"; }
  if (v.revision) fate += `, elle düzeltilmiş (revizyon ${v.revision})`;
  if (v.source !== "generate") fate += v.source === "external" ? ", dış çekim" : ", previs rehberinden";
  const score = (v.score != null ? ` · skor ${v.score.toFixed(3)}` : "") + measureText(v.measure);
  return {
    state, fate,
    tierStart: v.tier === "full" && (!prev || prev.tier !== "full"),
    label: `${v.tier === "probe" ? "Test turu" : "Tam tur"} ${v.wave_no + 1}. dalga${score} · ${fate}`,
  };
}

export const variationTitle = (v: Variation) => `${v.tier === "probe" ? "Test turu" : "Tam tur"} · ${v.wave_no + 1}. dalga`;

/** Olay akışındaki bir kaydın metni ve tonu (good / bad). */
export function eventText(e: PanelEvent, codes: Record<string, string>): { t: string; c?: "good" | "bad" | "" } {
  const x = e as PanelEvent & Record<string, any>;     // olay alanları türüne göre değişir
  const where = x.scene_id && codes[x.scene_id] ? codes[x.scene_id] + ": " : "";
  const p = (n: number) => Math.round((n || 0) * 100);
  switch (e.kind) {
    case "probe_wave": return { t: `${where}test turu ${x.round + 1}: ${x.n} klip, ortalama skor ${Number(x.mean_composite).toFixed(3)}, geçme oranı %${p(x.pass_rate)}` };
    case "full_wave": return { t: `${where}tam tur ${x.wave}. dalga: ${x.n} klip, ${x.excellent} mükemmel aday` };
    case "refined": return { t: `${where}rafine havuz hazır (${x.pool} klip)`, c: "good" };
    case "soft_limit": return { t: "Yumuşak bütçe sınırına ulaşıldı" };
    case "soft_limit_stop": return { t: `${where}yumuşak limit: yeni dalga başlatılmadı` };
    case "frozen": return { t: `Üretim donduruldu${x.reason ? ": " + x.reason : ""}`, c: "bad" };
    case "resumed": return { t: "Üretime devam edildi", c: "good" };
    case "timeline": return { t: `${x.cut === "draft" ? "Kaba kurgu" : "Film"} birleştirildi: ${x.scenes} sahne`, c: "good" };
    case "timeline_failed": return { t: `Film birleştirilemedi: ${x.error}`, c: "bad" };
    case "blocked": return { t: `${where}engellendi, eksik referans: ${(x.missing || []).join(", ")}`, c: "bad" };
    case "lint_warning": return { t: `${where}uyarı, onaysız referans: ${(x.missing || []).join(", ")}` };
    case "motion_test": return { t: `Hareket testi ${x.ok ? "temiz" : "kusurlu (" + (x.anomalies || []).join(", ") + ")"}`, c: x.ok ? "good" : "bad" };
    case "sequence_capped": return { t: `${x.code} sekansı bütçe tavanına ulaştı (${usd(x.spent)} / ${usd(x.cap)}); diğer sekanslar sürüyor`, c: "bad" };
    case "sequence_reopened": return { t: `${x.code} sekansı yeniden açıldı`, c: "good" };
    case "track_changed": return { t: `Film kanalı ${x.removed ? "kaldırıldı" : "değişti"}; hiçbir çekim yeniden üretilmedi, film yeniden birleştiriliyor` };
    case "board_ready": return { t: `${x.code}: storyboard karesi hazır${x.chain ? ` (${x.chain} karesinden devam)` : ""}` };
    case "board_status": return {
      t: `${x.code}: kare ${lowerTr(BOARD_TR[x.status] || x.status)}`,
      c: x.status === "approved" ? "good" : x.status === "changes_requested" ? "bad" : "",
    };
    case "board_required": return { t: `${where}kare onaylanmadan görüntü üretilmez` };
    case "board_rejected": return x.note
      ? { t: `${x.code}: ${x.note}; video üretilmedi`, c: "bad" }
      : { t: `${x.code}: storyboard karesi kalite kapısını geçemedi${x.score != null ? " (" + Number(x.score).toFixed(2) + ")" : ""}; video bu kareden üretilmez`, c: "bad" };
    case "keyframe_required": return { t: `${where}H3 ilk karesi hazır değil: görüntü üretilmedi`, c: "bad" };
    case "h3_started": return { t: "vast.ai makinesi başlatıldı (otomatik)", c: "good" };
    case "h3_stopped": return { t: `vast.ai makinesi durduruldu (${x.reason === "idle" ? "boşta" : x.reason === "budget" ? "bütçe" : x.reason})` };
    case "h3_replacement_proposed": return x.initial
      ? { t: `Yeni makine önerisi onay bekliyor (${x.gpu}, ${Number(x.dph).toFixed(2)} $/sa, ${x.geolocation})` }
      : { t: `Makine açılamadı; yedek makine önerisi onay bekliyor (${x.gpu}, ${Number(x.dph).toFixed(2)} $/sa, ${x.geolocation})`, c: "bad" };
    case "h3_replacing": return { t: `Yeni makine kiralanıyor (${x.gpu}, ${Number(x.dph).toFixed(2)} $/sa, ${x.geolocation})` };
    case "h3_replaced": return { t: `Yeni makine hazır (${x.new_instance_id})${x.old_instance_id ? "" : "; kurulum bitti"}`, c: "good" };
    case "h3_replace_failed": return { t: `Yeni makine kurulamadı: ${x.error}`, c: "bad" };
    case "h3_outputs_retained": return { t: `Eski makine ${x.instance_id} silinmedi: ${x.missing} çıktı yerel depoda yok`, c: "bad" };
    case "h3_outputs_retrieved": return { t: `Makineden ${x.stored} çıktı indirildi ve doğrulandı${x.remaining ? `; ${x.remaining} eksik` : ""}`, c: x.remaining ? "bad" : "good" };
    case "h3_outputs_abandoned": return { t: `Eski makine ${x.instance_id} silindi; ${x.lost} çıktının kaybı kabul edildi`, c: "bad" };
    case "h3_tls_pinned": return { t: `H3 sunucusunun sertifikası sabitlendi (${String(x.fingerprint ?? "").slice(0, 23)}…)`, c: "good" };
    case "h3_tls_mismatch": return { t: `H3 sunucusunun sertifikası değişti (${x.endpoint}): istek gönderilmedi`, c: "bad" };
    case "h3_tls_fallback": return { t: `Sertifika sabitlenemedi (${x.reason}); doğrulamasız sürüyor`, c: "bad" };
    case "runpod_cold_start": return { t: `Model yükleniyor (soğuk başlatma): ${x.model}, ${Number(x.seconds).toFixed(0)} sn` };
    case "runpod_model_swap": return { t: `Model değişti: ${x.previous} → ${x.model} (iş ${Number(x.seconds).toFixed(0)} sn; yeniden yükleme dahil)` };
    case "thawed_for_scene": return { t: "Tek çekim için donma geçici kaldırıldı; iş bitince proje yeniden donacak" };
    case "runpod_balance_low": return { t: `RunPod bakiyesi düşük (${Number(x.balance).toFixed(2)} $ < ${Number(x.floor).toFixed(2)} $): yeni iş başlatılmadı`, c: "bad" };
    case "runpod_config_applied": return { t: `RunPod uç nokta ayarları uygulandı (${(x.datacenters as string[] | undefined)?.join(", ") ?? "?"})`, c: "good" };
    case "director_override": return { t: `${x.code ?? ""}${x.code ? ": " : ""}yönetmen seçimi${x.full ? " (aynı seed ile tam tur)" : ""}: aday 1. sırada; kapının kararı geçersiz kılındı`, c: "good" };
    case "cut_failed": return { t: `${x.code}: kesme yöntemi (${CUT_METHOD_TR[String(x.method ?? x.cut_method)] ?? x.cut_method}) uygulanamadı: ${x.error}; bu deneme metinle kamera tarifiyle çizildi`, c: "bad" };
    case "chain_waiting": return { t: `${x.code}: zincirdeki önceki kare (${x.parent}) inceleme bekliyor; bu kare ondan çizilecek`, c: "bad" };
    case "refunded": return { t: `İade: ${x.count} "${x.label === "h3:idle" ? "GPU makinesi boşta" : x.label}" kaydı, ${usd(Number(x.usd))} (${x.reason})`, c: "good" };
    case "budget_changed": return { t: `Bütçe ${usd(Number(x.old))} → ${usd(Number(x.new))}` };
    case "chain_invalidated": {
      const frames = (x.frames as string[] | undefined) ?? [];
      const codes = x.codes as string[];
      const parts = [frames.length ? `${frames.join(", ")} kareleri eski (zincirle yeniden çizilecek)` : "",
        codes.length ? `${codes.join(", ")} eski kareden; yeniden üretilecek` : "", x.clips ? `${x.clips} klip eski` : ""].filter(Boolean);
      return { t: `${x.reason}: ${parts.join("; ") || "bağlı çekim yok"}`, c: "bad" };
    }
    case "state_reference": return { t: `${x.code}: ${x.character} için durum referansı (${x.state === "off" ? "kapalı" : "açık"}) ${x.source} karesinden alındı (sonda ${Number(x.probe).toFixed(2)})`, c: "good" };
    case "seam_outdated": return { t: `${x.code}: ${x.reason}; eski klip yeniden üretilecek`, c: "bad" };
    case "color_auto_started": return { t: "Otomatik renk eşleştirme başladı" };
    case "color_auto_matched": {
      const codes = (x.codes as string[] | undefined) ?? [];
      const skipped = ((x.skipped as { code: string; reason: string }[] | undefined) ?? []).map((s) => `${s.code}: ${s.reason}`);
      const refs = Object.values((x.references as Record<string, string> | undefined) ?? {});
      const head = codes.length ? `Renk otomatik eşleştirildi: ${codes.join(", ")} (GRADE + MUX, tek tur); bitince film bir kez birleştirilir`
        : "Otomatik renk eşleştirme: hiçbir çekim değişmedi";
      return { t: `${head}${refs.length ? `; referans ${refs.join(", ")}` : ""}${skipped.length ? `. Atlananlar: ${skipped.join("; ")}` : ""}`, ...(codes.length ? { c: "good" } : {}) };
    }
    case "color_auto_failed": return { t: `Otomatik renk eşleştirme uygulanamadı${x.code ? ` (${x.code})` : ""}: ${x.error || "sebep bildirilmedi"}`, c: "bad" };
    case "color_round_done": return { t: "Otomatik renk eşleme turu bitti" };
    case "auto_assemble_skipped": {
      const waiting = (x.waiting as string[] | undefined) ?? [];
      const more = Number(x.count ?? waiting.length) - waiting.length;
      return { t: `${x.cut === "draft" ? "Kaba kurgu" : "Film"} kendiliğinden birleştirilmedi${x.reason ? ` (${x.reason})` : ""}: bitmeyen çekimler var: ${waiting.join(", ")}${more > 0 ? ` ve ${more} çekim daha` : ""}. Elle birleştirebilirsiniz.` };
    }
    case "runpod_idle_changed": return { t: `RunPod boşta bekleme ${x.seconds} sn (storyboard şeridi incelemesi${Number(x.seconds) <= 120 ? " bitti" : ": art arda yeniden çizimler sıcak worker'a düşer"})` };
    case "runpod_idle_change_failed": return { t: `RunPod boşta bekleme süresi yazılamadı: ${x.error}`, c: "bad" };
    case "chain_stale_stop": return { t: `${where}üretim durdu: ${x.reason}; video eski kareden üretilmez`, c: "bad" };
    case "chain_reset": return { t: `Temiz başlangıç: ${x.clips} klip eski, ${x.stale_frames} zincir karesi yeniden çizilecek` };
    case "runpod_model_wait": return { t: `RunPod: ${x.model} işi sırasını bekliyor (önce çalışan model grubu bitsin)` };
    case "runpod_queue_full": return { t: `RunPod kuyruğu dolu: projede ${x.pending}/${x.limit} iş bekliyor; yenisi yer açılınca gönderilecek` };
    case "runpod_cancelled": return { t: `Kill-Switch: RunPod'daki ${x.jobs} bekleyen iş iptal edildi`, c: "good" };
    case "runpod_cancel_failed": return { t: `RunPod işleri iptal edilemedi: ${x.error}; RunPod konsolundan kuyruğu kontrol edin`, c: "bad" };
    case "task_redelivered": return { t: `${where}görev yarıda kesildi, yeniden çalışıyor (${x.delivery}. teslim; yeni iş gönderilmez${x.released ? `, ${x.released} rezervasyon iade edildi` : ""})`, c: "bad" };
    case "task_abandoned": return { t: `${where}görev defalarca yarıda kesildi; döngüyü önlemek için durduruldu`, c: "bad" };
    case "batched": return { t: `Toplu üretim: önce ${x.boards} storyboard karesi${x.edit_boards ? ` (${x.edit_boards} karakter referansından)` : ""}, sonra ${x.videos} çekimin videosu` };
    case "batch_videos": return { t: `${BATCH_GROUP_TR[String(x.group)] ?? "Sıradaki grup"}: ${x.videos} çekim kuyruğa alındı` };
    case "board_attempt": return { t: `${where}${x.end ? "son kare" : "ilk kare"} ${x.attempt}/${x.of}: ${x.passed ? "geçti" : "kaldı"}${attemptText(x)}`, c: x.passed ? "good" : "bad" };
    case "end_board_ready": return { t: `${x.code}: ${x.passed ? "hazır; video bu karede biter" : "kapıdan geçemedi; video son karesiz üretilir"}${x.note && x.passed ? ` (${x.note})` : ""}`, c: x.passed ? (x.note ? "" : "good") : "bad" };
    case "board_strip_approved": return { t: `Storyboard şeridi onaylandı: ${(x.approved as string[]).length} kare${(x.review as string[]).length ? `; inceleme bekleyen: ${(x.review as string[]).join(", ")}` : ""}`, c: (x.review as string[]).length ? "bad" : "good" };
    case "draft_completed": return { t: `${(x.part as string[] | undefined)?.join("+") ?? where}Seedance taslağı 1080p'ye tamamlandı (${Number(x.usd ?? 0).toFixed(2).replace(".", ",")} $)`, c: "good" };
    case "upscaled": return { t: `${where}klip 1080p'ye çıkarıldı (${String(x.endpoint ?? "").split("/").slice(-3).join("/")}, ${Number(x.usd ?? 0).toFixed(3).replace(".", ",")} $); orijinal varyasyon olarak kaldı`, c: "good" };
    case "color_rematch": return { t: `Klipleri değişen ${x.shots} çekim için otomatik renk eşleme yeniden çalışıyor (montajdan sonra)` };
    case "upscale_batch_done": return { t: "Bütün film upscale edildi: film yeniden birleştiriliyor (renk eşleme ardından)", c: "good" };
    case "part_clip": return { t: `${(x.shots as string[] | undefined)?.join("+") ?? ""} parça klibi (${x.engine === "seedance25" ? "Seedance 2.5" : "H3 Max"}, deneme ${x.attempt}): ${x.passed ? "video kapılarından geçti" : "video kapılarında kaldı"}`, c: x.passed ? "good" : "bad" };
    case "part_stopped": return { t: `${where}parça üretilmedi (${x.status === "plan_review" ? "plan onayı bekliyor" : x.status === "blocked" ? "eksik referans" : "inceleme"}): ${x.reason}`, c: "bad" };
    case "fal_clip": return { t: `${where}fal.ai klibi (${x.engine === "seedance25" ? "Seedance 2.5" : x.engine === "h3max" ? "H3 Max" : x.engine}, ${x.mode === "i2v" ? "image-to-video" : "reference-to-video"}, deneme ${x.attempt}): ${x.passed ? "kalite kapısından geçti" : "kalite kapısında kaldı"}`, c: x.passed ? "good" : "bad" };
    case "provider_rejected": return { t: `${where}fal.ai isteği reddetti (yeniden denenmedi): ${x.error}`, c: "bad" };
    case "engine_disabled": return { t: `${where}motor kapalı (MiniMax H3 · RunPod): H3 Max'e geçin`, c: "bad" };
    case "seam_waiting": return { t: `${where}kesintisiz dikiş: ${x.previous} bitmeden bu çekim üretilmez`, c: "bad" };
    case "storyboard_queued": return { t: `${x.queued} storyboard karesi hazırlanıyor${(x.requested as string[] | undefined)?.length ? ` (değişiklik istenen: ${(x.requested as string[]).join(", ")}; yönetmen notuyla)` : ""}` };
    case "draft_pass": return { t: `Taslak geçiş: ${x.shots} çekim yalnızca test turuyla` };
    case "upgrade_pass": return { t: `İyileştirme geçişi: ${x.shots} çekim tam tura alındı` };
    case "continuity": return {
      t: x.jumps ? `Süreklilik: ${x.jumps} renk sıçraması (${x.pairs} geçiş)` : `Süreklilik: sıçrama yok (${x.pairs} geçiş)`,
      c: x.jumps ? "bad" : "good",
    };
    case "grade_changed": return { t: `${x.code}: renk ayarı değişti` };
    case "lut_changed": return { t: x.removed ? "Film LUT'u kaldırıldı" : `Film LUT'u yüklendi (${x.name})` };
    case "style_activated": {
      const stale = (x.stale as string[] | undefined) ?? [];
      return { t: `Stil kılavuzu sürüm ${x.version} etkin (${x.affected} çekim etkilendi)${stale.length ? `; eski stil olarak işaretlendi, yeniden üretilmedi: ${stale.join(", ")}` : ""}`, c: "good" };
    }
    case "asset_status": return { t: `${x.name}: ${ASTATUS_TR[x.status] || x.status}`, c: x.status === "locked" ? "good" : "" };
    default: return { t: e.kind };
  }
}

/** Taslak ve iyileştirme geçişlerinin sonucu. */
export function passText(r: DispatchAll, kind: string): string {
  const started = Object.entries(r.planned).filter(([, st]) => st.includes("VISUAL")).map(([code]) => code);
  const parts = [started.length ? `${kind}: ${started.join(", ")}.` : `${kind} için üretilecek çekim yok.`];
  if (r.awaiting_board?.length) parts.push(`Kare onayı bekleyen: ${r.awaiting_board.join(", ")}.`);
  if (r.awaiting_plan?.length) parts.push(`Plan onayı bekliyor: ${r.awaiting_plan.join(", ")}.`);
  if (r.blocked && Object.keys(r.blocked).length) parts.push(`Eksik referans: ${Object.keys(r.blocked).join(", ")}.`);
  if (r.capped?.length) parts.push(`Sekans tavanında: ${r.capped.join(", ")}.`);
  return parts.join(" ");
}

/** Şerit onayı: onaylanan kareler, inceleme bekleyen ve karesi olmayan çekimler, başlayan videolar. */
export function stripText(r: StripApproval): { text: string; bad: boolean } {
  const parts = [r.approved.length ? `Onaylanan: ${r.approved.join(", ")}.` : "Yeni onaylanan kare yok."];
  if (r.review.length) parts.push(`İnceleme bekleyen (onaylanmadı): ${r.review.join(", ")}.`);
  if (r.missing.length) parts.push(`Karesi olmayan: ${r.missing.join(", ")}.`);
  if (r.frozen) parts.push("Proje donmuş: üretime devam edince videolar başlar.");
  else if (r.planned) parts.push(passText({ busy: [], blocked: {}, capped: [], ...r, planned: r.planned } as DispatchAll, "Video başlayan"));
  return { text: parts.join(" "), bad: r.review.length > 0 || r.missing.length > 0 };
}

/** "Engellenen çekimleri yeniden dene" ve stil etkinleştirmenin ortak sonucu. */
export function dispatchText(r: DispatchAll): { text: string; bad: boolean } {
  const started = Object.entries(r.planned).filter(([, st]) => st.length).map(([code]) => code);
  const still = Object.keys(r.blocked);
  const text = (started.length ? `Üretime alındı: ${started.join(", ")}.` : "Üretime alınan çekim yok.")
    + (still.length ? ` Hâlâ engelli: ${still.join(", ")}.` : "")
    + (r.awaiting_plan?.length ? ` Plan onayı bekliyor: ${r.awaiting_plan.join(", ")}.` : "")
    + (r.capped.length ? ` Sekans tavanında: ${r.capped.join(", ")}.` : "");
  return { text, bad: Boolean(still.length || r.capped.length) };
}

export function impactText(i: StyleImpact): string {
  const codes = i.affected.map((a) => a.scene_code);
  let t = codes.length
    ? `${codes.length} çekim yeniden üretilir: ${codes.join(", ")}. Tahmini sanal maliyet ${usd(i.expected_usd)} (en kötü durumda ${usd(i.worst_usd)}).`
    : "Üretilmiş hiçbir çekim yeniden üretilmez.";
  if (i.pending.length) t += ` Henüz üretilmemiş ${i.pending.length} çekim ilk üretiminde bu stili kullanır.`;
  return t;
}

/** Karakter dışı bölgenin LAB farkı: ΔL (parlaklık), Δb (sıcaklık), ΔE; doygunluk tüm kareden (bilgi). */
export const deltaText = (d: Record<string, number>) => {
  const sign = (v: number, digits = 1) => `${v > 0 ? "+" : ""}${v.toFixed(digits).replace(".", ",")}`;
  const parts = [`ΔL ${sign(d.brightness ?? 0)}`, `sıcaklık ${sign(d.warmth ?? 0)}`];
  if (d.E != null) parts.push(`ΔE ${(d.E).toFixed(1).replace(".", ",")}`);
  return parts.join(", ");
};

export const paletteOf = (raw: string) => raw.split(",").map((x) => x.trim()).filter(Boolean);
export const isHex = (c: string) => /^#[0-9a-f]{6}$/i.test(c);

/** Geçmişteki zaman: bugünse yalnızca saat, değilse gün ve saat (yerel saat). */
export function whenText(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  const time = d.toLocaleTimeString("tr-TR", { hour12: false });
  if (d.toDateString() === now.toDateString()) return time;
  return `${d.toLocaleDateString("tr-TR", { day: "numeric", month: "short", ...(d.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}) })} ${time}`;
}

/** Çağrının tutarı panelde nasıl okunur: işlenen, iade edilen ya da henüz ayrılmış. */
export function callAmountText(c: { status: string; amount_usd: number | null; estimate_usd: number }): { value: string; note: string } {
  if (c.status === "running") return { value: usdExact(c.estimate_usd), note: "ayrıldı" };
  if (c.status === "failed") return { value: usdExact(0), note: `${usdExact(c.estimate_usd)} iade edildi` };
  return { value: usdExact(c.amount_usd), note: c.amount_usd !== c.estimate_usd ? `tahmin ${usdExact(c.estimate_usd)}` : "" };
}

/** Sağlayıcı isteğinin parametre değeri, okunur biçimde. */
export function stepValue(key: string, value: unknown): string {
  if (typeof value === "boolean") return value ? (key === "reused" ? "evet: yeniden ödenmedi" : "evet") : "hayır";
  if (key === "loras" && Array.isArray(value)) return value.map((l) => `${(l as { path: string }).path} ×${(l as { scale?: number }).scale ?? 1}`).join(", ");
  if (value && typeof value === "object") return Object.entries(value as Record<string, unknown>).map(([k, v]) => `${k} ${v}`).join(", ");
  return String(value);
}
