/* Teslim: şartname hazır ayarları ve özeti, QC sorunlarının Kurgu zaman çizelgesindeki yeri (saf fonksiyonlar,
   tests/delivery.test.ts). */
import { filmToTimeline, type ShotClip } from "./timeline";
import type { CutClip, DeliverySpec, DeliveryState, DeliverySummary } from "./types";

export const CODEC_TR: Record<string, string> = { h264: "H.264", prores_hq: "ProRes 422 HQ" };
export const FPS_TR: Record<string, string> = { "23.976": "23,976", "24": "24", "25": "25" };

/** Hazır ayarı forma uygular (etiket hariç); preset alanı seçilen hazır ayar olur. */
export function applyPreset(state: Pick<DeliveryState, "presets">, id: string, base: DeliverySpec): DeliverySpec {
  const { label: _label, ...values } = state.presets[id];
  return { ...base, ...values, preset: id } as DeliverySpec;
}

/** "1920x1080 · 25 fps · ProRes 422 HQ · -23 LUFS · SRT, M&E, yazısız". */
export function specLine(s: DeliverySpec): string {
  const extra = [s.srt && "SRT", s.me_mix && "M&E", s.textless && "yazısız"].filter(Boolean).join(", ");
  return `${s.resolution} · ${FPS_TR[s.fps] ?? s.fps} fps · ${CODEC_TR[s.codec] ?? s.codec} · ${String(s.loudness_lufs).replace(".", ",")} LUFS${extra ? ` · ${extra}` : ""}`;
}

/** Hazır ayarla birebir aynı mı (form elle değiştirildiyse hazır ayar etiketi kalkar). */
export function matchesPreset(state: Pick<DeliveryState, "presets">, s: DeliverySpec): string | null {
  for (const [id, p] of Object.entries(state.presets)) {
    if ((["resolution", "fps", "codec", "loudness_lufs", "srt", "me_mix", "textless"] as const).every((k) => p[k] === s[k])) return id;
  }
  return null;
}

/** QC sorunlarının Kurgu zaman çizelgesindeki yeri: filmdeki zaman -> çekim (final filmin kesme listesiyle). */
export function qcMarks(sum: DeliverySummary | undefined, cut: CutClip[], shots: ShotClip[]): { t: number; name: string; tc: string; detail: string; key: string }[] {
  if (!sum?.qc || sum.qc.stale) return [];
  return sum.qc.issues.flatMap((x, i) => {
    const t = filmToTimeline(x.start, cut, shots);
    return t == null ? [] : [{ t, name: x.name, tc: x.tc, detail: x.detail, key: `${x.check}-${i}` }];
  });
}
