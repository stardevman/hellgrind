import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../src/lib/api";
import {
  denyEffect, dropStaleWaiters, hasWaiter, settleConsent, STALE_AFTER_MS, totalUsd, waitForConsent, type ConsentRequest,
} from "../src/lib/consent";

const req = (kind: ConsentRequest["kind"], usd = 0.32): ConsentRequest => ({
  id: kind, project_id: "p", kind, kind_tr: kind, target: "scene:s", label: kind, usd, scene_id: "s",
  scene_code: "S004_SH01", asset_id: null, created_at: 0, resume: null,
});

afterEach(() => { vi.unstubAllGlobals(); });

describe("ücretli ek işlem izni", () => {
  it("toplam tutar ve reddin sonucu türe göre", () => {
    expect(totalUsd([req("lipsync", 0.32), req("image", 0.04)])).toBeCloseTo(0.36);
    expect(denyEffect([req("lipsync")])).toContain("senkronsuz");
    expect(denyEffect([req("image")])).toContain("ücret doğmaz");
  });

  it("karar bekleyeni sonuçlandırır", async () => {
    const waiting = waitForConsent("a");
    settleConsent("a", true);
    await expect(waiting).resolves.toBe(true);
  });

  it("yoklamanın eski yanıtı yeni beklemeyi düşürmez, artık beklemeyen eski istek 'izin yok' olur", async () => {
    const waiting = waitForConsent("b");
    dropStaleWaiters([]);                                         // yeni: korunur
    dropStaleWaiters([], Date.now() + STALE_AFTER_MS + 1);         // eski ve listede yok: bırakılır
    await expect(waiting).resolves.toBe(false);
  });

  it("428 alan istek izin verilince bir kez yinelenir ve oturum başlığını taşır", async () => {
    const calls: { headers: Record<string, string> }[] = [];
    const responses = [
      new Response(JSON.stringify({ detail: "izin", consent: { id: "c" } }), { status: 428 }),
      new Response(JSON.stringify({ ok: true }), { status: 200 }),
    ];
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init: { headers: Record<string, string> }) => {
      calls.push(init);
      return responses.shift()!;
    }));
    const pending = api.post<{ ok: boolean }>("/assets/x/generate");
    await vi.waitFor(() => expect(hasWaiter("c")).toBe(true));
    settleConsent("c", true);
    await expect(pending).resolves.toEqual({ ok: true });
    expect(calls).toHaveLength(2);
    expect(calls[0].headers["X-HG-Session"]).toBeTruthy();
  });

  it("reddedilen 428 isteği yinelenmez ve hata verir", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "izin", consent: { id: "d" } }), { status: 428 })));
    const pending = api.post("/assets/x/generate");
    await vi.waitFor(() => expect(hasWaiter("d")).toBe(true));
    settleConsent("d", false);
    await expect(pending).rejects.toThrow("İzin verilmedi");
  });
});
