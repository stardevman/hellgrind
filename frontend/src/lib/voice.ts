/* Ses-önce akışı: replik listesi, toplu yüklemede dosya adı eşleştirme ve süre denetimi (saf fonksiyonlar,
   tests/voice.test.ts). Diyalog animasyondan önce kaydedilir; çekim süresi ve tempo sesten çıkar. */
import type { VoiceLine, VoiceShot, VoiceState } from "./types";

/** Ölçülen replik süresi ile çekimdeki yeri arasındaki fark bunu aşarsa amber uyarı. */
export const DRIFT_S = 0.5;
export const DRIFT_TEXT = "Replik süresi çekimden belirgin farklı; görüntünün yeniden üretilmesi gerekebilir.";
export const AUDIO_EXT = [".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"];

/** "S001_SH01_2.wav" -> { code: "S001_SH01", n: 2 }; tanınmazsa null. Büyük/küçük harf ve - ayırıcı kabul edilir. */
export function parseLineFile(name: string): { code: string; n: number } | null {
  const m = /^(s\d{3}_sh\d{2})[_-](\d{1,2})\.(wav|mp3|m4a|aac|flac|ogg)$/i.exec(name.trim());
  if (!m) return null;
  const n = Number(m[2]);
  return n >= 1 ? { code: m[1].toUpperCase(), n } : null;
}

export interface Matched { file: File; line: VoiceLine; shot: VoiceShot }
export interface Unmatched { file: File; reason: string }

/** Dosyaları repliklere eşler: adı biçime uyan ve kilitsiz sekanstaki repliği bulan dosya eşlenir; aynı repliğe iki dosya
    gelirse ikincisi eşlenmeyenlere düşer. */
export function matchFiles(files: File[], state: VoiceState): { matched: Matched[]; unmatched: Unmatched[] } {
  const matched: Matched[] = [], unmatched: Unmatched[] = [];
  const taken = new Set<string>();
  for (const file of files) {
    const p = parseLineFile(file.name);
    if (!p) { unmatched.push({ file, reason: AUDIO_EXT.some((e) => file.name.toLowerCase().endsWith(e)) ? "adı S001_SH01_2 biçiminde değil" : "ses dosyası değil" }); continue; }
    const seq = state.sequences.find((q) => q.shots.some((s) => s.scene_code === p.code));
    const shot = seq?.shots.find((s) => s.scene_code === p.code);
    if (!seq || !shot) { unmatched.push({ file, reason: `${p.code} çekimi yok` }); continue; }
    const line = shot.lines.find((l) => l.n === p.n);
    if (!line) { unmatched.push({ file, reason: `${p.code} çekiminde ${p.n}. replik yok` }); continue; }
    if (seq.locked) { unmatched.push({ file, reason: `${seq.code} sekansının sesi kilitli` }); continue; }
    if (taken.has(line.track_id)) { unmatched.push({ file, reason: `${p.code} ${p.n}. replik için ikinci dosya` }); continue; }
    taken.add(line.track_id);
    matched.push({ file, line, shot });
  }
  return { matched, unmatched };
}

/** Saniye, bir ondalık, Türkçe: "2,4 sn". */
export const secTr = (x: number | null | undefined) => x == null ? "—" : `${Number(x).toFixed(1).replace(".", ",")} sn`;

/** Kaynak rozeti: final (yüklendi) ya da geçici (Okur / espeak / ElevenLabs / ton; henüz sentezlenmediyse "seslendirilmedi"). */
export function sourceBadge(line: VoiceLine): { text: string; tone: "ok" | "warn" | ""; title: string } {
  if (line.final) return { text: "Final", tone: "ok", title: `Yüklendi${line.filename ? `: ${line.filename}` : ""}` };
  const engine = ({ okur: "Okur", elevenlabs: "ElevenLabs", espeak: "espeak", tone: "ton" } as Record<string, string>)[line.engine ?? ""] ?? null;
  return engine ? { text: `Geçici: ${engine}`, tone: "warn", title: "Sentezlenmiş geçici ses; final kayıt yüklenince yerini alır" }
    : { text: "Seslendirilmedi", tone: "", title: "Geçici ses henüz üretilmedi" };
}

/** Yüzde, Türkçe: 0.083 -> "%8". */
const pctTr = (x: number) => `%${Math.round(x * 100)}`;

/** Ses denetimi rozeti (Duyu-small yazıya döktü, metinle karşılaştırdı); denetlenmediyse null. */
export function checkBadge(line: VoiceLine): { text: string; tone: "ok" | "warn"; title: string } | null {
  if (!line.check) return null;
  const heard = `Duyulan: “${line.check.heard || "—"}” (harf hatası ${pctTr(line.check.cer)})`;
  return line.check.ok ? { text: "Uyuşuyor", tone: "ok", title: `Ses replikle uyuşuyor. ${heard}` }
    : { text: "Uyuşmuyor", tone: "warn", title: `Ses replik metniyle uyuşmuyor. ${heard}` };
}

/** Uyuşmayan replik için uyarı satırı. */
export const mismatchText = (line: VoiceLine) => line.check && !line.check.ok
  ? `${line.final ? "Kayıt" : "Ses"} replikle uyuşmuyor; duyulan: “${line.check.heard || "(konuşma duyulmadı)"}”. Yanlış repliğe yüklenmiş ya da metinden sapmış olabilir.`
  : null;

/** Elle atama listesi: kilitsiz sekanslardaki replikler, "S001_SH01 · 2 · Kerem: …" etiketiyle. */
export function assignable(state: VoiceState): { value: string; label: string; line: VoiceLine; shot: VoiceShot }[] {
  return state.sequences.filter((q) => !q.locked).flatMap((q) => q.shots.flatMap((shot) => shot.lines.map((line) => ({
    value: line.track_id, line, shot,
    label: `${shot.scene_code} · ${line.n} · ${line.character || "Anlatıcı"}: ${(line.text || "").slice(0, 48)}${(line.text || "").length > 48 ? "…" : ""}`,
  }))));
}

/** PUT /scenes/{id}/audio gövdesi: final kayıt repliğin yerine geçer (karakter, metin ve ofset aynen kalır). */
export const uploadBody = (line: VoiceLine, key: string, filename: string) => ({
  kind: "dialogue", source: "upload", file_key: key, replaces: line.track_id, character_id: line.character_id,
  text: line.text, emotion: line.emotion, start_offset_s: line.start_offset_s, params: { filename },
});
