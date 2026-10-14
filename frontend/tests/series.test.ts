/* Dizi hiyerarşisi: kırıntı etiketleri, teslim rozeti, varlık kapsamı, yeni sürüm özeti ve adım eksikleri (saf fonksiyonlar). */
import { describe, expect, it } from "vitest";
import { episodeLabel, inScope, lockLabel, upgradeText } from "@/lib/series";
import { seriesTodos, stepReadiness } from "@/lib/steps";
import type { Overview, Scene } from "@/lib/types";

const scene = (over: Partial<Scene>): Scene => ({ id: Math.random().toString(36).slice(2), scene_code: "S001_SH01", status: "done", engine: "h3max",
  sequence_id: null, board_status: "approved", board_note: null, board_url: null, upgrade_requested: false, cut_tier: null, draft_url: null,
  method: "generate", method_hint: false, external_url: null, guide_url: null, last_error: null, ...over } as Scene);
const ov = (scenes: Scene[], over: Partial<Overview["project"]> = {}): Overview => ({
  project: { id: "p", title: "t", status: "draft", budget_limit_usd: 10, spent_usd: 0, reserved_usd: 0, frozen: false, soft_limited: false, dry_run: true,
    settings: { soft_budget_ratio: .8, require_board_approval: false, asset_min_status: "locked", lint_strictness: "block" },
    style_version: 1, lut: false, continuity: null, asset_counts: { locked: 2 }, ...over },
  scenes, sequences: [], events: [], timeline: null, draft_timeline: null,
});
const kerem = { asset_id: "a1", name: "Kerem", type: "character", pinned: 1, current: 2 };

describe("series", () => {
  it("labels an episode by number and title without repeating a default title", () => {
    expect(episodeLabel(3, "Kayıp Kalıp")).toBe("Bölüm 3 · Kayıp Kalıp");
    expect(episodeLabel(1, "Bölüm 1")).toBe("Bölüm 1");
    expect(episodeLabel(null, "Bağımsız film")).toBe("Bağımsız film");
  });

  it("shows the delivered version on the lock badge", () => {
    expect(lockLabel("v004")).toBe("Sürüm sabit · v004");
    expect(lockLabel(null)).toBe("Sürüm sabit");
  });

  it("splits the asset list into the series library and this episode", () => {
    const shared = { series_id: "q" }, local = { series_id: null };
    expect([inScope(shared, ""), inScope(local, "")]).toEqual([true, true]);
    expect([inScope(shared, "series"), inScope(local, "series")]).toEqual([true, false]);
    expect([inScope(shared, "local"), inScope(local, "local")]).toEqual([false, true]);
  });

  it("summarises the upgrade impact before anything is applied", () => {
    expect(upgradeText({ asset_id: "a1", name: "Kerem", pinned: 1, current: 2, affected: ["S001_SH01", "S002_SH02"], expected_usd: 0.42, worst_usd: 1.5 }))
      .toBe("Kerem: bölüm v1 sürümünde, kütüphanede v2. Geçilirse 2 çekim yeniden üretilir: S001_SH01, S002_SH02. Tahmini sanal maliyet $0.42 (en kötü $1.50).");
    expect(upgradeText({ asset_id: "a1", name: "Taksi", pinned: 1, current: 2, affected: [], expected_usd: 0, worst_usd: 0 }))
      .toBe("Taksi: bölüm v1 sürümünde, kütüphanede v2. Hiçbir çekim yeniden üretilmez.");
  });

  it("a newer series version is a preparation todo; red only when a shot is blocked by it", () => {
    expect(seriesTodos(ov([scene({})]))).toEqual([]);
    const warn = seriesTodos(ov([scene({})], { series_updates: [kerem] }));
    expect(warn).toEqual([{ text: "Dizi varlığında yeni sürüm: Kerem (v1 → v2)", page: "assets", tone: "warn" }]);
    const blocked = ov([scene({ status: "blocked", last_error: "Üretim engellendi: dizi kütüphanesinde yeni sürümü olan varlıklar var: Kerem." })],
      { series_updates: [kerem] });
    expect(seriesTodos(blocked)[0].tone).toBe("bad");
    const delivered = ov([scene({})], { series_updates: [kerem], episode: { series: null, season: null, episode_no: 2, locked: { at: "x", label: "v001" } } });
    expect(seriesTodos(delivered)).toEqual([]);                   // teslim edilmiş bölüm geçemez: eksik sayılmaz
    expect(stepReadiness(blocked)[0].todos[0]).toMatchObject({ page: "assets", tone: "bad" });
  });
});
