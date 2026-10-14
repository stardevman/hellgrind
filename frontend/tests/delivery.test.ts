/* Teslim: hazır ayarlar, şartname özeti, QC sorunlarının Kurgu'daki yeri, Bitirme adımının eksikleri. */
import { describe, expect, it } from "vitest";
import { applyPreset, matchesPreset, qcMarks, specLine } from "@/lib/delivery";
import { deliveryTodos } from "@/lib/steps";
import type { DeliverySpec, Overview, Scene } from "@/lib/types";

const PRESETS = {
  tv_tr: { label: "TV (Türkiye, 25 fps)", resolution: "1920x1080", fps: "25", codec: "prores_hq", loudness_lufs: -23, srt: true, me_mix: true, textless: true },
  web: { label: "Web", resolution: "1920x1080", fps: "24", codec: "h264", loudness_lufs: -14, srt: true, me_mix: false, textless: false },
};
const BASE: DeliverySpec = { resolution: "1280x720", fps: "24", codec: "h264", loudness_lufs: -23, srt: false, me_mix: false, textless: false, preset: null };

describe("delivery", () => {
  it("applies a preset and recognises it until the form is changed by hand", () => {
    const tv = applyPreset({ presets: PRESETS }, "tv_tr", BASE);
    expect(tv).toMatchObject({ resolution: "1920x1080", fps: "25", codec: "prores_hq", preset: "tv_tr" });
    expect(matchesPreset({ presets: PRESETS }, tv)).toBe("tv_tr");
    expect(matchesPreset({ presets: PRESETS }, { ...tv, loudness_lufs: -24 })).toBeNull();
    expect(specLine(tv)).toBe("1920x1080 · 25 fps · ProRes 422 HQ · -23 LUFS · SRT, M&E, yazısız");
    expect(specLine({ ...BASE, fps: "23.976" })).toBe("1280x720 · 23,976 fps · H.264 · -23 LUFS");
  });

  it("puts QC issues on the edit timeline through the final cut, unless the report is for an older film", () => {
    const cut = [{ scene_code: "A", in_s: 0, out_s: 2 }, { scene_code: "B", in_s: 2, out_s: 5 }];
    const shots = [{ scene: { scene_code: "A" } as Scene, start: 0, end: 2.5 }, { scene: { scene_code: "B" } as Scene, start: 2.5, end: 6 }];
    const issue = { check: "black", name: "Siyah kare", start: 3, end: 3.5, tc: "00:00:03:00", scene_code: "B", detail: "0.50 sn siyah" };
    const sum = { spec: true, package: false, rights_missing: [], qc: { passed: false, stale: false, failed: ["Siyah kare"], issues: [issue] } };
    expect(qcMarks(sum, cut, shots).map((m) => m.t)).toEqual([3.5]);
    expect(qcMarks({ ...sum, qc: { ...sum.qc, stale: true } }, cut, shots)).toEqual([]);
  });

  it("finishing lists failed QC checks and missing rights as warnings", () => {
    const ov = { delivery: { spec: true, package: false, rights_missing: ["Müzik lisansı", "Senaryo hak devri"],
      qc: { passed: false, stale: false, failed: ["Loudness", "Çizgi titremesi"], issues: [] } } } as unknown as Overview;
    expect(deliveryTodos(ov)).toEqual([
      { text: "QC kaldı: Loudness, Çizgi titremesi", page: "deliver", tone: "warn" },
      { text: "2 hak zinciri belgesi eksik", page: "deliver", tone: "warn" }]);
    expect(deliveryTodos({ delivery: { spec: false, package: false, rights_missing: [], qc: { passed: true, stale: true, failed: [], issues: [] } } } as unknown as Overview))
      .toEqual([{ text: "QC raporu eski filme ait", page: "deliver", tone: "warn" }]);
  });
});
