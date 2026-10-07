/* Panelde gösterilen metinleri üreten saf fonksiyonlar (DOM yok; tests/text.test.ts ile sınanır). */
import { ANOMALY_TR, ALIGN_TR, ASTATUS_TR, BOARD_TR } from "./i18n";
import type { Blocked, DispatchAll, Measure, PanelEvent, StageResult, StyleImpact, Variation } from "./types";

export const usd = (n: number | null | undefined) => "$" + Number(n || 0).toFixed(2);
/** Kuruşun altını da gösteren tutar (tek çağrılar 0,035 USD gibi küçüktür): en az 2, en çok 4 ondalık. */
export const usdExact = (n: number | null | undefined) => "$" + Number(n || 0).toFixed(4).replace(/(\.\d\d\d*?)0+$/, "$1");
export const num2 = (x: number) => x.toFixed(2).replace(".", ",");
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
    case "board_rejected": return { t: `${x.code}: storyboard karesi kalite kapısını geçemedi${x.score != null ? " (" + Number(x.score).toFixed(2) + ")" : ""}; video bu kareden üretilmez`, c: "bad" };
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
    case "runpod_balance_low": return { t: `RunPod bakiyesi düşük (${Number(x.balance).toFixed(2)} $ < ${Number(x.floor).toFixed(2)} $): yeni iş başlatılmadı`, c: "bad" };
    case "runpod_config_applied": return { t: `RunPod uç nokta ayarları uygulandı (${(x.datacenters as string[] | undefined)?.join(", ") ?? "?"})`, c: "good" };
    case "batched": return { t: `Toplu üretim: önce ${x.boards} storyboard karesi, sonra ${x.videos} çekimin videosu` };
    case "batch_videos": return { t: `Kareler bitti: ${x.videos} çekimin videosu kuyruğa alındı` };
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
