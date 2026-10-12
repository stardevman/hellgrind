import { describe, expect, it } from "vitest";
import { cheapestUpscale, completeResultText, draftState, needsUpscale, promptLengthText, remainingText, resolutionBadge, shortId, upscaleName, upscaleOptions, upscaleResultText, upscaleTotal } from "@/lib/finishing";
import type { UpscaleItem } from "@/lib/types";

const DAY = 86400, HOUR = 3600;

describe("taslağın kalan süresi", () => {
  it.each([
    [7 * DAY - HOUR, "kalan 6 gün 23 sa"],
    [2 * DAY, "kalan 2 gün"],
    [5 * HOUR + 12 * 60 + 30, "kalan 5 sa 12 dk"],
    [3 * HOUR, "kalan 3 sa"],
    [12 * 60, "kalan 12 dk"],
    [20, "kalan 1 dk"],
    [0, "süresi doldu"],
    [-5, "süresi doldu"],
    [null, "süresi doldu"],
  ])("%s sn -> %s", (s, text) => expect(remainingText(s as number | null)).toBe(text));

  it("tamamlanan taslak, süresi dolan (kırmızı) ve bekleyen", () => {
    expect(draftState({ remaining_s: 100, expired: false, completed_at: 1 })).toEqual({ text: "tamamlandı", tone: "ok" });
    expect(draftState({ remaining_s: 0, expired: true })).toEqual({ text: "süresi doldu", tone: "bad" });
    expect(draftState({ remaining_s: 6 * DAY + 23 * HOUR, expired: false })).toEqual({ text: "kalan 6 gün 23 sa", tone: "" });
  });

  it("kısa taslak kimliği", () => {
    expect(shortId("local-0123456789abcdef")).toBe("01234567…");
    expect(shortId("abc123")).toBe("abc123");
  });
});

const item = (code: string, usd: number, reserve = usd): UpscaleItem => ({
  variation_id: `v-${code}`, scene_id: `s-${code}`, scene_code: code, seconds: 5, fps: 24, from: "480p",
  cost: { endpoint: "fal-ai/bytedance-upscaler/upscale/video", usd, reserve_usd: reserve, seconds: 5, fps: 24, target: "1080p", tier: "standard" },
});

describe("upscale", () => {
  const totals = {
    "fal-ai/bytedance-upscaler/upscale/video": 0.072, "fal-ai/kandinsky6-vsr": 0.5, "fal-ai/seedvr/upscale/video": 0.31,
    "fal-ai/flashvsr/upscale/video": 0.05, "blackforestlabs/flux-video-upscale": 1.2,
  };

  it("uç nokta adları; bilinmeyen uç nokta yolun sonuyla", () => {
    expect(upscaleName("fal-ai/seedvr/upscale/video")).toBe("SeedVR2");
    expect(upscaleName("fal-ai/new/thing")).toBe("new/thing");
  });

  it("en ucuz uç nokta; boşta null", () => {
    expect(cheapestUpscale(totals)).toEqual({ endpoint: "fal-ai/flashvsr/upscale/video", usd: 0.05 });
    expect(cheapestUpscale({})).toBeNull();
    expect(cheapestUpscale(undefined)).toBeNull();
  });

  it("seçenekler toplamı ve varsayılanı gösterir; meta listesi yoksa toplamların anahtarları", () => {
    const opts = upscaleOptions(Object.keys(totals), totals, "fal-ai/bytedance-upscaler/upscale/video");
    expect(opts[0]).toEqual({ id: "fal-ai/bytedance-upscaler/upscale/video", label: "ByteDance Upscaler · ~0,072 $ (varsayılan)" });
    expect(opts[4].label).toBe("FLUX Video Upscale · ~1,20 $");
    expect(upscaleOptions(undefined, { "fal-ai/kandinsky6-vsr": 0.5 }).map((o) => o.id)).toEqual(["fal-ai/kandinsky6-vsr"]);
    expect(upscaleOptions(["fal-ai/kandinsky6-vsr"], undefined)[0].label).toBe("Kandinsky 6 VSR");
  });

  it("toplam klip başına ayrılan tutarların toplamıdır", () => {
    expect(upscaleTotal([item("S001_SH01", 0.036), item("S001_SH02", 0.036, 0.04)])).toBe(0.076);
    expect(upscaleTotal([])).toBe(0);
  });

  it("sonuç metni: kapsam, uç nokta, toplam ve çekimler", () => {
    const r = { scope: "project" as const, endpoint: "fal-ai/flashvsr/upscale/video", items: [item("S001_SH01", 0.03), item("S002_SH01", 0.02)], total_usd: 0.05, queued: 2 };
    expect(upscaleResultText(r)).toBe(
      "2 klip FlashVSR ile 1080p'ye çıkarılıyor (tahmini ~0,050 $): S001_SH01, S002_SH01; bitince film yeniden birleştirilir, renk eşleme ardından yapılır.");
    expect(upscaleResultText({ ...r, scope: "variation", items: [item("S001_SH01", 0.03)], total_usd: 0.03, queued: 1 }))
      .toMatch(/orijinal klip varyasyon olarak kalır, renk eşleme upscale'den sonra yeniden yapılır\.$/);
  });

  it("1080p rozeti: upscale, tamamlama ya da 1080p üretim; değilse upscale gerekir", () => {
    expect(resolutionBadge({ resolution: "1080p", upscaled_from: "v0" })?.long).toBe("1080p · upscale");
    expect(resolutionBadge({ resolution: "1080p", completed_from: "v0" })?.long).toBe("1080p · tamamlandı");
    expect(resolutionBadge({ resolution: "1080P" })?.short).toBe("1080p");
    expect(resolutionBadge({ resolution: "480P" })).toBeNull();
    expect(needsUpscale({ resolution: "720p" })).toBe(true);
    expect(needsUpscale({ resolution: "1080p" })).toBe(false);
  });
});

describe("plan ve tamamlama metinleri", () => {
  it("istem uzunluğu ve sınır", () => {
    expect(promptLengthText(1234, 50000)).toEqual({ text: "İstem: 1.234 karakter / 50.000", over: false });
    expect(promptLengthText(812, null)).toEqual({ text: "İstem: 812 karakter", over: false });
    expect(promptLengthText(60000, 50000)?.over).toBe(true);
    expect(promptLengthText(undefined)).toBeNull();
  });

  it("taslağı tamamlama sonucu parçayı ve ayrılan tutarı söyler", () => {
    expect(completeResultText({ queued: true, part: ["S001_SH01", "S001_SH02"], total_usd: 11.64, reserve_usd: 13.97, draft_id: "d" }))
      .toBe("S001_SH01 + S001_SH02 Seedance taslağı 1080p'ye tamamlanıyor (tahmini ~11,64 $, ayrılan ~13,97 $); taslak kesimleri varyasyon olarak kalır.");
  });
});
