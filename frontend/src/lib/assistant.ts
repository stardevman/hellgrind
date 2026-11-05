/* Panel asistanı (Claude) için türler ve metin yardımcıları. Sohbet arka uçta yazılır; panel oturumu yoklar. */

export interface AssistantAction {
  id: string;
  tool: string;
  title: string;
  summary: string;
  estimate_usd: number | null;
  status: "pending" | "approved" | "declined" | "done" | "failed";
  result: { ok?: boolean; stages?: string[]; error?: string; by?: string } | null;
  scene_id: string | null;
  virtual: boolean;
}

export type AssistantItem =
  | { id: number; role: "user"; text: string; at: number }
  | { id: number; role: "assistant"; text: string; at: number; offline?: boolean }
  | { id: number; role: "tool"; text: string; error?: string | null; at: number }
  | { id: number; role: "action"; action: AssistantAction; at: number };

export interface AssistantSessionView {
  id: string;
  title: string | null;
  busy: boolean;
  error: string | null;
  allow_all: boolean;
  spent_usd: number;
  items: AssistantItem[];
}

export interface AssistantState {
  available: boolean;
  reason: string | null;
  model: string;
  sessions: { id: string; title: string; busy: boolean; updated_at: number | null }[];
}

const usd = (x: number) => x.toLocaleString("tr-TR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** Kartın bedel satırı: "Tahmini 0,42 $" (ücretsiz modda sanal). */
export function costLabel(a: Pick<AssistantAction, "estimate_usd" | "virtual">): string {
  if (a.estimate_usd == null) return "Yalnızca ses ve montaj; ücretli modda dudak senkronu gerekirse ayrıca sorulur";
  return `Tahmini ${usd(a.estimate_usd)} $${a.virtual ? " (ücretsiz mod: sanal)" : ""}`;
}

/** Kararı verilmiş kartın durum satırı. */
export function actionOutcome(a: AssistantAction): string | null {
  switch (a.status) {
    case "pending": return null;
    case "approved": return "Başlatılıyor…";
    case "declined": return "Vazgeçildi; hiçbir şey üretilmedi.";
    case "done": {
      const stages = a.result?.stages?.length ? ` (${a.result.stages.join(", ")})` : "";
      return `${a.result?.by === "session" ? "Oturum izniyle başlatıldı" : "Onaylandı, başlatıldı"}${stages}.`;
    }
    case "failed": return `Başlatılamadı: ${a.result?.error || "bilinmeyen hata"}`;
  }
}

/** Sohbet listesindeki son kullanım: "az önce", "5 dk önce", "2 sa önce", tarih. */
export function ago(at: number | null, now = Date.now() / 1000): string {
  if (!at) return "";
  const s = Math.max(0, now - at);
  if (s < 60) return "az önce";
  if (s < 3600) return `${Math.floor(s / 60)} dk önce`;
  if (s < 86400) return `${Math.floor(s / 3600)} sa önce`;
  return new Date(at * 1000).toLocaleDateString("tr-TR", { day: "numeric", month: "short" });
}

export const SESSION_KEY = (projectId: string) => `hg-assistant:${projectId}`;
