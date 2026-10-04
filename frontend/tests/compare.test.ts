import { describe, expect, it } from "vitest";
import { bestIndex, candidates, defaultSelection, deltaFrom, measureRows, needsResync, parseIds, playable, toggle } from "@/lib/compare";
import { ALIGN_TR } from "@/lib/i18n";
import type { Scene, Variation } from "@/lib/types";

const v = (id: string, over: Partial<Variation> = {}): Variation => ({
  id, tier: "full", wave_no: 0, status: "passed", seed: 1, score: 0.9, rank: null, cost_usd: 0.2, anomalies: [],
  reason: null, stage: null, measure: null, thumb_url: null, video_url: `/api/media?key=variations/${id}.mp4`, source: "generate",
  revisions: 0, revision: null, ...over,
});
const scene = (variations: Variation[]) => ({ id: "s", variations } as unknown as Scene);

describe("candidates", () => {
  it("excludes clips that cannot be played, keeps quarantined ones", () => {
    expect(playable(v("a", { status: "running" }))).toBe(false);
    expect(playable(v("a", { status: "failed" }))).toBe(false);
    expect(playable(v("a", { video_url: null }))).toBe(false);
    expect(playable(v("a", { status: "purged", reason: "quality_gate", video_url: "/api/media?key=quarantine/a.mp4" }))).toBe(true);
  });

  it("orders the refined pool by rank first, then the rest by score", () => {
    const sc = scene([v("low", { score: 0.5 }), v("r2", { rank: 2, score: 0.93 }), v("high", { score: 0.99 }), v("r1", { rank: 1, score: 0.95 })]);
    expect(candidates(sc).map((x) => x.id)).toEqual(["r1", "r2", "high", "low"]);
  });

  it("opens on the refined pool, topping up with the best others when the pool is small", () => {
    expect(defaultSelection(scene([v("a", { rank: 1 }), v("b", { rank: 2 }), v("c", { rank: 3 }), v("d", { score: 0.99 })])))
      .toEqual(["a", "b", "c"]);
    expect(defaultSelection(scene([v("a", { rank: 1 }), v("d", { score: 0.99 }), v("e", { score: 0.4 })]))).toEqual(["a", "d"]);
    expect(defaultSelection(scene([v("x", { score: 0.7 }), v("y", { score: 0.8 }), v("z", { score: 0.6 })]))).toEqual(["y", "x"]);
  });
});

describe("selection", () => {
  it("toggles and drops the oldest pick past four", () => {
    expect(toggle(["a", "b"], "b")).toEqual(["a"]);
    expect(toggle(["a", "b", "c", "d"], "e")).toEqual(["b", "c", "d", "e"]);
  });

  it("drops ids from the URL that are unknown, unplayable or duplicated", () => {
    const sc = scene([v("a"), v("b"), v("c", { status: "running" })]);
    expect(parseIds("a,zz,c,b,a", sc)).toEqual(["a", "b"]);
    expect(parseIds(null, sc)).toEqual([]);
    expect(parseIds("a", undefined)).toEqual([]);
  });
});

describe("measurements", () => {
  it("compares score, alignment and faces, marking a single winner per row", () => {
    const rows = measureRows([
      v("a", { score: 0.91, measure: { alignment: { style: 0.8, content: 0.9 }, identity: { Kerem: 0.7 }, faces_seen: 1 } }),
      v("b", { score: 0.94, measure: { alignment: { style: 0.8, content: 0.85 }, identity: { Ayşe: 0.6, Kerem: 0.5 }, faces_seen: 2 } }),
    ], ALIGN_TR);
    expect(rows.map((r) => r.label)).toEqual(["Birleşik skor", "Stil uyumu", "Tarif uyumu", "Ayşe yüzü", "Kerem yüzü"]);
    expect(rows.map((r) => r.best)).toEqual([1, null, 0, null, 0]);     // stilde beraberlik; Ayşe yalnızca birinde
    expect(rows[3].values).toEqual([null, 0.6]);
  });

  it("has no winner when values are missing or tied", () => {
    expect(bestIndex([null, null])).toBeNull();
    expect(bestIndex([0.5, 0.5])).toBeNull();
    expect(bestIndex([0.5, null, 0.7])).toBe(2);
  });

  it("writes deltas against the leader with a Turkish decimal comma", () => {
    expect(deltaFrom(0.912, 0.946)).toBe("−0,034");
    expect(deltaFrom(0.95, 0.95)).toBe("");
    expect(deltaFrom(null, 0.9)).toBe("");
  });
});

describe("sync", () => {
  it("resyncs only past the drift threshold and lets a shorter clip wait at its end", () => {
    expect(needsResync(2.0, 2.05, 5)).toBe(false);
    expect(needsResync(2.0, 2.2, 5)).toBe(true);
    expect(needsResync(4.5, 2.999, 3)).toBe(false);          // 3 sn'lik klip sonunda bekler, başa sarılmaz
  });
});
