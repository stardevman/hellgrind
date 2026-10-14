/* Yapılacaklar modeli: dört grubun durumu, sayfaya göre eksikler ve sayfa çubuğu noktaları (saf fonksiyonlar). */
import { describe, expect, it } from "vitest";
import { nextTodo, pageBadges, pageTones, STEPS, stepOf, stepReadiness, todosByPage, voiceTodos } from "@/lib/steps";
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
  it("covers all nine pages once, in production order (voice right after the script)", () => {
    expect(STEPS.flatMap((s) => s.pages)).toEqual(["script", "voice", "style", "assets", "board", "edit", "compare", "color", "deliver"]);
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

  it("the style badge names the active version and its drawing medium", () => {
    expect(pageBadges(ov([scene({})], { style_version: 2, style_medium: "2d" })).style).toMatchObject({ text: "s2 · 2D" });
    expect(pageBadges(ov([scene({})], { style_version: 1, style_medium: "3d" })).style?.title).toContain("çizim türü 3D");
    expect(pageBadges(ov([scene({})], { style_version: null })).style).toBeUndefined();
  });

  it("voice-first lists lines without a final recording (warn) and unlocked sequences (blocks preparation)", () => {
    const line = (source: string) => ({ id: Math.random().toString(36), kind: "dialogue", source, text: "x", emotion: null, start_offset_s: 0.4,
      gain_db: 0, version: 1, character_id: null, character: "Kerem", mood: null });
    const seq = (locked: boolean) => ({ id: Math.random().toString(36), code: "SEQ01", title: "t", seq_order: 1, status: "active", budget_cap_usd: null,
      spent_usd: 0, reserved_usd: 0, shots: 1, done: 0, tracks: [], voice_locked_at: locked ? 1 : null });
    const shots = [scene({ status: "pending", tracks: [line("tts"), line("upload")] } as Partial<Scene>)];
    const settings = { soft_budget_ratio: .8, require_board_approval: false, asset_min_status: "locked", lint_strictness: "block" };
    const off = { ...ov(shots, { settings }), sequences: [seq(false)] };
    expect(voiceTodos(off)).toEqual([]);                                             // ayar kapalı: eksik yok
    const on = { ...ov(shots, { settings: { ...settings, voice_first: true } }), sequences: [seq(false), seq(true)] };
    expect(voiceTodos(on)).toEqual([
      { text: "1 sekansın sesi kilitlenmedi", page: "voice" }, { text: "1 replik final sesi bekliyor", page: "voice", tone: "warn" }]);
    const st = stepReadiness({ ...on, project: { ...on.project, style_version: null } });
    expect(st[0].state).toBe("current");
    expect(st[0].todos.map((t) => t.text)).toEqual(["1 sekansın sesi kilitlenmedi", "Stil kılavuzu etkinleştirilmedi", "1 replik final sesi bekliyor"]);
    expect(nextTodo(st)?.todo).toMatchObject({ page: "voice", text: "1 sekansın sesi kilitlenmedi" });
    const locked = { ...on, sequences: [seq(true)] };
    expect(stepReadiness(locked)[0].state).toBe("done");                              // final ses uyarıdır, engellemez
    expect(pageBadges(locked).voice).toMatchObject({ text: "1/2" });
  });

  it("groups the open jobs by the page that resolves them, blockers first, and colours the page bar dots", () => {
    const st = stepReadiness(ov([scene({ status: "blocked" }), scene({ status: "pending", board_status: "pending" })],
      { asset_counts: { locked: 1, draft: 1 }, style_version: null }));
    const groups = todosByPage(st);
    expect(groups.map((g) => g.page)).toEqual(["style", "assets", "board", "edit", "deliver"]);
    expect(groups[1].todos.map((t) => t.text)).toEqual(["1 varlık kilitsiz", "1 çekim engelli"]);     // ikisi de engelleyici
    const warnFirst = todosByPage([{ id: "prep", state: "current", todos: [{ text: "u", page: "assets", tone: "warn" }, { text: "b", page: "assets", tone: "bad" }, { text: "p", page: "assets" }] }]);
    expect(warnFirst[0].todos.map((t) => t.text)).toEqual(["b", "p", "u"]);                            // engelleyici, sıradan, uyarı
    expect(pageTones(st)).toEqual({ style: "warn", assets: "bad", board: "warn", edit: "todo", deliver: "todo" });
    expect(todosByPage(stepReadiness(ov([scene({})], {}, { url: "u", cut: null })))).toEqual([]);
  });
});
