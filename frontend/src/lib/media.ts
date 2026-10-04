/* Bir çekim için gösterilecek video ve küçük resim. */
import type { Scene } from "./types";

export interface Media { src: string; poster: string | null; label: string; preview: boolean }

/** İzleyicide oynatılacak: seçili varyasyon (karantinadakiler dahil) > sonuç videosu > rafine #1 (montaj bekliyor). */
export function pickMedia(sc: Scene, varId: string | null): Media | null {
  const best = sc.variations.find((v) => v.rank === 1);
  const chosen = varId ? sc.variations.find((v) => v.id === varId) : undefined;
  if (chosen?.video_url) {
    const quarantine = chosen.video_url.includes("quarantine");
    return { src: chosen.video_url, poster: chosen.thumb_url, preview: true,
             label: `Varyasyon önizlemesi${quarantine ? " (karantinada)" : ""}${chosen.score != null ? " · skor " + chosen.score.toFixed(3) : ""}` };
  }
  if (sc.final_url) return { src: sc.final_url, poster: best?.thumb_url ?? null, preview: false, label: "Sonuç videosu" };
  if (best?.video_url) return { src: best.video_url, poster: best.thumb_url, preview: false, label: "Rafine video (montaj bekliyor)" };
  return null;
}

/** Çekimi temsil eden küçük resim: rafine #1 > storyboard karesi > son üretilen kare. */
export function posterOf(sc: Scene): string | null {
  const best = sc.variations.find((v) => v.rank === 1 && v.thumb_url);
  if (best) return best.thumb_url;
  if (sc.board_url) return sc.board_url;
  const any = [...sc.variations].reverse().find((v) => v.thumb_url && v.status !== "purged");
  return any?.thumb_url ?? null;
}
