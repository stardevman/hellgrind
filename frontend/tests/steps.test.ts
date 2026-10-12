/* Adım modeli: dört adımın durumu ve sıradaki iş (saf fonksiyonlar). */
import { describe, expect, it } from "vitest";
import { nextTodo, STEPS, stepOf, stepReadiness } from "@/lib/steps";
import type { Overview, Scene } from "@/lib/types";

const scene = (over: Partial<Scene>): Scene => ({ id: Math.random().toString(36).slice(2), scene_code: "S001_SH01", status: "done", engine: "h3max", sequence_id: null,
  board_status: "approved", board_note: null, board_url: null, upgrade_requested: false, cut_tier: null, draft_url: null,
  method: "generate", method_hint: false, external_url: null, guide_url: null, ...over } as Scene);
const ov = (scenes: Scene[], over: Partial<Overview["project"]> = {}, timeline: Overview["timeline"] = null): Overview => ({
  project: { id: "p", title: "t", status: "draft", budget_limit_usd: 10, spent_usd: 0, reserved_usd: 0, frozen: false, soft_limited: false, dry_run: true,
    settings: { soft_budget_ratio: .8, require_board_approval: true, asset_min_status: "locked", lint_strictness: "block" },
    style_version: 1, lut: false, continuity: null, asset_counts: { locked: 2 }, ...over },
  scenes, sequences: [], events: [], timeline, draft_timeline: null,
});

describe("steps", () => {
  it("covers all eight pages once, in production order", () => {
    expect(STEPS.flatMap((s) => s.pages)).toEqual(["script", "style", "assets", "board", "edit", "compare", "color", "deliver"]);
    expect(stepOf("compare").id).toBe("produce");
  });

  it("without a project only preparation is open and the next job is the script", () => {
    const st = stepReadiness(undefined);
    expect(st.map((s) => s.state)).toEqual(["current", "todo", "todo", "todo"]);
    expect(nextTodo(st)?.todo).toMatchObject({ page: "script" });
  });

  it("unlocked assets block preparation only when a shot is blocked; otherwise they warn", () => {
    const warn = stepReadiness(ov([scene({ status: "done" })], { asset_counts: { locked: 1, draft: 1 } }));
    expect(warn[0].state).toBe("done");
    expect(warn[0].todos[0]).toMatchObject({ text: "1 varlık kilitsiz", tone: "warn" });
    const bad = stepReadiness(ov([scene({ status: "blocked" })], { asset_counts: { locked: 1, draft: 1 } }));
    expect(bad[0].state).toBe("blocked");
    expect(nextTodo(bad)?.todo).toMatchObject({ page: "assets", tone: "bad" });
  });

  it("board approval gates the approval step only when the project requires it", () => {
    const shots = [scene({ board_status: "pending", status: "pending" }), scene({ status: "pending" })];
    const on = stepReadiness(ov(shots));
    expect(on[1]).toMatchObject({ state: "current", todos: [{ text: "1 kare onay bekliyor", page: "board" }] });
    expect(on[2].state).toBe("todo");
    const off = stepReadiness(ov(shots, { settings: { soft_budget_ratio: .8, require_board_approval: false, asset_min_status: "locked", lint_strictness: "block" } }));
    expect(off[1].state).toBe("done");
    expect(off[2]).toMatchObject({ state: "current", todos: [{ text: "2 çekim henüz bitmedi", page: "edit" }] });
  });

  it("finishing waits for colour jumps and the assembled film, then everything is done", () => {
    const shots = [scene({}), scene({})];
    const jump = stepReadiness(ov(shots, { continuity: { pairs: [{ from: "a", to: "b", to_id: "x", jump: true, delta: { brightness: 1, warmth: 0, saturation: 0 } }] } }));
    expect(jump[3].todos.map((t) => t.text)).toEqual(["1 renk sıçraması", "Film henüz birleştirilmedi"]);
    const done = stepReadiness(ov(shots, {}, { url: "u", cut: null }));
    expect(done.map((s) => s.state)).toEqual(["done", "done", "done", "done"]);
    expect(nextTodo(done)).toBeNull();
  });

  it("a frozen project marks production as blocked", () => {
    const st = stepReadiness(ov([scene({ status: "frozen" })], { frozen: true }));
    expect(st[2].state).toBe("blocked");
  });
});
