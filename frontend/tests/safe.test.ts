/* 2D yayın güvenliği: güvenli alan geometrisi ve karakter boy sıralaması (saf fonksiyonlar). */
import { describe, expect, it } from "vitest";
import { fitRect, heightOf, insetPct, lineup, meters, SAFE } from "@/lib/safe";

describe("safe areas", () => {
  it("fits a 16:9 picture inside any box and insets action / title safe", () => {
    expect(fitRect(1600, 900, 16 / 9)).toEqual({ left: 0, top: 0, width: 1600, height: 900 });
    expect(fitRect(1000, 900, 16 / 9)).toEqual({ left: 0, top: 168.75, width: 1000, height: 562.5 });      // letterbox
    expect(fitRect(2000, 900, 16 / 9)).toEqual({ left: 200, top: 0, width: 1600, height: 900 });           // pillarbox
    expect(fitRect(0, 900, 16 / 9).width).toBe(0);
    expect([SAFE.action, SAFE.title]).toEqual([0.93, 0.9]);
    expect([insetPct(SAFE.action), insetPct(SAFE.title)]).toEqual(["3.50%", "5.00%"]);
  });
});

describe("line-up", () => {
  it("reads the real height from the scale text: measure, then word, then default", () => {
    expect(heightOf("1,80 m boyunda", true)).toEqual({ m: 1.8, source: "measure" });
    expect(heightOf("about 30 cm tall", false)).toEqual({ m: 0.3, source: "measure" });
    expect(heightOf("tiny, palm-sized; much smaller than a hen", false)).toEqual({ m: 0.1, source: "word" });
    expect(heightOf("on yaşında bir çocuk", true)).toEqual({ m: 1.2, source: "word" });
    expect(heightOf("uzun saçlı", true)).toEqual({ m: 1.7, source: "default" });                   // saç uzunluğu boy değil
    expect(heightOf(null, false)).toEqual({ m: 1.0, source: "default" });
  });

  it("lines characters up tallest first against the tallest one", () => {
    const r = lineup([{ id: "p", name: "Pip", scale: "palm-sized", human: false }, { id: "n", name: "Nora", scale: "1,65 m", human: true },
      { id: "k", name: "Kerem", scale: "1,80 m", human: true }], 200);
    expect(r.items.map((x) => [x.name, x.px])).toEqual([["Kerem", 200], ["Nora", 183], ["Pip", 11]]);
    expect(r.grid).toEqual([0.5, 1, 1.5]);
    expect(meters(1.65)).toBe("1,65 m");
  });
});
