import { describe, expect, it } from "vitest";
import { filmToTimeline, layout, rulerStep, speechSeconds, timecode, timelineToFilm } from "@/lib/timeline";
import type { Scene, Sequence, Track, TimelineTrack } from "@/lib/types";

const track = (over: Partial<Track>): Track => ({
  id: "t", kind: "dialogue", source: "tts", text: "Burada kimse yok mu", emotion: "fear", start_offset_s: 0.5, gain_db: 0,
  version: 1, character_id: null, character: "Kerem", mood: null, ...over,
});
const scene = (id: string, code: string, dur: number, seq: string | null, tracks: Track[] = []) =>
  ({ id, scene_code: code, duration_s: dur, sequence_id: seq, tracks, variations: [] } as unknown as Scene);
const seqTrack = (over: Partial<TimelineTrack>): TimelineTrack => ({
  id: "m", sequence_id: "q1", kind: "music", source: "procedural", mood: "tension", filename: null, duration_s: null, url: null,
  start_s: 0, end_s: null, gain_db: -8, fade_in_s: 0, fade_out_s: 0, is_active: true, ...over,
});
const seq = (id: string, code: string, tracks: TimelineTrack[] = []) => ({ id, code, title: code, tracks } as unknown as Sequence);

describe("layout", () => {
  const scenes = [
    scene("a", "S001_SH01", 3, "q1", [track({ id: "d1" })]),
    scene("b", "S001_SH02", 3.5, "q1", [track({ id: "m1", kind: "music", mood: "joy", start_offset_s: 0 })]),
    scene("c", "S002_SH01", 4, "q2", [track({ id: "s1", kind: "sfx", start_offset_s: 1 })]),
  ];
  const l = layout(scenes, [seq("q1", "SEQ01", [seqTrack({ id: "q1m" })]), seq("q2", "SEQ02", [seqTrack({ id: "q2m", start_s: 1, end_s: 2.5 })])]);

  it("lays shots end to end and sequences over their shots", () => {
    expect(l.shots.map((s) => [s.start, s.end])).toEqual([[0, 3], [3, 6.5], [6.5, 10.5]]);
    expect(l.duration).toBe(10.5);
    expect(l.bands.map((b) => [b.code, b.start, b.end])).toEqual([["SEQ01", 0, 6.5], ["SEQ02", 6.5, 10.5]]);
  });

  it("places dialogue at its offset for its spoken length, inside the shot", () => {
    expect(l.dialogue).toHaveLength(1);
    expect(l.dialogue[0].start).toBe(0.5);
    expect(l.dialogue[0].end).toBeCloseTo(0.5 + speechSeconds("Burada kimse yok mu"));
    expect(l.dialogue[0].label).toBe("Kerem");
  });

  it("puts sequence music over the sequence (clamped) and shot music over the shot", () => {
    const byId = Object.fromEntries(l.music.map((m) => [m.id, m]));
    expect([byId.q1m.start, byId.q1m.end]).toEqual([0, 6.5]);
    expect([byId.q2m.start, byId.q2m.end]).toEqual([7.5, 9]);
    expect([byId.m1.start, byId.m1.end, byId.m1.label]).toEqual([3, 6.5, "Çekim müziği · Sevinç"]);
    expect(l.effects.map((e) => [e.label, e.start])).toEqual([["Efekt", 7.5]]);
  });
});

describe("film and timeline mapping", () => {
  const shots = layout([scene("a", "S1", 3, null), scene("b", "S2", 4, null), scene("c", "S3", 2, null)], []).shots;
  const cut = [{ scene_code: "S1", in_s: 0, out_s: 3 }, { scene_code: "S3", in_s: 3, out_s: 5 }];     // S2 henüz filmde yok

  it("maps film time onto the shot it belongs to, skipping shots missing from the film", () => {
    expect(filmToTimeline(1.5, cut, shots)).toBe(1.5);
    expect(filmToTimeline(3.5, cut, shots)).toBe(7.5);
    expect(filmToTimeline(9, cut, shots)).toBe(9);               // sonda son çekimin sonunda kalır
  });

  it("maps a timeline position back to film time, or null when that shot is not in the film", () => {
    expect(timelineToFilm(1, cut, shots)).toBe(1);
    expect(timelineToFilm(4, cut, shots)).toBeNull();
    expect(timelineToFilm(8, cut, shots)).toBe(4);
  });
});

describe("timecode and ruler", () => {
  it("formats 24 fps timecode", () => {
    expect(timecode(0)).toBe("00:00:00:00");
    expect(timecode(1.5)).toBe("00:00:01:12");
    expect(timecode(3725 + 23 / 24)).toBe("01:02:05:23");
    expect(timecode(-2)).toBe("00:00:00:00");
  });
  it("picks a ruler step that keeps labels apart", () => {
    expect(rulerStep(200)).toBe(0.5);
    expect(rulerStep(64)).toBe(1);
    expect(rulerStep(20)).toBe(5);
    expect(rulerStep(0.1)).toBe(600);
  });
});
