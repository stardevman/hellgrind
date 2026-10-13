/* Ses-önce: toplu yüklemede dosya adı eşleştirme, elle atama listesi ve kaynak rozeti (saf fonksiyonlar). */
import { describe, expect, it } from "vitest";
import type { VoiceLine, VoiceState } from "@/lib/types";
import { assignable, matchFiles, parseLineFile, sourceBadge, uploadBody } from "@/lib/voice";

const line = (n: number, over: Partial<VoiceLine> = {}): VoiceLine => ({ track_id: `t${n}`, n, character_id: "c", character: "Kerem", text: `replik ${n}`,
  emotion: null, source: "tts", final: false, engine: "espeak", url: "/u", duration_s: 1.2, start_offset_s: 0.4, slot_s: 1.5, drift: false,
  filename: null, version: 1, ...over });
const shot = (code: string, lines: VoiceLine[]) => ({ scene_id: `s-${code}`, scene_code: code, heading: "", description: null, duration_s: 4,
  duration_source: "script" as const, voice_s: null, voice_clamped: null, has_video: false, status: "pending", drift: false, drift_text: null, lines });
const state = (locked = false): VoiceState => ({
  sequences: [{ id: "q1", code: "SEQ01", title: "Döküm", locked, locked_at: null, lines: 3, final: 0,
    shots: [shot("S001_SH01", [line(1), line(2)]), shot("S001_SH02", [line(3, { track_id: "t3", n: 1 })])] }],
  voice_first: false, counts: { lines: 3, final: 0, temp: 3, unmeasured: 0, drift: 0, unlocked: 1 },
  pads: { in: 0.4, gap: 0.35, out: 0.6, drift: 0.5 }, drift_text: "" });
const file = (name: string) => new File([new Uint8Array([1, 2, 3])], name, { type: "audio/wav" });

describe("voice", () => {
  it("parses S001_SH01_2.wav names and rejects everything else", () => {
    expect(parseLineFile("S001_SH01_2.wav")).toEqual({ code: "S001_SH01", n: 2 });
    expect(parseLineFile("s001_sh01-1.MP3")).toEqual({ code: "S001_SH01", n: 1 });
    for (const bad of ["S001_SH01.wav", "S001_SH01_0.wav", "S1_SH01_1.wav", "notlar.txt"]) expect(parseLineFile(bad)).toBeNull();
  });

  it("matches files to lines and lists the rest with a Turkish reason", () => {
    const { matched, unmatched } = matchFiles([file("S001_SH01_2.wav"), file("S001_SH02_1.wav"), file("S001_SH01_2.wav"),
      file("S009_SH01_1.wav"), file("S001_SH01_5.wav"), file("tema.wav"), file("liste.txt")], state());
    expect(matched.map((m) => `${m.shot.scene_code}:${m.line.n}`)).toEqual(["S001_SH01:2", "S001_SH02:1"]);
    expect(unmatched.map((u) => u.reason)).toEqual(["S001_SH01 2. replik için ikinci dosya", "S009_SH01 çekimi yok",
      "S001_SH01 çekiminde 5. replik yok", "adı S001_SH01_2 biçiminde değil", "ses dosyası değil"]);
  });

  it("a locked sequence takes no files and offers no lines for manual assignment", () => {
    const { matched, unmatched } = matchFiles([file("S001_SH01_1.wav")], state(true));
    expect(matched).toEqual([]);
    expect(unmatched[0].reason).toBe("SEQ01 sekansının sesi kilitli");
    expect(assignable(state(true))).toEqual([]);
    expect(assignable(state()).map((o) => o.label)).toEqual(["S001_SH01 · 1 · Kerem: replik 1", "S001_SH01 · 2 · Kerem: replik 2", "S001_SH02 · 1 · Kerem: replik 3"]);
  });

  it("badges final and temporary voices and keeps the line identity in the upload body", () => {
    expect(sourceBadge(line(1, { final: true, source: "upload", filename: "a.wav" }))).toMatchObject({ text: "Final", tone: "ok" });
    expect(sourceBadge(line(1))).toMatchObject({ text: "Geçici: espeak", tone: "warn" });
    expect(sourceBadge(line(1, { engine: "elevenlabs" })).text).toBe("Geçici: ElevenLabs");
    expect(sourceBadge(line(1, { engine: null, url: null })).text).toBe("Seslendirilmedi");
    expect(uploadBody(line(2), "k", "S001_SH01_2.wav")).toEqual({ kind: "dialogue", source: "upload", file_key: "k", replaces: "t2",
      character_id: "c", text: "replik 2", emotion: null, start_offset_s: 0.4, params: { filename: "S001_SH01_2.wav" } });
  });
});
