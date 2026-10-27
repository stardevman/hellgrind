/* Google Flow köprüsü (backend/app/engine/flow_pack.py): Flow'un API'si yok; klipler Flow'da elle üretilir ve dosya
   adındaki çekim koduyla (S001_SH02) dış çekim olarak yüklenir. Eşleştirme burada, saf fonksiyonlarla. */

export type FlowQuality = "lite" | "fast" | "quality";
export const FLOW_QUALITY: Record<FlowQuality, { label: string; credits: number }> = {
  lite: { label: "Veo 3.1 Lite", credits: 10 },
  fast: { label: "Veo 3.1 Fast", credits: 20 },
  quality: { label: "Veo 3.1 Quality", credits: 100 },
};

const CODE = /S\d{3,}_SH\d{2,}/i;

export function codeOf(name: string): string | null {
  const m = CODE.exec(name);
  return m ? m[0].toUpperCase() : null;
}

export interface FlowMatch<F> { matched: { file: F; code: string; sceneId: string }[]; unknown: string[]; duplicate: string[] }

/** Dosyaları çekimlere eşler. Kodu olmayan ya da projede bulunmayan dosyalar `unknown`; aynı çekime ikinci dosya
    `duplicate` (ilk seçilen yüklenir). */
export function matchClips<F extends { name: string }>(files: F[], scenes: { id: string; scene_code: string }[]): FlowMatch<F> {
  const byCode = new Map(scenes.map((s) => [s.scene_code.toUpperCase(), s.id]));
  const out: FlowMatch<F> = { matched: [], unknown: [], duplicate: [] };
  const seen = new Set<string>();
  for (const file of files) {
    const code = codeOf(file.name);
    const sceneId = code ? byCode.get(code) : undefined;
    if (!code || !sceneId) out.unknown.push(file.name);
    else if (seen.has(code)) out.duplicate.push(file.name);
    else {
      seen.add(code);
      out.matched.push({ file, code, sceneId });
    }
  }
  return out;
}
