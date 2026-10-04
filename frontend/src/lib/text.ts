/* Panelde gösterilen metinleri üreten saf fonksiyonlar (DOM yok; tests/text.test.ts ile sınanır). */
import { ANOMALY_TR, ALIGN_TR, ASTATUS_TR, BOARD_TR } from "./i18n";
import type { Blocked, DispatchAll, Measure, PanelEvent, StageResult, StyleImpact, Variation } from "./types";

export const usd = (n: number | null | undefined) => "$" + Number(n || 0).toFixed(2);
export const num2 = (x: number) => x.toFixed(2).replace(".", ",");
export const clock = (ts: number) => new Date(ts * 1000).toLocaleTimeString("tr-TR", { hour12: false });
export const pct = (x: number | null | undefined) => (x == null ? "—" : `%${Math.round(x * 100)}`);
export const ratio = (x: number | null | undefined, unit = "") => (x == null ? "—" : `${String(x).replace(".", ",")}${unit}`);
export const lowerTr = (s: string) => s.toLocaleLowerCase("tr");

/** Bir eylemden sonra hangi aşamaların çalışacağını yönetmenin dilinde söyler. */
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

export const blockedText = (b: Blocked) =>
  `Engellendi: ${b.missing.map((m) => m.name).join(", ")} için onaylı referans yok. Varlıklar sekmesinden kilitle, sonra yeniden üret.`;

export const resultText = (r: StageResult) => (r.blocked ? blockedText(r.blocked) : stagesText(r.stages));

export function measureText(m: Measure | null): string {
  if (!m) return "";
  const parts = Object.entries(m.alignment).map(([k, x]) => `${ALIGN_TR[k] || k} ${num2(x)}`);
  parts.push(...Object.entries(m.identity).map(([who, x]) => `${who} yüzü ${num2(x)}`));
  if (!Object.keys(m.identity).length && m.faces_seen) parts.push(`${m.faces_seen} yüz görüldü`);
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
    case "board_ready": return { t: `${x.code}: storyboard karesi hazır` };
    case "board_status": return {
      t: `${x.code}: kare ${lowerTr(BOARD_TR[x.status] || x.status)}`,
      c: x.status === "approved" ? "good" : x.status === "changes_requested" ? "bad" : "",
    };
    case "board_required": return { t: `${where}kare onaylanmadan görüntü üretilmez` };
    case "storyboard_queued": return { t: `${x.queued} storyboard karesi hazırlanıyor` };
    case "draft_pass": return { t: `Taslak geçiş: ${x.shots} çekim yalnızca test turuyla` };
    case "upgrade_pass": return { t: `İyileştirme geçişi: ${x.shots} çekim tam tura alındı` };
    case "continuity": return {
      t: x.jumps ? `Süreklilik: ${x.jumps} renk sıçraması (${x.pairs} geçiş)` : `Süreklilik: sıçrama yok (${x.pairs} geçiş)`,
      c: x.jumps ? "bad" : "good",
    };
    case "grade_changed": return { t: `${x.code}: renk ayarı değişti` };
    case "lut_changed": return { t: x.removed ? "Film LUT'u kaldırıldı" : `Film LUT'u yüklendi (${x.name})` };
    case "style_activated": return { t: `Stil kılavuzu sürüm ${x.version} etkin (${x.affected} çekim etkilendi)`, c: "good" };
    case "asset_status": return { t: `${x.name}: ${ASTATUS_TR[x.status] || x.status}`, c: x.status === "locked" ? "good" : "" };
    default: return { t: e.kind };
  }
}

/** Taslak ve iyileştirme geçişlerinin sonucu. */
export function passText(r: DispatchAll, kind: string): string {
  const started = Object.entries(r.planned).filter(([, st]) => st.includes("VISUAL")).map(([code]) => code);
  const parts = [started.length ? `${kind}: ${started.join(", ")}.` : `${kind} için üretilecek çekim yok.`];
  if (r.awaiting_board?.length) parts.push(`Kare onayı bekleyen: ${r.awaiting_board.join(", ")}.`);
  if (r.blocked && Object.keys(r.blocked).length) parts.push(`Eksik referans: ${Object.keys(r.blocked).join(", ")}.`);
  if (r.capped?.length) parts.push(`Sekans tavanında: ${r.capped.join(", ")}.`);
  return parts.join(" ");
}

/** "Engellenen çekimleri yeniden dene" ve stil etkinleştirmenin ortak sonucu. */
export function dispatchText(r: DispatchAll): { text: string; bad: boolean } {
  const started = Object.entries(r.planned).filter(([, st]) => st.length).map(([code]) => code);
  const still = Object.keys(r.blocked);
  const text = (started.length ? `Üretime alındı: ${started.join(", ")}.` : "Üretime alınan çekim yok.")
    + (still.length ? ` Hâlâ engelli: ${still.join(", ")}.` : "")
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

export const deltaText = (d: Record<string, number>) =>
  ([["brightness", "parlaklık"], ["warmth", "sıcaklık"], ["saturation", "doygunluk"]] as const)
    .map(([k, label]) => `${label} ${d[k] > 0 ? "+" : ""}${Math.round(d[k])}`).join(", ");

export const paletteOf = (raw: string) => raw.split(",").map((x) => x.trim()).filter(Boolean);
export const isHex = (c: string) => /^#[0-9a-f]{6}$/i.test(c);
