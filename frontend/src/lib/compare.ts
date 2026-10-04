/* Varyasyon karşılaştırma: seçim, sıralama ve ölçüm farkları. DOM'dan bağımsızdır (tests/compare.test.ts). */
import type { Scene, Variation } from "./types";

export const MAX_COMPARE = 4;

/** Videosu izlenebilen varyasyon (karantinadakiler dahil; üretilemeyen ya da hâlâ üretilen hariç). */
export const playable = (v: Variation) => Boolean(v.video_url) && !["queued", "running", "failed"].includes(v.status);

/** Karşılaştırmaya alınabilecek adaylar: önce rafine havuz (sırasıyla), sonra skora göre azalan. */
export function candidates(scene: Scene): Variation[] {
  return scene.variations.filter(playable).sort((a, b) => {
    if (a.rank && b.rank) return a.rank - b.rank;
    if (a.rank) return -1;
    if (b.rank) return 1;
    return (b.score ?? -1) - (a.score ?? -1);
  });
}

/** Açılışta karşılaştırılacaklar: rafine havuz; havuz ikiden azsa en yüksek skorlu adaylarla tamamlanır. */
export function defaultSelection(scene: Scene, max = MAX_COMPARE): string[] {
  const all = candidates(scene);
  const pool = all.filter((v) => v.rank);
  const picked = pool.length >= 2 ? pool : all.slice(0, Math.max(2, pool.length));
  return picked.slice(0, max).map((v) => v.id);
}

/** Seçime ekler ya da çıkarır; doluysa en eski seçim düşer. */
export function toggle(ids: string[], id: string, max = MAX_COMPARE): string[] {
  if (ids.includes(id)) return ids.filter((x) => x !== id);
  const next = [...ids, id];
  return next.length > max ? next.slice(next.length - max) : next;
}

/** URL'deki "a,b,c" değerini sahnenin geçerli varyasyonlarına süzer (silinmiş ya da başka sahneye ait olanlar düşer). */
export function parseIds(raw: string | null, scene: Scene | undefined): string[] {
  if (!raw || !scene) return [];
  const known = new Set(scene.variations.filter(playable).map((v) => v.id));
  return [...new Set(raw.split(",").filter((id) => known.has(id)))].slice(0, MAX_COMPARE);
}

export interface Row { key: string; label: string; values: (number | null)[]; best: number | null }

/** Ölçüm tablosu: birleşik skor, stil/tarif uyumu ve karakter yüz benzerlikleri; her satırda en iyi değer işaretlenir. */
export function measureRows(vs: Variation[], alignTr: Record<string, string>): Row[] {
  const rows: Row[] = [{ key: "score", label: "Birleşik skor", values: vs.map((v) => v.score), best: null }];
  const alignKeys = [...new Set(vs.flatMap((v) => Object.keys(v.measure?.alignment ?? {})))];
  for (const k of alignKeys) {
    rows.push({ key: "a:" + k, label: `${capitalize(alignTr[k] || k)} uyumu`, values: vs.map((v) => v.measure?.alignment[k] ?? null), best: null });
  }
  const faces = [...new Set(vs.flatMap((v) => Object.keys(v.measure?.identity ?? {})))].sort((a, b) => a.localeCompare(b, "tr"));
  for (const who of faces) {
    rows.push({ key: "f:" + who, label: `${who} yüzü`, values: vs.map((v) => v.measure?.identity[who] ?? null), best: null });
  }
  for (const r of rows) r.best = bestIndex(r.values);
  return rows;
}

/** En yüksek değerin sırası; değer yoksa ya da herkes eşitse null (vurgulanacak bir kazanan yok). */
export function bestIndex(values: (number | null)[]): number | null {
  const present = values.map((x, i) => [x, i] as const).filter(([x]) => x != null) as [number, number][];
  if (present.length < 2) return null;
  const top = Math.max(...present.map(([x]) => x));
  const winners = present.filter(([x]) => x === top);
  return winners.length === 1 ? winners[0][1] : null;
}

/** Önde gidene göre fark: "+0,012" ya da "−0,034" (kendisi için boş). */
export function deltaFrom(value: number | null, lead: number | null): string {
  if (value == null || lead == null || value === lead) return "";
  const d = value - lead;
  return (d > 0 ? "+" : "−") + Math.abs(d).toFixed(3).replace(".", ",");
}

/** Takipçi oynatıcının lidere göre kayması bu eşiği aşarsa yeniden hizalanır (saniye; 24 fps'te ~2 kare). */
export const DRIFT_S = 0.08;
export function needsResync(leader: number, follower: number, duration: number, threshold = DRIFT_S): boolean {
  const target = Math.min(leader, Math.max(0, duration - 0.001));     // kısa klip sonunda bekler; zorla sarılmaz
  return Math.abs(target - follower) > threshold;
}

export const FRAME_S = 1 / 24;

const capitalize = (s: string) => s.charAt(0).toLocaleUpperCase("tr") + s.slice(1);
