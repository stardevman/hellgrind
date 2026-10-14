/* 2D yayın güvenliği görünümleri: güvenli alanlar (action safe %93, title safe %90), karakter boy sıralaması (line-up)
   ve renk betiği yardımcıları (saf fonksiyonlar, tests/safe.test.ts). */

/** Yayın güvenli alanları: çerçevenin ortasında, genişlik ve yüksekliğin bu oranı. */
export const SAFE = { action: 0.93, title: 0.9 } as const;

/** Kapsayıcıya "contain" ile sığdırılmış görüntünün dikdörtgeni (letterbox/pillarbox dahil). */
export function fitRect(cw: number, ch: number, aspect: number): { left: number; top: number; width: number; height: number } {
  if (!(cw > 0 && ch > 0 && aspect > 0)) return { left: 0, top: 0, width: 0, height: 0 };
  const width = Math.min(cw, ch * aspect), height = width / aspect;
  return { left: (cw - width) / 2, top: (ch - height) / 2, width, height };
}

/** Güvenli alanın kenar payı (yüzde): %93 -> her kenardan %3,5. */
export const insetPct = (ratio: number) => `${((1 - ratio) * 50).toFixed(2)}%`;

/* ---------- boy sıralaması ---------- */

const UNITS: [RegExp, number][] = [
  [/(\d+(?:[.,]\d+)?)\s*(?:m|metre|meter|meters|metres)\b/i, 1],
  [/(\d+(?:[.,]\d+)?)\s*(?:cm|santim|santimetre|centimet(?:er|re)s?)\b/i, 0.01],
  [/(\d+(?:[.,]\d+)?)\s*(?:mm|milimetre|millimet(?:er|re)s?)\b/i, 0.001],
  [/(\d+(?:[.,]\d+)?)\s*(?:ft|feet|foot)\b/i, 0.3048],
];
/** Sözcükten tahmin (metre): metinde sayı yoksa. Önce en belirleyici olanlar. */
const WORDS: [RegExp, number][] = [
  [/palm[- ]?sized|avuç|tiny|minik|minicik/i, 0.1],
  [/bebek|baby|infant/i, 0.6],
  [/small|küçük|ufak/i, 0.5],
  [/child|kid|çocuk/i, 1.2],
  [/giant|dev\b|devasa|huge|enormous/i, 3.0],
  [/tall|uzun boylu/i, 1.9],
  [/short|kısa boylu/i, 1.55],
];

export interface Height { m: number; source: "measure" | "word" | "default" }

/** Varlığın gerçek boyu (metre) ölçek metninden: önce sayı ve birim ("1,80 m", "30 cm"), sonra sözcük ("palm-sized",
    "çocuk"), yoksa varsayılan (insan 1,70; insan olmayan 1,0). */
export function heightOf(scale: string | null | undefined, human: boolean): Height {
  const text = scale ?? "";
  for (const [re, f] of UNITS) {
    const m = re.exec(text);
    if (m) return { m: Number(m[1].replace(",", ".")) * f, source: "measure" };
  }
  for (const [re, m] of WORDS) if (re.test(text)) return { m, source: "word" };
  return { m: human ? 1.7 : 1.0, source: "default" };
}

export interface LineupItem { id: string; name: string; height: Height; px: number }

/** Boy sıralaması: soldan sağa uzundan kısaya, en uzun karakter `tallPx` piksel; çizgiler 0,5 m aralıklı. */
export function lineup(chars: { id: string; name: string; scale?: string | null; human: boolean }[], tallPx: number):
  { items: LineupItem[]; maxM: number; grid: number[] } {
  const items = chars.map((c) => ({ id: c.id, name: c.name, height: heightOf(c.scale, c.human), px: 0 }))
    .sort((a, b) => b.height.m - a.height.m || a.name.localeCompare(b.name, "tr"));
  const maxM = Math.max(0.5, ...items.map((x) => x.height.m));
  for (const x of items) x.px = Math.max(4, Math.round((x.height.m / maxM) * tallPx));
  const step = maxM > 4 ? 1 : 0.5;
  const grid = Array.from({ length: Math.floor(maxM / step) }, (_, i) => Number(((i + 1) * step).toFixed(2)));
  return { items, maxM, grid };
}

/** Metre, Türkçe: "1,70 m", "0,1 m". */
export const meters = (m: number) => `${(Math.round(m * 100) / 100).toString().replace(".", ",")} m`;
