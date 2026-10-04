import { describe, expect, it } from "vitest";
import { dispatchText, eventText, impactText, measureText, passText, resultText, stagesText, tileSpec } from "@/lib/text";
import type { Variation } from "@/lib/types";

const v = (over: Partial<Variation> = {}): Variation => ({
  id: "v1", tier: "probe", wave_no: 0, status: "passed", seed: 1, score: 0.9, rank: null, cost_usd: 0.2, anomalies: [],
  reason: null, stage: null, measure: null, thumb_url: null, video_url: "/api/media?key=variations/a.mp4", source: "generate",
  revisions: 0, revision: null, ...over,
});

describe("stagesText", () => {
  it.each([
    [[], "Yapılacak iş yok: girdiler değişmedi."],
    [["MUX"], "Yalnızca montaj; görüntü motoru çağrılmadı."],
    [["LIPSYNC", "MUX"], "Yalnızca dudak senkronu ve montaj; görüntü motoru çağrılmadı."],
    [["GRADE", "MUX"], "Yalnızca renk ve montaj; görüntü motoru çağrılmadı."],
    [["LIPSYNC", "GRADE", "MUX"], "Yalnızca dudak senkronu, renk ve montaj; görüntü motoru çağrılmadı."],
    [["VISUAL", "LIPSYNC", "MUX"], "Görüntü baştan üretilecek, ardından ses ve montaj."],
    [["STORYBOARD", "VISUAL", "LIPSYNC", "GRADE", "MUX"], "Storyboard karesi yeniden çizilecek, görüntü baştan üretilecek, ardından ses, renk ve montaj."],
  ])("%j", (stages, text) => expect(stagesText(stages)).toBe(text));

  it("names unknown stages instead of guessing", () => {
    expect(stagesText(["MUX", "NEW"])).toBe("Çalışacak aşamalar: MUX, NEW");
  });

  it("a blocked result names the missing references", () => {
    expect(resultText({ stages: [], blocked: { missing: [{ name: "Taksi" }, { name: "Köprü" }] } }))
      .toBe("Engellendi: Taksi, Köprü için onaylı referans yok. Varlıklar sekmesinden kilitle, sonra yeniden üret.");
  });
});

describe("tileSpec", () => {
  it("refined pool members carry their rank", () => {
    expect(tileSpec(v({ rank: 2 })).state).toBe("refined");
    expect(tileSpec(v({ rank: 2 })).fate).toBe("rafine havuzda, 2. sıra");
  });

  it("quality gate rejections name the anomalies in Turkish; first-frame rejections say no video was made", () => {
    const t = tileSpec(v({ status: "purged", reason: "quality_gate", anomalies: ["black", "freeze"] }));
    expect(t.state).toBe("rejected");
    expect(t.fate).toBe("kalite kontrolünde elendi: siyah kare, donmuş görüntü");
    const f = tileSpec(v({ status: "purged", reason: "quality_gate", stage: "first_frame" }));
    expect(f.fate).toBe("ilk karede elendi, video üretilmedi: eşik altı");
  });

  it("separates retired, failed and working tiles", () => {
    expect(tileSpec(v({ status: "purged", reason: "not_top_k" })).state).toBe("retired");
    expect(tileSpec(v({ status: "failed" })).state).toBe("failed");
    for (const status of ["queued", "running", "generated"]) expect(tileSpec(v({ status })).state).toBe("working");
  });

  it("marks where the full tier starts and describes revisions and sources", () => {
    expect(tileSpec(v({ tier: "full" }), v()).tierStart).toBe(true);
    expect(tileSpec(v({ tier: "full" }), v({ tier: "full" })).tierStart).toBe(false);
    const t = tileSpec(v({ rank: 1, revision: 2, source: "external" }));
    expect(t.fate).toBe("rafine havuzda, 1. sıra, elle düzeltilmiş (revizyon 2), dış çekim");
    expect(t.label).toMatch(/^Test turu 1\. dalga · skor 0\.900 · rafine havuzda/);
  });

  it("includes the real measurement when present", () => {
    expect(measureText({ alignment: { style: 0.91, content: 0.97 }, identity: { Kerem: 0.66 }, faces_seen: 1 }))
      .toBe(" (stil 0,91, tarif 0,97, Kerem yüzü 0,66)");
    expect(measureText({ alignment: { generic: 0.5 }, identity: {}, faces_seen: 2 })).toBe(" (genel 0,50, 2 yüz görüldü)");
  });
});

describe("eventText", () => {
  const codes = { s1: "S001_SH01" };
  it("prefixes the shot code and keeps the tone", () => {
    expect(eventText({ kind: "refined", ts: 0, scene_id: "s1", pool: 3 }, codes)).toEqual({ t: "S001_SH01: rafine havuz hazır (3 klip)", c: "good" });
    expect(eventText({ kind: "frozen", ts: 0, reason: "bütçe" }, codes)).toEqual({ t: "Üretim donduruldu: bütçe", c: "bad" });
    expect(eventText({ kind: "board_status", ts: 0, code: "S001_SH01", status: "changes_requested" }, codes).t)
      .toBe("S001_SH01: kare değişiklik istendi");
  });
  it("falls back to the raw kind for unknown events", () => {
    expect(eventText({ kind: "something_new", ts: 0 }, codes).t).toBe("something_new");
  });
});

describe("pass and dispatch summaries", () => {
  const r = { planned: { S001_SH01: ["VISUAL", "MUX"], S001_SH02: ["MUX"], S002_SH01: [] }, busy: [], blocked: { S002_SH02: {} },
              capped: ["S003_SH01"], awaiting_board: ["S001_SH03"], frozen: false };
  it("lists only the shots that go to the image engine in a pass", () => {
    expect(passText(r, "Taslak geçiş")).toBe(
      "Taslak geçiş: S001_SH01. Kare onayı bekleyen: S001_SH03. Eksik referans: S002_SH02. Sekans tavanında: S003_SH01.");
  });
  it("lists every shot that got work when retrying blocked shots", () => {
    expect(dispatchText(r)).toEqual({ text: "Üretime alındı: S001_SH01, S001_SH02. Hâlâ engelli: S002_SH02. Sekans tavanında: S003_SH01.", bad: true });
  });
  it("style impact names the shots and the virtual cost", () => {
    expect(impactText({ version: 2, affected: [{ scene_code: "S001_SH01", style_only: true }], pending: ["S002_SH01"], expected_usd: 1.2, worst_usd: 3 }))
      .toBe("1 çekim yeniden üretilir: S001_SH01. Tahmini sanal maliyet $1.20 (en kötü durumda $3.00). Henüz üretilmemiş 1 çekim ilk üretiminde bu stili kullanır.");
  });
});
