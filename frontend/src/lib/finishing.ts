/* Bitirme (Seedance taslağını 1080p'ye tamamlama ve upscale): denetçide, Kurgu sayfasında ve planda gösterilen metinler.
   Saf fonksiyonlar (DOM yok; tests/finishing.test.ts ile sınanır). */
import { UPSCALE_TR } from "./i18n";
import { usdTr } from "./text";
import type { CompleteResult, SeedanceDraft, UpscalePlan, Variation } from "./types";

/** Kalan süre: "kalan 6 gün 23 sa", "kalan 5 sa 12 dk", "kalan 12 dk"; bittiyse "süresi doldu". */
export function remainingText(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) return "süresi doldu";
  const s = Math.floor(seconds);
  const days = Math.floor(s / 86400), hours = Math.floor((s % 86400) / 3600), mins = Math.floor((s % 3600) / 60);
  if (days > 0) return `kalan ${days} gün${hours ? ` ${hours} sa` : ""}`;
  if (hours > 0) return `kalan ${hours} sa${mins ? ` ${mins} dk` : ""}`;
  return `kalan ${Math.max(1, mins)} dk`;
}

/** Taslağın durumu: tamamlandı, süresi doldu (kırmızı) ya da kalan süre. */
export function draftState(d: Pick<SeedanceDraft, "remaining_s" | "expired" | "completed_at">): { text: string; tone: "ok" | "bad" | "" } {
  if (d.completed_at) return { text: "tamamlandı", tone: "ok" };
  if (d.expired || d.remaining_s <= 0) return { text: "süresi doldu", tone: "bad" };
  return { text: remainingText(d.remaining_s), tone: "" };
}

/** Kısa taslak kimliği: ilk 8 karakter ("local-" öneki atılır). */
export const shortId = (id: string) => {
  const x = id.replace(/^local-/, "");
  return x.length > 10 ? `${x.slice(0, 8)}…` : x;
};

/** Upscale uç noktasının adı ("fal-ai/seedvr/upscale/video" -> "SeedVR2"). */
export const upscaleName = (endpoint: string) => UPSCALE_TR[endpoint] ?? endpoint.split("/").slice(-2).join("/");

/** En ucuz uç nokta ve toplamı (eşitlikte listedeki ilk). Boşsa null. */
export function cheapestUpscale(totals: Record<string, number> | undefined): { endpoint: string; usd: number } | null {
  let best: { endpoint: string; usd: number } | null = null;
  for (const [endpoint, usd] of Object.entries(totals ?? {})) {
    if (!Number.isFinite(usd)) continue;
    if (!best || usd < best.usd) best = { endpoint, usd };
  }
  return best;
}

/** Uç nokta seçicisinin seçenekleri: /meta'daki liste (yoksa toplamların anahtarları), her birinin toplamı ve varsayılanı. */
export function upscaleOptions(endpoints: string[] | undefined, totals: Record<string, number> | undefined, fallback?: string) {
  const ids = endpoints?.length ? endpoints : Object.keys(totals ?? {});
  return ids.map((id) => {
    const usd = totals?.[id];
    return { id, label: `${upscaleName(id)}${usd != null ? ` · ~${usdTr(usd)}` : ""}${id === fallback ? " (varsayılan)" : ""}` };
  });
}

/** Planın toplamı klip tahminlerinden (arka ucun total_usd'si yoksa; ayrılan tutar). */
export const upscaleTotal = (items: UpscalePlan["items"]) =>
  Math.round(items.reduce((a, x) => a + Number(x.cost.reserve_usd ?? x.cost.usd ?? 0), 0) * 10000) / 10000;

/** Onaylanan upscale'in sonucu. */
export function upscaleResultText(r: Pick<UpscalePlan, "scope" | "endpoint" | "items" | "total_usd"> & { queued?: number }): string {
  const codes = r.items.map((x) => x.scene_code);
  const head = `${r.queued ?? codes.length} klip ${upscaleName(r.endpoint)} ile 1080p'ye çıkarılıyor (tahmini ~${usdTr(r.total_usd)})`;
  const tail = r.scope === "project" ? "; bitince film yeniden birleştirilir, renk eşleme ardından yapılır."
    : "; orijinal klip varyasyon olarak kalır, renk eşleme upscale'den sonra yeniden yapılır.";
  return `${head}${codes.length ? `: ${[...new Set(codes)].join(", ")}` : ""}${tail}`;
}

/** "Taslağı 1080p'ye tamamla" sonucu. */
export const completeResultText = (r: CompleteResult) =>
  `${r.part.join(" + ")} Seedance taslağı 1080p'ye tamamlanıyor (tahmini ~${usdTr(r.total_usd)}, ayrılan ~${usdTr(r.reserve_usd)}); taslak kesimleri varyasyon olarak kalır.`;

/** İstem uzunluğu: "İstem: 1.234 karakter / 50.000"; sınır aşılırsa over. */
export function promptLengthText(chars: number | null | undefined, limit?: number | null): { text: string; over: boolean } | null {
  if (chars == null) return null;
  const n = (x: number) => x.toLocaleString("tr-TR");
  return { text: `İstem: ${n(chars)} karakter${limit ? ` / ${n(limit)}` : ""}`, over: Boolean(limit && chars > limit) };
}

/** Klibin çözünürlük rozeti: 1080p (upscale edilmiş, taslaktan tamamlanmış ya da 1080p üretilmiş); değilse null. */
export function resolutionBadge(v: Pick<Variation, "resolution" | "upscaled_from" | "completed_from">): { short: string; long: string; title: string } | null {
  if (v.upscaled_from) return { short: "1080p", long: "1080p · upscale", title: "Upscale edilmiş klip; orijinali varyasyon olarak duruyor" };
  if (v.completed_from) return { short: "1080p", long: "1080p · tamamlandı", title: "Seedance taslağından 1080p'ye tamamlandı; taslak kesimi varyasyon olarak duruyor" };
  if ((v.resolution ?? "").toLowerCase() === "1080p") return { short: "1080p", long: "1080p", title: "1080p üretildi" };
  return null;
}

/** Upscale gerekir mi: 1080p olmayan klip. */
export const needsUpscale = (v: Pick<Variation, "resolution" | "upscaled_from" | "completed_from">) => !resolutionBadge(v);
