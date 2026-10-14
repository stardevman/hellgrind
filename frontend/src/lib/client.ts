/* Müşteri onay döngüsü: kapıların Yapılacaklar listesindeki çipleri, revizyon turu metni, notların zaman çizelgesindeki yeri
   (saf fonksiyonlar, tests/client.test.ts). Kapılar: Senaryo, Tasarım, Animatik, Taslak kurgu, Final, Kabul. */
import type { ClientGateSummary, ClientNote, Overview } from "./types";
import type { StepId } from "./steps";

export type Gate = "script" | "design" | "animatic" | "draft" | "final" | "acceptance";
export const GATE_TR: Record<Gate, string> = {
  script: "Senaryo", design: "Tasarım", animatic: "Animatik", draft: "Taslak kurgu", final: "Final", acceptance: "Kabul",
};
export const GATE_STATUS_TR: Record<string, string> = { pending: "bekliyor", approved: "onaylandı", changes_requested: "değişiklik istendi",
  superseded: "yerini yenisi aldı" };
/** Kapının ait olduğu üretim grubu (eksik sayılırken o grubun altına yazılır). */
export const GATE_STEP: Record<Gate, StepId> = {
  script: "prep", design: "prep", animatic: "approve", draft: "produce", final: "finish", acceptance: "finish",
};
/** Değişiklik istenen kapının çözüldüğü sayfa. */
export const GATE_PAGE = { script: "script", design: "assets", animatic: "board", draft: "edit", final: "edit", acceptance: "deliver" } as const;
export const VIDEO_GATES: readonly Gate[] = ["animatic", "draft", "final", "acceptance"];

export interface GateChip { gate: Gate; label: string; status: string; tone: "ok" | "bad" | "warn" | ""; title: string }

/** Bir grubun kapı çipleri: onaylandı yeşil, değişiklik istendi kırmızı, gönderildi ve bekliyor amber, hiç gönderilmedi gri. */
export function gateChips(ov: Overview | undefined, step: StepId): GateChip[] {
  const gates = ov?.client?.gates ?? [];
  return (Object.keys(GATE_STEP) as Gate[]).filter((g) => GATE_STEP[g] === step).map((g) => {
    const row = gates.find((x) => x.gate === g);
    const status = row?.status ?? "pending";
    const sent = Boolean(row?.sent);
    const tone = status === "approved" ? "ok" : status === "changes_requested" ? "bad" : sent ? "warn" : "";
    const rounds = row?.rounds ? ` · ${roundText(row)}` : "";
    return { gate: g, label: GATE_TR[g], status, tone,
      title: `Müşteri onayı · ${GATE_TR[g]}: ${status === "pending" && sent ? "müşteride, karar bekleniyor" : GATE_STATUS_TR[status]}${rounds}` };
  });
}

/** "Revizyon 1/2"; sınır aşıldıysa "Revizyon 3/2 (sınır aşıldı)". */
export const roundText = (g: Pick<ClientGateSummary, "rounds" | "limit">) =>
  `Revizyon ${g.rounds}/${g.limit}${g.rounds > g.limit ? " (sınır aşıldı)" : ""}`;

/** Notun Kurgu zaman çizelgesindeki yeri (sn): bağlı çekimin başlangıcı + çekim içindeki ofset (çekim süresiyle sınırlı). */
export function noteAt(note: Pick<ClientNote, "scene_id" | "shot_offset_s">, shots: { scene: { id: string }; start: number; end: number }[]): number | null {
  const shot = shots.find((x) => x.scene.id === note.scene_id);
  if (!shot) return null;
  return Math.min(shot.end - 0.05, shot.start + Math.max(0, Number(note.shot_offset_s) || 0));
}
