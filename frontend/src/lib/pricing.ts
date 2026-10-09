/* RunPod Serverless fiyat tablosundan panel hesapları (pricing.yaml › serverless.runpod). */
import type { Serverless } from "./types";

export type RenderRows = Record<string, Record<string, number>>;

/** Koşula göre video saniyesi başına GPU saniyesi; eski düz biçim FL2VA sayılır. */
export function renderRows(rp: Serverless): RenderRows {
  const rows = rp.render_seconds as Record<string, unknown>;
  const nested = Object.values(rows).every((v) => typeof v === "object" && v !== null);
  return nested ? (rows as RenderRows) : { fl2va: rows as Record<string, number> };
}

/** Rezervasyonun kullandığı birincil GPU'nun saniye ücreti (tablodaki ilk tip; varsayılan RTX 5090). */
export const primaryRate = (rp: Serverless) => Object.values(rp.per_second)[0] ?? 0;

/** Bir klibin sıcak worker'daki tahmini bedeli (H3 en az 4 sn üretir). */
export function clipCost(rp: Serverless, mode: string, resolution: string, seconds = 4): number {
  const rows = renderRows(rp)[mode === "ref2va" ? "ref2va" : "fl2va"] ?? Object.values(renderRows(rp))[0] ?? {};
  const perSecond = rows[resolution] ?? Math.max(0, ...Object.values(rows));
  return primaryRate(rp) * perSecond * Math.max(4, Math.ceil(seconds));
}
