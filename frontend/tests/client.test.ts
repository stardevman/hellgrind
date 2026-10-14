/* Müşteri onay döngüsü: adım çubuğu çipleri, revizyon turu metni, notların zaman çizelgesindeki yeri, eksikler. */
import { describe, expect, it } from "vitest";
import { gateChips, noteAt, roundText } from "@/lib/client";
import { clientTodos, stepReadiness } from "@/lib/steps";
import type { ClientNote, Overview, Scene } from "@/lib/types";

const scene = (over: Partial<Scene>): Scene => ({ id: "s1", scene_code: "S001_SH01", status: "done", tracks: [], board_status: "approved", ...over } as Scene);
const note = (over: Partial<ClientNote>): ClientNote => ({ id: "n1", version_id: "v", gate: "final", gate_tr: "Final", at_s: 2.75, shot_offset_s: 0.75,
  scene_id: "s2", scene_code: "S001_SH02", text: "ışık", resolved: false, created_at: 1, label: "v001", ...over });
const ov = (gates: Overview["client"] extends infer C ? C extends { gates: infer G } ? G : never : never, notes: ClientNote[] = []): Overview => ({
  project: { id: "p", title: "t", status: "draft", budget_limit_usd: 10, spent_usd: 0, reserved_usd: 0, frozen: false, soft_limited: false, dry_run: true,
    settings: { soft_budget_ratio: .8, require_board_approval: false, asset_min_status: "locked", lint_strictness: "block" },
    style_version: 1, lut: false, continuity: null, asset_counts: {} },
  scenes: [scene({}), scene({ id: "s2", scene_code: "S001_SH02" })], sequences: [], events: [], timeline: { url: "u", cut: null }, draft_timeline: null,
  client: { gates, notes } });

describe("client gates", () => {
  it("chips follow the gate status and whether a version is with the client", () => {
    const o = ov([{ gate: "script", status: "approved", rounds: 0, limit: 2 }, { gate: "design", status: "pending", rounds: 0, limit: 2, sent: false },
      { gate: "draft", status: "pending", rounds: 1, limit: 2, sent: true }, { gate: "final", status: "changes_requested", rounds: 3, limit: 2, sent: true }]);
    expect(gateChips(o, "prep").map((c) => [c.label, c.tone])).toEqual([["Senaryo", "ok"], ["Tasarım", ""]]);
    expect(gateChips(o, "produce")[0]).toMatchObject({ label: "Taslak kurgu", tone: "warn" });
    expect(gateChips(o, "produce")[0].title).toContain("müşteride, karar bekleniyor");
    expect(gateChips(o, "finish").map((c) => c.tone)).toEqual(["bad", ""]);
    expect(gateChips(o, "finish")[0].title).toContain("Revizyon 3/2 (sınır aşıldı)");
    expect(gateChips(undefined, "approve").map((c) => c.label)).toEqual(["Animatik"]);
  });

  it("writes revision rounds against the contract limit", () => {
    expect(roundText({ rounds: 1, limit: 2 })).toBe("Revizyon 1/2");
    expect(roundText({ rounds: 3, limit: 2 })).toBe("Revizyon 3/2 (sınır aşıldı)");
  });

  it("puts a note on its shot in the edit timeline", () => {
    const shots = [{ scene: { id: "s1" }, start: 0, end: 2 }, { scene: { id: "s2" }, start: 2, end: 4 }];
    expect(noteAt(note({}), shots)).toBe(2.75);
    expect(noteAt(note({ shot_offset_s: 9 }), shots)).toBeCloseTo(3.95);
    expect(noteAt(note({ scene_id: null }), shots)).toBeNull();
  });

  it("a change request blocks its step; a version with the client and open notes only warn", () => {
    const o = ov([{ gate: "final", status: "changes_requested", rounds: 1, limit: 2, sent: true }, { gate: "draft", status: "pending", rounds: 0, limit: 2, sent: true }],
      [note({}), note({ id: "n2", resolved: true })]);
    expect(clientTodos(o).map((t) => [t.step, t.todo.text, t.todo.tone])).toEqual([
      ["finish", "Müşteri değişiklik istedi: Final", "bad"], ["produce", "Müşteri onayı bekleniyor: Taslak kurgu", "warn"],
      ["produce", "1 müşteri notu çözülmedi", "warn"]]);
    const st = stepReadiness(o);
    expect(st[2].state).toBe("current");                                           // müşteride bekleyen taslak kurgu
    expect(st[3].todos[0]).toMatchObject({ text: "Müşteri değişiklik istedi: Final", page: "edit", tone: "bad" });
    const only = stepReadiness(ov([{ gate: "final", status: "changes_requested", rounds: 1, limit: 2, sent: true }]));
    expect(only[3].state).toBe("blocked");
  });
});
