/* Model karışımı ve parça bölme (referans modu, whole_scene): bir parçanın çekimleri aynı üretimden gelmeli. Karma üretim
   seçenekleri, motor değişiminin kapsamı ve klibin çekimlere bölünmesinin metinleri. Saf fonksiyonlar
   (tests/mixing.test.ts). */
import { isFal } from "./engines";
import { dispatchedText, resultText, stagesText, usdTr } from "./text";
import type { NoteKind } from "./panel";
import type { AdoptResult, Generation, Meta, MixOption, PartSplit, PromoteResult, SceneEngineResult, ShotEngineResult } from "./types";

export const ADOPT_LABEL = "Bütün sahneyi bu motorla değiştir";
export const SHOT_LABEL = "Bu çekimi ayrı üret";
export const SHOT_ONLY_LABEL = "Yalnızca bu çekim (ayrı üretim, son kareyle bağlanır)";
export const BOUNDARY_NOTE = "Model değişimi varsayılan olarak yalnızca sahne sınırında önerilir.";

/** Saniye, iki ondalık: "3,40". */
export const sec2 = (x: number) => Number(x || 0).toFixed(2).replace(".", ",");

/** Sınırın kaynağı: klipte bulunan kesme ya da (bulunamadıysa) planlı sınır. */
export const SOURCE_TR: Record<string, string> = { cut: "klipteki kesme", planned: "planlı sınır" };
const SOURCE_SHORT: Record<string, string> = { cut: "klipten", planned: "planlı sınırdan" };

/** Motor değişimi bütün sahneye mi uygulanır: referans modu + whole_scene ve iki motor da fal.ai (H3 Max, Seedance 2.5).
    Değilse eski davranış (yalnızca çekimin motoru yazılır). */
export const wholeSceneSwitch = (meta: Pick<Meta, "video_input_mode" | "scene_render_mode"> | undefined, from: string | null | undefined, to: string) =>
  meta?.video_input_mode === "reference" && (meta.scene_render_mode ?? "whole_scene") === "whole_scene" && isFal(from) && isFal(to) && from !== to;

export type MixAction =
  | { action: "adopt"; label: string; variationId: string }
  | { action: "shot"; label: string; engine: string };

const query = (path: string) => new URLSearchParams(path.split("?")[1] ?? "");

/** Karma üretimin düğmeleri: arka ucun seçenekleri (promote yanıtı; yoldaki variation_id ve engine okunur) ya da yoksa
    sahnenin generation alanından iki varsayılan seçenek. Tanınmayan eylem atlanır. */
export function mixActions(g: Generation | null | undefined, options?: MixOption[] | null): MixAction[] {
  const out: MixAction[] = [];
  for (const o of options ?? []) {
    const q = query(o.path);
    if (o.action === "adopt" && (q.get("variation_id") || g?.variation_id))
      out.push({ action: "adopt", label: o.label || ADOPT_LABEL, variationId: q.get("variation_id") || g!.variation_id });
    else if (o.action === "shot" && (q.get("engine") || g?.engine))
      out.push({ action: "shot", label: o.label || SHOT_LABEL, engine: q.get("engine") || g!.engine });
  }
  if (out.length || !g) return out;
  return [{ action: "adopt", label: ADOPT_LABEL, variationId: g.variation_id }, { action: "shot", label: SHOT_LABEL, engine: g.engine }];
}

/** "Bunu kullan" / "Yine de kullan" sonucu: aşamalar ve (karma üretimse) uyarı ile uyumsuz çekimler. */
export function promoteText(r: PromoteResult, prefix = ""): { text: string; kind: NoteKind } {
  const base = `${prefix}${resultText(r)}`;
  if (r.blocked) return { text: base, kind: "err" };
  if (!r.warning) return { text: base, kind: "ok" };
  const codes = r.mixed?.mismatched?.length ? ` Farklı üretimden: ${r.mixed.mismatched.join(", ")}.` : "";
  return { text: `${base} ${r.warning}${codes}`, kind: "" };
}

