/* Video motorları (fal.ai: H3 Max taslak, Seedance 2.5 final; RunPod'daki H3 varsayılan olarak kapalı): seçenek
   listeleri ve denetçideki değerler. Saf fonksiyonlar (tests/engines.test.ts). */
import { ENGINE_CHOICES, ENGINE_TR } from "./i18n";
import type { Scene, VideoMeta } from "./types";

/** Kuru çalıştırma gövdesi ve "yükselt" yalnızca bu motorlarda (backend fal_plan.FAL_ENGINES). */
export const FAL_ENGINES = ["h3max", "seedance25"] as const;
export const isFal = (engine: string | null | undefined) => (FAL_ENGINES as readonly string[]).includes(engine ?? "");

/** Ayarlar'da tanımlı değilse kullanılan çözünürlükler (fal_plan.H3_RESOLUTIONS, SEEDANCE_RESOLUTIONS). */
const RESOLUTIONS: Record<string, string[]> = { h3max: ["480P", "768P", "1080P"], seedance25: ["480p", "720p", "1080p"] };
const DEFAULT_RESOLUTION: Record<string, string> = { h3max: "768P", seedance25: "720p" };

export interface EngineChoice { id: string; label: string; disabled: boolean }

/** Seçicideki motorlar: /meta'daki liste (yoksa yerel liste); kapalı motor "(kapalı)" ekiyle ve seçilemez. Çekimin
    listede olmayan (eski) motoru sona eklenir ki seçici boş görünmesin. */
export function engineChoices(video: VideoMeta | undefined, current?: string | null): EngineChoice[] {
  const base = video?.engines?.length
    ? video.engines.map((e) => ({ id: e.id, label: e.label || ENGINE_TR[e.id] || e.id, disabled: Boolean(e.disabled) }))
    : ENGINE_CHOICES.map((id) => ({ id, label: ENGINE_TR[id] ?? id, disabled: false }));
  const out = base.map((e) => ({ ...e, label: e.disabled ? `${e.label} (kapalı)` : e.label }));
  if (current && !out.some((e) => e.id === current)) out.push({ id: current, label: ENGINE_TR[current] ?? current, disabled: false });
  return out;
}

export const resolutionsFor = (engine: string, video?: VideoMeta) =>
  (engine === "h3max" ? video?.h3max_resolutions : engine === "seedance25" ? video?.seedance25_resolutions : undefined)
  ?? RESOLUTIONS[engine] ?? [];

export const defaultResolution = (engine: string, video?: VideoMeta) =>
  (engine === "h3max" ? video?.h3max_resolution : engine === "seedance25" ? video?.seedance25_resolution : undefined)
  ?? DEFAULT_RESOLUTION[engine] ?? "";

/** Seçicilerin değeri: "auto" Ayarlar'daki varsayılandır (PATCH /scenes/{id} aynı değeri alır). */
export const modeValue = (sc: Pick<Scene, "h3_mode_explicit">) => sc.h3_mode_explicit ?? "auto";
export const resolutionValue = (sc: Pick<Scene, "video_opts">) => sc.video_opts?.resolution ?? "auto";
export const audioValue = (sc: Pick<Scene, "video_opts">) =>
  sc.video_opts?.audio == null ? "auto" : sc.video_opts.audio ? "true" : "false";

/** Çekim süresi girdisi: virgül ya da nokta; 2-15 sn dışı ya da sayı değilse null (H3 Max en çok 15 sn üretir). */
export function parseDuration(raw: string): number | null {
  const x = Number(raw.trim().replace(",", "."));
  if (!raw.trim() || !Number.isFinite(x) || x < 2 || x > 15) return null;
  return Math.round(x * 10) / 10;
}

/** "Seedance 2.5 ile yükselt": motorun kısa adı (etiketteki parantez atılır). */
export const engineShort = (engine: string, label?: string) => (label ?? ENGINE_TR[engine] ?? engine).replace(/\s*\(.*\)\s*$/, "").replace(/^MiniMax /, "");
