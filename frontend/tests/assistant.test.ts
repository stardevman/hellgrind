import { describe, expect, it } from "vitest";
import { actionOutcome, ago, costLabel, type AssistantAction } from "../src/lib/assistant";

const action = (over: Partial<AssistantAction>): AssistantAction => ({
  id: "a", tool: "rerender_shot", title: "Çekimi yeniden üret", summary: "S013", estimate_usd: 0.42, status: "pending",
  result: null, scene_id: null, virtual: false, ...over,
});

describe("assistant helpers", () => {
  it("shows the estimate in Turkish money format and marks free mode as virtual", () => {
    expect(costLabel(action({}))).toBe("Tahmini 0,42 $");
    expect(costLabel(action({ virtual: true }))).toBe("Tahmini 0,42 $ (ücretsiz mod: sanal)");
    expect(costLabel(action({ estimate_usd: null }))).toContain("dudak senkronu");
  });

  it("describes each decision", () => {
    expect(actionOutcome(action({}))).toBeNull();
    expect(actionOutcome(action({ status: "declined" }))).toContain("hiçbir şey üretilmedi");
    expect(actionOutcome(action({ status: "done", result: { ok: true, stages: ["VISUAL", "MUX"] } }))).toBe("Onaylandı, başlatıldı (VISUAL, MUX).");
    expect(actionOutcome(action({ status: "done", result: { ok: true, by: "session" } }))).toBe("Oturum izniyle başlatıldı.");
    expect(actionOutcome(action({ status: "failed", result: { error: "Bütçe yetmiyor" } }))).toBe("Başlatılamadı: Bütçe yetmiyor");
  });

  it("formats recency", () => {
    expect(ago(1000, 1030)).toBe("az önce");
    expect(ago(1000, 1000 + 600)).toBe("10 dk önce");
    expect(ago(1000, 1000 + 7200)).toBe("2 sa önce");
    expect(ago(null)).toBe("");
  });
});
