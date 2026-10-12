import { describe, expect, it } from "vitest";
import { approveText, awaitingPlanText, blockedText, dispatchText, gateLines, passText, planWarnings, resultText, segmentText, upgradeText } from "@/lib/text";
import type { DispatchAll, UpgradeFinal, VideoGates } from "@/lib/types";

const all = (over: Partial<DispatchAll> = {}): DispatchAll => ({ planned: {}, busy: [], blocked: {}, capped: [], frozen: false, ...over });

describe("plan approval texts", () => {
  it("render result awaiting the plan names the part and estimate", () => {
    expect(resultText({ stages: [], awaiting_plan: true, plan: { part: ["S001_SH01", "S001_SH02"], plan_hash: "h", usd: 0.42 } }))
      .toBe("Plan onayı bekliyor: S001_SH01, S001_SH02 (tahmini ~0,420 $). Denetçideki fal.ai video bölümünden planı inceleyip onaylayın.");
    expect(awaitingPlanText(["S002_SH01"])).toMatch(/^Plan onayı bekliyor: S002_SH01\. /);
  });

  it("render-all and passes list the shots awaiting the plan", () => {
    expect(dispatchText(all({ awaiting_plan: ["S001_SH01"] })).text).toBe("Üretime alınan çekim yok. Plan onayı bekliyor: S001_SH01.");
    expect(passText(all({ awaiting_plan: ["S001_SH01"] }), "Taslak")).toContain("Plan onayı bekliyor: S001_SH01.");
  });

  it("a reference-mode block asks for images, not locking", () => {
    expect(blockedText({ missing: [{ name: "Taksi", status: "no_image" }] }))
      .toBe("Engellendi: Taksi için referans görseli yok. Varlıklar sayfasından görsel yükleyin; görseli olmayan varlıkla video üretilmez.");
  });

  it("approve: plan only, then plan and render", () => {
    const r = { approved: true, plan_hash: "h", part: ["S001_SH01", "S001_SH02"], total_usd: 1.2,
      dispatched: { S001_SH01: { stages: ["VISUAL", "MUX"] }, S001_SH02: { stages: [], busy: "işleniyor" } } };
    expect(approveText({ ...r, dispatched: {} }, false))
      .toBe('Plan onaylandı: S001_SH01 + S001_SH02 tek istekte (tahmini ~1,20 $). Üretim için "Onayla ve üret" ya da "Bu sahneyi baştan üret".');
    expect(approveText(r, true))
      .toBe("Plan onaylandı: S001_SH01 + S001_SH02 tek istekte (tahmini ~1,20 $). Üretime alındı: S001_SH01. Şu an işlendiği için atlandı: S001_SH02.");
    expect(approveText({ ...r, dispatched: { S001_SH01: { stages: [] } } }, true)).toMatch(/Üretilecek aşama yok/);
  });

  it("upgrade names the whole scene and the total", () => {
    const r: UpgradeFinal = { stages: ["VISUAL"], engine: "seedance25", from_engine: "h3max", draft_variation_id: "v",
      shots: ["S001_SH01", "S001_SH02"], total_usd: 2.5, dispatched: { S001_SH01: { stages: ["VISUAL"] }, S001_SH02: { stages: ["VISUAL"] } } };
    expect(upgradeText(r, "Seedance 2.5", "S001_SH01")).toBe(
      "S001_SH01, S001_SH02 Seedance 2.5 ile yeniden üretiliyor (tahmini ~2,50 $); taslak klipler varyasyon olarak kaldı (Bunu kullan ile dönülebilir). Üretime alındı: S001_SH01, S001_SH02.");
  });
});

describe("planWarnings", () => {
  it("drops warnings shown elsewhere (missing, dropped, continuity note)", () => {
    const p = {
      warnings: ["Eksik referans: Taksi", "Referans sınırı (9 görsel): düşenler Köprü", "Sekansın ilk çekimi: önceki klip yok.", "Dosya sınırı aşıldı (10 > 9)."],
      missing: [{ kind: "prop", name: "Taksi", asset_id: "a" }], dropped: [{ kind: "location", name: "Köprü" }],
      continuity: { transition: "continuous", previous: null, frame_key: null, expected: false, winner_id: null, note: "Sekansın ilk çekimi: önceki klip yok." },
    };
    expect(planWarnings(p)).toEqual(["Dosya sınırı aşıldı (10 > 9)."]);
    expect(planWarnings({ warnings: ["Eksik referans: Taksi"] })).toEqual(["Eksik referans: Taksi"]);
  });
});

describe("gateLines", () => {
  it("dry scorer says nothing was measured", () => {
    const g: VideoGates = { step: 0.5, scorer: "dry", passed: true, anomalies: [] };
    expect(gateLines(g).map((x) => x.t)).toEqual(["Örnek kare yok: ücretsiz modun sahte skoru (kimlik ve ölçek ölçülmedi)", "Video kapılarından geçti"]);
  });

  it("real gates: identity, scale over seconds, face count and Turkish anomalies", () => {
    const g: VideoGates = {
      step: 0.5, frames: 24, identity: { identity: { Mila: 0.812 }, look: { Bolt: 0.6 } },
      scale: { Bolt: { max: 0.4, measured: 20, over: [3.5, 4], peak: 0.9, ok: false }, Mila: { max: 2, measured: 20, over: [], peak: 1.1, ok: true } },
      face_counts: [1, 2, 1], passed: false, anomalies: ["scale_mismatch"],
    };
    const lines = gateLines(g);
    expect(lines.map((x) => x.t)).toEqual([
      "24 örnek kare, 0,5 sn'de bir",
      "Kimlik: Mila 0,81",
      "Benzerlik: Bolt 0,60",
      "Ölçek Bolt: tutmuyor, 3,5, 4,0 sn'de büyük (tepe 0,90 > 0,40)",
      "Ölçek Mila: tutuyor (tepe 1,10, sınır 2,00; 20 karede ölçüldü)",
      "Yüz sayısı: en çok 2",
      "Video kapılarında kaldı: boyut tutmadı (gerçek boyutundan büyük)",
    ]);
    expect(lines.filter((x) => x.bad).length).toBe(2);
  });

  it("segment text", () => {
    expect(segmentText({ part: ["S001_SH01", "S001_SH02"], segment: [4, 8.5] })).toBe("S001_SH01 + S001_SH02 parçasından 4,0–8,5 sn");
    expect(segmentText({ part: null, segment: null })).toBe("");
  });
});