/** "Bütün sahneyi bu motorla değiştir" sonucu: yeniden üretim ve ücret yok. */
export const adoptText = (r: AdoptResult, engineName: string) =>
  `${r.part.join(", ")} artık tek üretimden (${engineName}); yeniden üretim yok, ücret yok. ${stagesText(r.stages)}`;

/** Bütün sahnenin motoru değişti (onaylı yeniden üretim). */
export const sceneEngineText = (r: SceneEngineResult, engineName: string) =>
  `Bütün sahne ${engineName} ile yeniden üretiliyor: ${r.shots.join(", ")} (tahmini ~${usdTr(r.total_usd)}). ${dispatchedText(r.dispatched) || "Üretilecek aşama yok."}`.trim();

/** "Bu çekimi ayrı üret" sonucu: çekim önceki çekimin son karesiyle başlar, sonraki çekim yeniden bağlanır. */
export function shotEngineText(r: ShotEngineResult, engineName: string, code: string): string {
  const others = r.relinked.filter((c) => c !== code);
  return [`${code} ${engineName} ile ayrı üretiliyor; önceki çekimin son karesiyle başlar.`,
    others.length ? `${others.join(", ")} bu çekimin son karesiyle yeniden bağlanıp yeniden üretiliyor.` : "",
    dispatchedText(r.dispatched)].filter(Boolean).join(" ");
}

export interface Boundary { from: string | null; to: string | null; at: number; planned: number | null; source: string | null; text: string }

/** Çekimin kesmeleri (gerçek segment ile planlı segment): giriş (çekim parçanın ilki değilse) ve çıkış (sonuncusu
    değilse). codes: parçanın çekimleri; code: bu çekim. "Kesme S01 → S02: 3,40 sn (klipten) · planlı 3,00 sn". */
export function shotBoundaries(split: Pick<PartSplit, "sources"> | null | undefined, segment: [number, number] | null | undefined,
  planned: [number, number] | null | undefined, codes: string[] | null | undefined, code: string): Boundary[] {
  if (!segment) return [];
  const list = codes?.length ? codes : [code];
  const i = list.indexOf(code);
  if (i < 0 || list.length < 2) return [];
  const make = (k: number, at: number, p: number | null | undefined): Boundary => {
    const source = split?.sources?.[k] ?? null;
    const from = list[k] ?? null, to = list[k + 1] ?? null;
    const how = source ? ` (${SOURCE_SHORT[source] ?? source})` : "";
    return { from, to, at, planned: p ?? null, source,
      text: `Kesme ${from} → ${to}: ${sec2(at)} sn${how}${p != null ? ` · planlı ${sec2(p)} sn` : ""}` };
  };
  const out: Boundary[] = [];
  if (i > 0) out.push(make(i - 1, segment[0], planned?.[0]));
  if (i < list.length - 1) out.push(make(i, segment[1], planned?.[1]));
  return out;
}

/** Parçanın bütün iç sınırlarının kaynağı: "S01 → S02: klipteki kesme". */
export const boundarySources = (split: Pick<PartSplit, "sources"> | null | undefined, codes: string[] | null | undefined) =>
  (split?.sources ?? []).map((s, k) => `${codes?.[k] ?? `${k + 1}.`} → ${codes?.[k + 1] ?? `${k + 2}.`}: ${SOURCE_TR[s] ?? s}`);

/** Klipte bulunan kesmeler: "Klipte 2 kesme bulundu (3,40 · 7,10 sn)". */
export const cutsText = (split: Pick<PartSplit, "cuts"> | null | undefined) =>
  !split ? "" : split.cuts.length ? `Klipte ${split.cuts.length} kesme bulundu (${split.cuts.map(sec2).join(" · ")} sn)` : "Klipte kesme bulunamadı";
