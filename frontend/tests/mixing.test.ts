import { describe, expect, it } from "vitest";
import {
  ADOPT_LABEL, SHOT_LABEL, adoptText, boundarySources, cutsText, mixActions, promoteText, sceneEngineText, sec2, shotBoundaries,
  shotEngineText, wholeSceneSwitch,
} from "@/lib/mixing";
import type { Generation } from "@/lib/types";

const MSG = "Bu çekim başka bir üretimden; geçiş tutarsız olabilir. Bütün sahneyi bu motorla değiştir ya da bu çekimi ayrı üret.";
const gen: Generation = { consistent: false, part: "p1", engine: "seedance25", variation_id: "v9", mismatched: ["S001_SH01", "S001_SH03"], message: MSG };

describe("wholeSceneSwitch", () => {
  const meta = { video_input_mode: "reference" as const, scene_render_mode: "whole_scene" as const };
  it("applies to the whole scene only in reference + whole_scene between fal engines", () => {
    expect(wholeSceneSwitch(meta, "h3max", "seedance25")).toBe(true);
    expect(wholeSceneSwitch({ video_input_mode: "reference" }, "seedance25", "h3max")).toBe(true);   // varsayılan whole_scene
    expect(wholeSceneSwitch({ ...meta, scene_render_mode: "per_shot" }, "h3max", "seedance25")).toBe(false);
    expect(wholeSceneSwitch({ ...meta, video_input_mode: "keyframe" }, "h3max", "seedance25")).toBe(false);
    expect(wholeSceneSwitch(meta, "h3max", "seedance")).toBe(false);
    expect(wholeSceneSwitch(meta, "h3", "h3max")).toBe(false);
    expect(wholeSceneSwitch(meta, "h3max", "h3max")).toBe(false);
    expect(wholeSceneSwitch(undefined, "h3max", "seedance25")).toBe(false);
  });
});

describe("mixActions", () => {
  it("maps the promote options by reading variation_id and engine from their paths", () => {
    const options = [
      { action: "adopt", label: ADOPT_LABEL, path: "/scenes/s1/generation/adopt?variation_id=v42" },
      { action: "shot", label: SHOT_LABEL, path: "/scenes/s1/shot-engine?engine=h3max" },
      { action: "unknown", label: "?", path: "/x" },
    ];
    expect(mixActions(gen, options)).toEqual([
      { action: "adopt", label: ADOPT_LABEL, variationId: "v42" },
      { action: "shot", label: SHOT_LABEL, engine: "h3max" },
    ]);
  });

  it("falls back to the scene's generation when there are no options, and to nothing without a generation", () => {
    expect(mixActions(gen)).toEqual([
      { action: "adopt", label: ADOPT_LABEL, variationId: "v9" },
      { action: "shot", label: SHOT_LABEL, engine: "seedance25" },
    ]);
    expect(mixActions(null)).toEqual([]);
    expect(mixActions(gen, [{ action: "adopt", label: "", path: "/scenes/s1/generation/adopt" }]))
      .toEqual([{ action: "adopt", label: ADOPT_LABEL, variationId: "v9" }]);
  });
});

