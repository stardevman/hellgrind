/* Rehberler: her adımın hedefi (ve yedeği, tıklanan öğesi) arayüzde bir data-tour öğesine bağlı olmalı; metinler Türkçe. */
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { BOOK_STEPS, BOOK_TEMPLATE, TOUR_STEPS, tourStepsFor, type TourCtx } from "@/lib/tour";
import type { Overview, Scene } from "@/lib/types";

const dir = join(__dirname, "..", "src", "components");
const SRC = readdirSync(dir).map((f) => readFileSync(join(dir, f), "utf8")).join("\n");
// Döngüyle üretilen hedefler: data-tour={`insp-${t}`}, {`settings-${id}`}, {`env-${f.env}`}
const DYNAMIC = [["insp-", "data-tour={`insp-${t}`}"], ["settings-", "data-tour={`settings-${id}`}"], ["env-", "data-tour={`env-${f.env}`}"]];
const exists = (name: string) => SRC.includes(`data-tour="${name}"`) || SRC.includes(`tour="${name}"`)
  || DYNAMIC.some(([prefix, code]) => name.startsWith(prefix) && SRC.includes(code));

describe("tour", () => {
  it("every step targets an element that exists in the components", () => {
    for (const s of [...TOUR_STEPS, ...BOOK_STEPS]) {
      for (const name of [s.target, s.fallback, s.click].filter(Boolean) as string[]) {
        expect(exists(name), `data-tour="${name}" bulunamadı`).toBe(true);
      }
    }
  });

  it("filters steps by whether a project is open and starts with the page bar", () => {
    expect(tourStepsFor(false)[0].target).toBe("pages");
    expect(tourStepsFor(false).some((s) => s.when === "project")).toBe(false);
    expect(tourStepsFor(true).some((s) => s.when === "no-project")).toBe(false);
    expect(tourStepsFor(true).length).toBeGreaterThan(tourStepsFor(false).length);
  });

  it("presents the pages as a menu, not a sequence, and ends with how to reopen the guide", () => {
    expect(TOUR_STEPS[0].text).toContain("menüdür");
    expect(TOUR_STEPS[0].text).toContain("Sıra zorunlu değildir");
    expect(TOUR_STEPS.some((s) => s.target === "todos" || s.target === "next-todo")).toBe(false);
    expect(TOUR_STEPS[TOUR_STEPS.length - 1].title).toBe("Hepsi bu");
    expect(TOUR_STEPS[TOUR_STEPS.length - 1].text).toContain("Rehberler");
  });
});

describe("book tour", () => {
  it("walks from project to delivery in production order", () => {
    const order = ["script-form", "asset-ref-upload", "first-frame", "voice-external", "assemble", "exports"];
    const idx = order.map((t) => BOOK_STEPS.findIndex((s) => s.target === t));
    expect(idx.every((x) => x >= 0)).toBe(true);
    expect([...idx].sort((a, b) => a - b)).toEqual(idx);
    expect(tourStepsFor(true, "book").length).toBe(BOOK_STEPS.length);
  });

  it("keeps the Ponpon lessons: uncropped pages, no chaining, no speech, no tiny names", () => {
    const all = BOOK_STEPS.map((s) => s.text).join(" ");
    expect(all).toContain("kırpmadan");
    expect(all).toContain("Yeni sahne");
    expect(all).toContain("konuşma");
    expect(all).toContain("küçük");
  });

  it("checks real state", () => {
    const sc = (x: Partial<Scene>) => ({ assets: [], keyframe_source: null, status: "blocked", ...x }) as unknown as Scene;
    const ctx = (scenes: Scene[]): TourCtx => ({ admin: true, ov: { scenes, sequences: [], timeline: null } as unknown as Overview });
    const frame = BOOK_STEPS.find((s) => s.target === "first-frame")!;
    expect(frame.check!(ctx([]))).toBeNull();
    expect(frame.check!(ctx([sc({ keyframe_source: "upload" }), sc({})]))).toBe(false);
    expect(frame.check!(ctx([sc({ keyframe_source: "upload" })]))).toBe(true);
    const loc = BOOK_STEPS.find((s) => s.target === "shot-assets")!;
    expect(loc.check!(ctx([sc({ assets: [{ id: "a", type: "location", name: "Kitap", status: "draft" }] })]))).toBe(false);
  });

  it("ships a dialogue-free template where every page is its own scene", () => {
    expect(BOOK_TEMPLATE.match(/^DIŞ\. KİTAP - GÜN$/gm)?.length).toBe(3);
    expect(BOOK_TEMPLATE).not.toMatch(/^\(.*\)$/m);                 // duygu ipucu (replik) yok
  });
});
