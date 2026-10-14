/* Dizi hiyerarşisinin saf yardımcıları (Dizi › Sezon › Bölüm): kırıntı etiketleri, teslim rozeti, varlık kapsamı ve
   yeni sürüme geçiş özeti. Tarayıcı gerektirmez; vitest ile sınanır. */
import { usd } from "./text";
import type { Asset, UpgradeImpact } from "./types";

/** "Bölüm 3 · Pilot"; ad zaten "Bölüm 3" ise tekrarlanmaz, numara yoksa yalnızca ad. */
export function episodeLabel(no: number | null | undefined, title: string): string {
  if (!no) return title;
  const head = `Bölüm ${no}`;
  return !title || title.trim() === head ? head : `${head} · ${title}`;
}

/** Teslim rozeti: "Sürüm sabit · v004" (panelden onaylanan Kabul'de sürüm olmayabilir: "Sürüm sabit"). */
export const lockLabel = (label: string | null | undefined): string => (label ? `Sürüm sabit · ${label}` : "Sürüm sabit");

export type AssetScope = "" | "series" | "local";
export const SCOPE_TR: Record<AssetScope, string> = { "": "Tümü", series: "Dizi kütüphanesi", local: "Bu bölüm" };

/** Varlık kapsam süzgecine uyuyor mu: dizi kütüphanesi (series_id dolu) ya da bölüme özel. */
export function inScope(a: Pick<Asset, "series_id">, scope: AssetScope): boolean {
  if (scope === "series") return Boolean(a.series_id);
  if (scope === "local") return !a.series_id;
  return true;
}

/** Yeni sürüme geçişin etkisi, panel notu olarak. */
export function upgradeText(x: UpgradeImpact): string {
  const head = `${x.name}: bölüm v${x.pinned ?? x.current} sürümünde, kütüphanede v${x.current}.`;
  if (!x.affected.length) return `${head} Hiçbir çekim yeniden üretilmez.`;
  const range = x.worst_usd > x.expected_usd ? ` (en kötü ${usd(x.worst_usd)})` : "";
  return `${head} Geçilirse ${x.affected.length} çekim yeniden üretilir: ${x.affected.join(", ")}. Tahmini sanal maliyet ${usd(x.expected_usd)}${range}.`;
}
