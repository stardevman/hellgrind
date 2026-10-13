/* Kurgu sayfasının zaman çizelgesi: çekimlerin, sekansların ve ses izlerinin film süresi üzerindeki yeri; film
   oynatıcısının zamanıyla zaman çizelgesi arasında eşleme; zaman kodu. Saf fonksiyonlar (tests/timeline.test.ts). */
import { EMOTION_TR } from "./i18n";
import type { CutClip, Scene, Sequence } from "./types";

export const FPS = 24;

export interface ShotClip { scene: Scene; start: number; end: number }
export interface Band { id: string; code: string; title: string; start: number; end: number }
export interface AudioClip { id: string; label: string; start: number; end: number; sceneId?: string; title: string }
export interface Layout {
  shots: ShotClip[]; bands: Band[]; dialogue: AudioClip[]; music: AudioClip[]; effects: AudioClip[]; duration: number;
}

/** Konuşma süresi tahmini (Hermes'in kuralıyla aynı hız: saniyede 2,5 kelime). */
export const speechSeconds = (text: string | null) => Math.max(0.8, (text || "").trim().split(/\s+/).filter(Boolean).length / 2.5 + 0.3);

const moodLabel = (mood: string | null) => (mood ? EMOTION_TR[mood] || mood : "");

/** Çekimler sırayla, süreleri kadar yan yana dizilir; sekans bantları ve ses izleri bu konumlara göre yerleşir. */
export function layout(scenes: Scene[], sequences: Sequence[]): Layout {
  let t = 0;
  const shots = scenes.map((scene) => {
    const start = t;
    t += Math.max(0.1, Number(scene.duration_s) || 0);
    return { scene, start, end: t };
  });
  const bands: Band[] = [];
  for (const q of sequences) {
    const mine = shots.filter((s) => s.scene.sequence_id === q.id);
    if (mine.length) bands.push({ id: q.id, code: q.code, title: q.title, start: mine[0].start, end: mine[mine.length - 1].end });
  }
  const dialogue: AudioClip[] = [], music: AudioClip[] = [], effects: AudioClip[] = [];
  for (const s of shots) {
    for (const tr of s.scene.tracks) {
      const at = Math.min(s.end, s.start + Number(tr.start_offset_s || 0));
      if (tr.kind === "dialogue") {
        const who = tr.character || "Anlatıcı";
        dialogue.push({ id: tr.id, sceneId: s.scene.id, label: who, title: `${who}: ${tr.text ?? ""}`,
                        start: at, end: Math.min(s.end, at + (Number(tr.duration_s) || speechSeconds(tr.text))) });   // ölçülen ses varsa o
      } else if (tr.kind === "music") {
        const label = `Çekim müziği${tr.mood ? " · " + moodLabel(tr.mood) : ""}`;
        music.push({ id: tr.id, sceneId: s.scene.id, label, title: `${s.scene.scene_code}: ${label}`, start: s.start, end: s.end });
      } else {
        const label = tr.kind === "sfx" ? "Efekt" : "Ortam";
        effects.push({ id: tr.id, sceneId: s.scene.id, label, title: `${s.scene.scene_code}: ${label}`, start: at, end: s.end });
      }
    }
  }
  for (const b of bands) {
    const q = sequences.find((x) => x.id === b.id)!;
    for (const tr of q.tracks) {
      const start = Math.min(b.end, b.start + Number(tr.start_s || 0));
      const end = tr.end_s != null ? Math.min(b.end, b.start + Number(tr.end_s)) : b.end;
      if (end <= start) continue;
      const label = tr.source === "procedural" ? `Müzik · ${moodLabel(tr.mood)}` : `${tr.kind === "music" ? "Stem" : tr.kind} · ${tr.filename ?? ""}`;
      const clip = { id: tr.id, label, title: `${b.code}: ${label} (${Number(tr.gain_db).toFixed(0)} dB)`, start, end };
      (tr.kind === "music" ? music : effects).push(clip);
    }
  }
  return { shots, bands, dialogue, music, effects, duration: t };
}

/** Birleştirilmiş filmin t saniyesi, zaman çizelgesinde nereye düşer (filmde olmayan çekimler atlanmıştır). */
export function filmToTimeline(t: number, cut: CutClip[], shots: ShotClip[]): number | null {
  const c = cut.find((x) => t >= x.in_s && t < x.out_s) ?? (cut.length && t >= cut[cut.length - 1].out_s ? cut[cut.length - 1] : undefined);
  const s = c && shots.find((x) => x.scene.scene_code === c.scene_code);
  if (!c || !s) return null;
  return Math.min(s.end, s.start + Math.max(0, t - c.in_s));
}

/** Zaman çizelgesindeki x saniyesi, filmde hangi ana karşılık gelir; o çekim filmde yoksa null. */
export function timelineToFilm(x: number, cut: CutClip[], shots: ShotClip[]): number | null {
  const s = shots.find((y) => x >= y.start && x < y.end) ?? (shots.length && x >= shots[shots.length - 1].end ? shots[shots.length - 1] : undefined);
  const c = s && cut.find((y) => y.scene_code === s.scene.scene_code);
  if (!s || !c) return null;
  return Math.min(c.out_s - 0.001, c.in_s + Math.max(0, x - s.start));
}

/** Zaman kodu: SS:DD:SN:KR (24 fps). */
export function timecode(seconds: number, fps = FPS): string {
  const total = Math.max(0, Math.round((Number(seconds) || 0) * fps));
  const f = total % fps, s = Math.floor(total / fps) % 60, m = Math.floor(total / fps / 60) % 60, h = Math.floor(total / fps / 3600);
  return [h, m, s, f].map((n) => String(n).padStart(2, "0")).join(":");
}

/** Cetvel aralığı: ekranda en az `minPx` piksel aralık bırakan en küçük adım (saniye). */
export function rulerStep(pxPerSecond: number, minPx = 64): number {
  for (const step of [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300]) if (step * pxPerSecond >= minPx) return step;
  return 600;
}
