import { describe, expect, it } from "vitest";
import { audioValue, defaultResolution, engineChoices, engineShort, isFal, modeValue, parseDuration, resolutionValue, resolutionsFor } from "@/lib/engines";
import { ENGINE_CHOICES, ENGINE_TR } from "@/lib/i18n";
import { usdTr } from "@/lib/text";
import type { VideoMeta } from "@/lib/types";

const video: VideoMeta = {
  draft_engine: "h3max", final_engine: "seedance25",
  engines: [
    { id: "h3max", label: "MiniMax H3 Max (fal.ai)", disabled: false },
    { id: "seedance25", label: "Seedance 2.5 (fal.ai)", disabled: false },
    { id: "seedance", label: "Seedance 2.0 fast (fal.ai)", disabled: false },
    { id: "h3", label: "MiniMax H3 (RunPod)", disabled: true },
  ],
  h3max_resolutions: ["480P", "768P", "1080P"], seedance25_resolutions: ["480p", "720p", "1080p"],
  h3max_resolution: "768P", seedance25_resolution: "720p", seedance25_audio: false,
};

describe("video motorları", () => {
  it("H3 Max varsayılan; RunPod'daki H3 kapalı ve seçilemez", () => {
    const c = engineChoices(video);
    expect(c.map((e) => e.id)).toEqual(["h3max", "seedance25", "seedance", "h3"]);
    expect(c[3]).toEqual({ id: "h3", label: "MiniMax H3 (RunPod) (kapalı)", disabled: true });
    expect(c.filter((e) => e.disabled)).toHaveLength(1);
  });
  it("meta yoksa yerel liste; çekimin eski motoru sona eklenir", () => {
    const c = engineChoices(undefined, "fal_wan");
    expect(c.map((e) => e.id)).toEqual([...ENGINE_CHOICES, "fal_wan"]);
    expect(c[0].label).toBe(ENGINE_TR.h3max);
    expect(engineChoices(video, "h3").map((e) => e.id)).toHaveLength(4);
  });
  it("kuru çalıştırma ve yükseltme yalnızca fal motorlarında", () => {
    expect(isFal("h3max") && isFal("seedance25")).toBe(true);
    expect(isFal("h3") || isFal("seedance") || isFal(null)).toBe(false);
  });
  it("motorun çözünürlükleri ve Ayarlar'daki varsayılanı", () => {
    expect(resolutionsFor("h3max")).toEqual(["480P", "768P", "1080P"]);
    expect(resolutionsFor("seedance25", { ...video, seedance25_resolutions: ["720p"] })).toEqual(["720p"]);
    expect(resolutionsFor("h3")).toEqual([]);
    expect(defaultResolution("h3max", { ...video, h3max_resolution: "1080P" })).toBe("1080P");
    expect(defaultResolution("seedance25")).toBe("720p");
  });
  it("seçici değerleri: auto Ayarlar'daki varsayılandır", () => {
    expect(modeValue({ h3_mode_explicit: null })).toBe("auto");
    expect(modeValue({ h3_mode_explicit: "ref2va" })).toBe("ref2va");
    expect(resolutionValue({ video_opts: {} })).toBe("auto");
    expect(resolutionValue({ video_opts: { resolution: "1080P" } })).toBe("1080P");
    expect(audioValue({})).toBe("auto");
    expect(audioValue({ video_opts: { audio: false } })).toBe("false");
    expect(audioValue({ video_opts: { audio: true } })).toBe("true");
  });
  it("süre 2-15 sn; virgül kabul edilir", () => {
    expect(parseDuration("4,5")).toBe(4.5);
    expect(parseDuration("15")).toBe(15);
    expect(parseDuration("1.9")).toBeNull();
    expect(parseDuration("16")).toBeNull();
    expect(parseDuration("")).toBeNull();
    expect(parseDuration("abc")).toBeNull();
  });
  it("yükseltme düğmesinin kısa adı", () => {
    expect(engineShort("seedance25")).toBe("Seedance 2.5");
    expect(engineShort("h3max")).toBe("H3 Max");
    expect(engineShort("seedance25", "Seedance 2.5 (fal.ai)")).toBe("Seedance 2.5");
  });
});

describe("Türkçe tutar", () => {
  it("ondalık virgül; 1 doların altında 3 ondalık", () => {
    expect(usdTr(0.042)).toBe("0,042 $");
    expect(usdTr(1.25)).toBe("1,25 $");
    expect(usdTr(0)).toBe("0,000 $");
    expect(usdTr(null)).toBe("—");
    expect(usdTr(Number.NaN)).toBe("—");
  });
});
