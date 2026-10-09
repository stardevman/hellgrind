import { describe, expect, it } from "vitest";
import { clipCost, primaryRate, renderRows } from "@/lib/pricing";
import type { Serverless } from "@/lib/types";

const rp: Serverless = {
  per_second: { "RTX 5090": 0.000439, "RTX PRO 6000": 0.000969 },
  render_seconds: { fl2va: { "480p": 11, "720p": 22 }, ref2va: { "480p": 7, "720p": 14 } },
  keyframe_seconds: 45, keyframe_cold_seconds: 140, cold_start_seconds: 140,
};

describe("RunPod fiyatları", () => {
  it("klip başına tahmin ölçüme yakın (sıcak worker, 4 sn, RTX 5090)", () => {
    expect(primaryRate(rp)).toBe(0.000439);
    expect(clipCost(rp, "fl2va", "480p")).toBeCloseTo(0.0193, 4);     // ölçülen 0.0179-0.0192
    expect(clipCost(rp, "ref2va", "480p")).toBeCloseTo(0.0123, 4);    // ölçülen 0.0116-0.0118
    expect(clipCost(rp, "ref2va", "480p", 3)).toBe(clipCost(rp, "ref2va", "480p", 4));   // en az 4 sn üretilir
  });
  it("eski düz biçimi FL2VA sayar", () => {
    expect(renderRows({ ...rp, render_seconds: { "480p": 8 } })).toEqual({ fl2va: { "480p": 8 } });
  });
});
