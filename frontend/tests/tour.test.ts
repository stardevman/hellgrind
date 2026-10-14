/* Rehber turu: her adımın hedefi arayüzde bir data-tour öğesine bağlı olmalı; metinler Türkçe. */
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { TOUR_STEPS, tourStepsFor } from "@/lib/tour";

const dir = join(__dirname, "..", "src", "components");
const SRC = readdirSync(dir).map((f) => readFileSync(join(dir, f), "utf8")).join("\n");

describe("tour", () => {
  it("every step targets an element that exists in the components", () => {
    for (const s of TOUR_STEPS) {
      const found = SRC.includes(`data-tour="${s.target}"`) || SRC.includes(`tour="${s.target}"`);
      expect(found, `data-tour="${s.target}" bulunamadı`).toBe(true);
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
    expect(TOUR_STEPS.some((s) => s.target === "todos")).toBe(true);
    expect(TOUR_STEPS[TOUR_STEPS.length - 1].title).toBe("Hepsi bu");
  });
});