describe("result texts", () => {
  it("a mixed promote keeps the stages and adds the warning with the mismatched shots", () => {
    expect(promoteText({ stages: ["LIPSYNC", "MUX"] })).toEqual({ text: "Yalnızca dudak senkronu ve montaj; görüntü motoru çağrılmadı.", kind: "ok" });
    const x = promoteText({ stages: ["LIPSYNC", "MUX"], warning: MSG, mixed: gen });
    expect(x.kind).toBe("");
    expect(x.text).toBe(`Yalnızca dudak senkronu ve montaj; görüntü motoru çağrılmadı. ${MSG} Farklı üretimden: S001_SH01, S001_SH03.`);
    expect(promoteText({ stages: [], blocked: { missing: [{ name: "Taksi" }] } }).kind).toBe("err");
  });

  it("adopt says nothing is regenerated and nothing is paid", () => {
    expect(adoptText({ adopted: true, engine: "seedance25", part: ["S001_SH01", "S001_SH02"], stages: ["LIPSYNC", "MUX"], usd: 0 }, "Seedance 2.5"))
      .toBe("S001_SH01, S001_SH02 artık tek üretimden (Seedance 2.5); yeniden üretim yok, ücret yok. Yalnızca dudak senkronu ve montaj; görüntü motoru çağrılmadı.");
  });

  it("scene and shot engine changes name the shots", () => {
    const dispatched = { S001_SH01: { stages: ["VISUAL", "MUX"] }, S001_SH02: { stages: [], busy: "işleniyor" } };
    expect(sceneEngineText({ engine: "h3max", shots: ["S001_SH01", "S001_SH02"], total_usd: 0.42, dispatched }, "H3 Max"))
      .toBe("Bütün sahne H3 Max ile yeniden üretiliyor: S001_SH01, S001_SH02 (tahmini ~0,420 $). Üretime alındı: S001_SH01. Şu an işlendiği için atlandı: S001_SH02.");
    expect(shotEngineText({ engine: "seedance25", relinked: ["S001_SH02", "S001_SH03"], dispatched: { S001_SH02: { stages: ["VISUAL"] }, S001_SH03: { stages: ["VISUAL"] } }, note: "" }, "Seedance 2.5", "S001_SH02"))
      .toBe("S001_SH02 Seedance 2.5 ile ayrı üretiliyor; önceki çekimin son karesiyle başlar. S001_SH03 bu çekimin son karesiyle yeniden bağlanıp yeniden üretiliyor. Üretime alındı: S001_SH02, S001_SH03.");
  });
});

describe("split boundaries", () => {
  const codes = ["S001_SH01", "S001_SH02", "S001_SH03"];
  const split = { cuts: [3.4], sources: ["cut", "planned"], warning: "Klipte 1 kesme bulundu, senaryoda 2 çekim sınırı var" };

  it("formats seconds with a Turkish comma and two decimals", () => expect(sec2(3.4)).toBe("3,40"));

  it("a middle shot has an in and an out boundary with their sources and planned values", () => {
    const b = shotBoundaries(split, [3.4, 7.1], [3, 7], codes, "S001_SH02");
    expect(b.map((x) => x.text)).toEqual([
      "Kesme S001_SH01 → S001_SH02: 3,40 sn (klipten) · planlı 3,00 sn",
      "Kesme S001_SH02 → S001_SH03: 7,10 sn (planlı sınırdan) · planlı 7,00 sn",
    ]);
    expect(b.map((x) => x.source)).toEqual(["cut", "planned"]);
  });

  it("the first and last shots have one boundary; a single-shot part or a missing segment has none", () => {
    expect(shotBoundaries(split, [0, 3.4], [0, 3], codes, "S001_SH01").map((x) => x.text)).toEqual(["Kesme S001_SH01 → S001_SH02: 3,40 sn (klipten) · planlı 3,00 sn"]);
    expect(shotBoundaries(split, [7.1, 10], null, codes, "S001_SH03").map((x) => x.text)).toEqual(["Kesme S001_SH02 → S001_SH03: 7,10 sn (planlı sınırdan)"]);
    expect(shotBoundaries(split, [0, 5], [0, 5], ["S001_SH01"], "S001_SH01")).toEqual([]);
    expect(shotBoundaries(split, null, [0, 3], codes, "S001_SH01")).toEqual([]);
  });

  it("lists every inner boundary's source and the detected cuts", () => {
    expect(boundarySources(split, codes)).toEqual(["S001_SH01 → S001_SH02: klipteki kesme", "S001_SH02 → S001_SH03: planlı sınır"]);
    expect(cutsText(split)).toBe("Klipte 1 kesme bulundu (3,40 sn)");
    expect(cutsText({ cuts: [] })).toBe("Klipte kesme bulunamadı");
    expect(cutsText(null)).toBe("");
  });
});
