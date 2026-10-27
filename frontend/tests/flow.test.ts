import { describe, expect, it } from "vitest";
import { codeOf, matchClips } from "@/lib/flow";

describe("codeOf", () => {
  it.each([
    ["S001_SH02.mp4", "S001_SH02"],
    ["s002_sh01_flow2.mp4", "S002_SH01"],
    ["Flow - S010_SH12 (1).mp4", "S010_SH12"],
    ["kovalama.mp4", null],
  ])("%s", (name, code) => expect(codeOf(name)).toBe(code));
});

describe("matchClips", () => {
  const scenes = [{ id: "a", scene_code: "S001_SH01" }, { id: "b", scene_code: "S001_SH02" }];
  it("eşler, bilinmeyenleri ve ikinci dosyayı ayırır", () => {
    const r = matchClips([{ name: "S001_SH01.mp4" }, { name: "s001_sh01_v2.mp4" }, { name: "S009_SH01.mp4" }, { name: "x.mp4" }], scenes);
    expect(r.matched.map((m) => [m.code, m.sceneId])).toEqual([["S001_SH01", "a"]]);
    expect(r.duplicate).toEqual(["s001_sh01_v2.mp4"]);
    expect(r.unknown).toEqual(["S009_SH01.mp4", "x.mp4"]);
  });
});
