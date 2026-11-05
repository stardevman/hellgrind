import { describe, expect, it } from "vitest";
import { ADMIN_PAGES, GROUPS, groupEntry, groupOf, groupSignal, pagePath, visiblePages } from "@/lib/groups";

const user = { admin: false, hasShots: true };
const admin = { admin: true, hasShots: true };
const empty = { admin: false, hasShots: false };
const g = (id: string) => GROUPS.find((x) => x.id === id)!;

describe("page groups", () => {
  it("puts every bar page in exactly one of five groups, in production order", () => {
    expect(GROUPS.map((x) => x.label)).toEqual(["Hikâye", "Görünüm", "Storyboard", "Kurgu", "Teslim"]);
    expect(GROUPS.flatMap((x) => x.pages)).toEqual(["script", "voice", "style", "images", "assets", "board", "edit", "compare", "color", "deliver"]);
    for (const x of GROUPS) expect(x.pages).toContain(x.main);
  });

  it("finds the group of a page; settings and history stay outside", () => {
    expect(groupOf("voice")?.id).toBe("story");
    expect(groupOf("images")?.id).toBe("look");
    expect(groupOf("compare")?.id).toBe("edit");
    expect(groupOf("color")?.id).toBe("deliver");
    expect(groupOf("settings")).toBeUndefined();
  });

  it("hides admin pages in user mode", () => {
    expect(ADMIN_PAGES.has("compare")).toBe(true);
    expect(visiblePages(g("edit"), user)).toEqual(["edit"]);
    expect(visiblePages(g("edit"), admin)).toEqual(["edit", "compare"]);
  });

  it("opens the last visited page, else the main page, else the first open page", () => {
    expect(groupEntry(g("look"), user)).toBe("assets");
    expect(groupEntry(g("look"), user, "style")).toBe("style");
    expect(groupEntry(g("look"), user, "edit")).toBe("assets");          // başka grubun sayfası yok sayılır
    expect(groupEntry(g("edit"), user, "compare")).toBe("edit");         // kullanıcı modunda Karşılaştır yok
    expect(groupEntry(g("story"), empty, "voice")).toBe("script");       // çekim yokken Ses kapalı
    expect(groupEntry(g("deliver"), empty)).toBeNull();                  // çekim yokken Teslim kapalı
    expect(groupEntry(g("look"), empty)).toBe("assets");
  });

  it("shows the main badge and the worst tone of the other pages", () => {
    const badges = { voice: { text: "1/2", title: "" } };
    expect(groupSignal(g("story"), user, badges, { script: "bad", voice: "warn" })).toEqual({ badge: badges.voice, tone: "bad" });
    expect(groupSignal(g("story"), user, {}, { voice: "warn", script: "todo" })).toEqual({ badge: undefined, tone: "warn" });
    expect(groupSignal(g("look"), user, { assets: { text: "2/3", title: "" } }, { assets: "bad", style: "warn" }).tone).toBe("warn");
  });

  it("labels pages with their group when it helps", () => {
    expect(pagePath("voice", "Ses")).toBe("Hikâye › Ses");
    expect(pagePath("board", "Storyboard")).toBe("Storyboard");
    expect(pagePath("edit", "Kurgu")).toBe("Kurgu");
    expect(pagePath("color", "Renk")).toBe("Teslim › Renk");
  });
});
