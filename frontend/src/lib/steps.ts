/* Üretim adımları: sayfa çubuğundaki dokuz sayfa, üretim sırasını izleyen dört adım altında toplanır. `stepReadiness`
   her adım için durumu ve bir sonraki adıma geçmek için eksik olanları çıkarır; saf fonksiyondur (tarayıcı gerekmez). */
import { GATE_PAGE, GATE_STEP, GATE_TR, type Gate } from "./client";
import type { Overview } from "./types";

export type StepId = "prep" | "approve" | "produce" | "finish";
export type StepPage = "script" | "voice" | "style" | "assets" | "board" | "edit" | "compare" | "color" | "deliver";
export interface Step { id: StepId; label: string; pages: readonly StepPage[]; hint: string }

export const STEPS: readonly Step[] = [
  { id: "prep", label: "Hazırlık", pages: ["script", "voice", "style", "assets"], hint: "Senaryo işlenir, diyalog kaydedilir, stil kılavuzu seçilir, karakter, mekân ve nesne referansları kilitlenir." },
  { id: "approve", label: "Onay", pages: ["board"], hint: "Her çekimin storyboard karesi onaylanır; onaysız çekim üretime girmez." },
  { id: "produce", label: "Üretim", pages: ["edit", "compare"], hint: "Çekimler üretilir, adaylar karşılaştırılır ve kazanan seçilir." },
  { id: "finish", label: "Bitirme", pages: ["color", "deliver"], hint: "Renk sıçramaları giderilir, film birleştirilir ve dışa aktarılır." },
];

export const stepOf = (page: string): Step => STEPS.find((s) => (s.pages as readonly string[]).includes(page)) ?? STEPS[0];

export type StepState = "todo" | "current" | "done" | "blocked";
/** Eksik bir iş: Türkçe metin ve onu çözen sayfa. */
export interface Todo { text: string; page: StepPage; tone?: "bad" | "warn" }
export interface StepStatus { id: StepId; state: StepState; todos: Todo[] }

const n = (k: number, one: string, many: string) => `${k} ${k === 1 ? one : many}`;

/** Adımların durumu: ilk tamamlanmamış adım `current`, öncekiler `done`, sonrakiler `todo`; engelli çekim ya da donma
    adımı `blocked` yapar. Proje yoksa yalnızca hazırlık adımı açıktır. */
export function stepReadiness(ov: Overview | undefined): StepStatus[] {
  const todos: Record<StepId, Todo[]> = { prep: [], approve: [], produce: [], finish: [] };
  const scenes = ov?.scenes ?? [];
  const p = ov?.project;

  if (!ov || !scenes.length) todos.prep.push({ text: ov ? "Senaryo henüz işlenmedi" : "Proje seçilmedi ya da oluşturulmadı", page: "script" });
  else {
    const c = p!.asset_counts ?? {};
    const total = Object.values(c).reduce((x, y) => x + y, 0);
    const locked = c.locked || 0;
    const strict = p!.settings.lint_strictness !== "off";
    if (strict && total && locked < total) todos.prep.push({ text: n(total - locked, "varlık kilitsiz", "varlık kilitsiz"), page: "assets", tone: scenes.some((s) => s.status === "blocked") ? "bad" : "warn" });
    if (!p!.style_version) todos.prep.push({ text: "Stil kılavuzu etkinleştirilmedi", page: "style", tone: "warn" });
    todos.prep.push(...voiceTodos(ov!));
    // Hazırlıkta engelleyiciler önce: "Sıradaki" uyarıdan (stil, final ses) önce kilitsiz sekansı gösterir.
    const rank = (t: Todo) => (t.tone === "bad" ? 0 : t.tone === "warn" ? 2 : 1);
    todos.prep.sort((a, b) => rank(a) - rank(b));
  }

  if (scenes.length) {
    if (p!.settings.require_board_approval) {
      const waiting = scenes.filter((s) => s.board_status !== "approved").length;
      if (waiting) todos.approve.push({ text: n(waiting, "kare onay bekliyor", "kare onay bekliyor"), page: "board", tone: "warn" });
    }
    const blocked = scenes.filter((s) => s.status === "blocked").length;
    const failed = scenes.filter((s) => s.status === "failed" || s.status === "needs_review").length;
    const planReview = scenes.filter((s) => s.status === "plan_review").length;
    const notDone = scenes.filter((s) => s.status !== "done").length;
    if (blocked) todos.produce.push({ text: n(blocked, "çekim engelli", "çekim engelli"), page: "assets", tone: "bad" });
    if (planReview) todos.produce.push({ text: n(planReview, "plan onay bekliyor", "plan onay bekliyor"), page: "edit", tone: "warn" });
    if (failed) todos.produce.push({ text: n(failed, "çekim inceleme bekliyor", "çekim inceleme bekliyor"), page: "edit", tone: "bad" });
    const stuck = scenes.filter((s) => ["pending", "parsed", "storyboard", "frozen"].includes(s.status) && s.final_url).length;
    if (stuck) todos.produce.push({ text: n(stuck, "çekim klibi hazır, tamamlanmayı bekliyor", "çekim klibi hazır, tamamlanmayı bekliyor"), page: "edit", tone: "warn" });
    const rest = notDone - blocked - failed - planReview - stuck;
    if (rest > 0) todos.produce.push({ text: n(rest, "çekim henüz bitmedi", "çekim henüz bitmedi"), page: "edit" });
    const jumps = p!.continuity?.pairs.filter((x) => x.jump).length ?? 0;
    if (jumps) todos.finish.push({ text: n(jumps, "renk sıçraması", "renk sıçraması"), page: "color", tone: "warn" });
    if (!ov!.timeline) todos.finish.push({ text: "Film henüz birleştirilmedi", page: "deliver" });
    else todos.finish.push(...deliveryTodos(ov!));
  }

  for (const t of clientTodos(ov)) todos[t.step].push(t.todo);

  let currentFound = false;
  return STEPS.map((s) => {
    const list = todos[s.id];
    // Hazırlıkta stil ve kilit uyarıları engelleyici değildir (warn): sahneler varsa adım tamamlanmış sayılır.
    const hard = s.id === "prep" ? list.filter((t) => t.tone !== "warn") : list;
    let state: StepState;
    if (!scenes.length && s.id !== "prep") state = "todo";
    else if (hard.length === 0 && !currentFound) state = "done";
    else if (!currentFound) { state = hard.some((t) => t.tone === "bad") || Boolean(p?.frozen && s.id === "produce") ? "blocked" : "current"; currentFound = true; }
    else state = "todo";
    return { id: s.id, state, todos: list };
  });
}

/** Teslim: QC'de kalan kontroller (uyarı; rapor eski filme aitse yeniden çalıştırma önerisi) ve eksik hak zinciri
    belgeleri (uyarı: paket yine de oluşturulabilir). Film birleşmeden gösterilmez. */
export function deliveryTodos(ov: Overview): Todo[] {
  const d = ov.delivery;
  if (!d) return [];
  const out: Todo[] = [];
  if (d.qc?.stale) out.push({ text: "QC raporu eski filme ait", page: "deliver", tone: "warn" });
  else if (d.qc && !d.qc.passed) out.push({ text: `QC kaldı: ${d.qc.failed.join(", ")}`, page: "deliver", tone: "warn" });
  if (d.rights_missing.length) out.push({ text: n(d.rights_missing.length, "hak zinciri belgesi eksik", "hak zinciri belgesi eksik"), page: "deliver", tone: "warn" });
  return out;
}

/** Müşteri onay döngüsü: değişiklik istenen kapı ilgili adımı tamamlanmamış sayar; müşteride bekleyen sürüm ve çözülmemiş
    müşteri notları uyarıdır. Hiç gönderilmemiş kapı eksik sayılmaz. */
export function clientTodos(ov: Overview | undefined): { step: StepId; todo: Todo }[] {
  const out: { step: StepId; todo: Todo }[] = [];
  for (const g of ov?.client?.gates ?? []) {
    const gate = g.gate as Gate;
    if (!GATE_STEP[gate]) continue;
    if (g.status === "changes_requested") out.push({ step: GATE_STEP[gate], todo: { text: `Müşteri değişiklik istedi: ${GATE_TR[gate]}`, page: GATE_PAGE[gate], tone: "bad" } });
    else if (g.status === "pending" && g.sent) out.push({ step: GATE_STEP[gate], todo: { text: `Müşteri onayı bekleniyor: ${GATE_TR[gate]}`, page: "deliver", tone: "warn" } });
  }
  const open = (ov?.client?.notes ?? []).filter((x) => !x.resolved).length;
  if (open) out.push({ step: "produce", todo: { text: n(open, "müşteri notu çözülmedi", "müşteri notu çözülmedi"), page: "edit", tone: "warn" } });
  return out;
}

/** Ses-önce (proje ayarı voice_first): final sesi olmayan replikler (uyarı) ve sesi kilitlenmemiş sekanslar (görüntü
    üretimini bekletir; adımı tamamlanmamış sayar). Ayar kapalıyken eksik yoktur. */
export function voiceTodos(ov: Overview): Todo[] {
  if (!ov.project.settings.voice_first) return [];
  const out: Todo[] = [];
  const lines = ov.scenes.flatMap((s) => (s.tracks ?? []).filter((t) => t.kind === "dialogue"));
  const waiting = lines.filter((t) => t.source !== "upload").length;
  const unlocked = (ov.sequences ?? []).filter((q) => !q.voice_locked_at).length;
  if (unlocked) out.push({ text: n(unlocked, "sekansın sesi kilitlenmedi", "sekansın sesi kilitlenmedi"), page: "voice" });
  if (waiting) out.push({ text: n(waiting, "replik final sesi bekliyor", "replik final sesi bekliyor"), page: "voice", tone: "warn" });
  return out;
}

/** Sıradaki iş: ilk tamamlanmamış adımın ilk eksiği (çubuğun sağındaki "Sıradaki" bağlantısı). */
export function nextTodo(status: StepStatus[]): { step: Step; todo: Todo } | null {
  for (const st of status) {
    if (st.state === "done") continue;
    const todo = st.todos[0];
    if (todo) return { step: STEPS.find((s) => s.id === st.id)!, todo };
  }
  return null;
}

export interface PageBadge { text: string; tone?: "ok" | "bad" | ""; title: string }
/** Sayfa düğmelerindeki rozetler: kilitli varlık, onaylı kare, hazır ya da engelli çekim, renk sıçraması, film. */
export function pageBadges(ov: Overview | undefined): Partial<Record<StepPage, PageBadge>> {
  const badge: Partial<Record<StepPage, PageBadge>> = {};
  if (!ov) return badge;
  const scenes = ov.scenes;
  const c = ov.project.asset_counts ?? {};
  const assetsTotal = Object.values(c).reduce((x, y) => x + y, 0);
  const blocked = scenes.filter((s) => s.status === "blocked").length;
  const jumps = ov.project.continuity?.pairs.filter((x) => x.jump).length ?? 0;
  const lines = ov.scenes.flatMap((s) => (s.tracks ?? []).filter((t) => t.kind === "dialogue"));
  if (lines.length) {
    const finals = lines.filter((t) => t.source === "upload").length;
    const locked = ov.sequences.filter((q) => q.voice_locked_at).length;
    badge.voice = { text: `${finals}/${lines.length}`, tone: finals === lines.length ? "ok" : "",
      title: `Final sesli replikler${ov.sequences.length ? ` · ${locked}/${ov.sequences.length} sekansın sesi kilitli` : ""}` };
  }
  if (ov.project.style_version) {
    const medium = ov.project.style_medium === "3d" ? "3D" : ov.project.style_medium === "2d" ? "2D" : "";
    badge.style = { text: `s${ov.project.style_version}${medium ? ` · ${medium}` : ""}`, title: `Etkin stil sürümü ${ov.project.style_version}${medium ? `, çizim türü ${medium}` : ""}` };
  }
  if (assetsTotal) badge.assets = { text: `${c.locked || 0}/${assetsTotal}`, tone: c.locked === assetsTotal ? "ok" : blocked ? "bad" : "", title: "Kilitli varlıklar" };
  if (scenes.length) {
    const approved = scenes.filter((s) => s.board_status === "approved").length;
    badge.board = { text: `${approved}/${scenes.length}`, tone: approved === scenes.length ? "ok" : "", title: "Onaylı storyboard kareleri" };
    const done = scenes.filter((s) => s.status === "done").length;
    badge.edit = blocked ? { text: String(blocked), tone: "bad", title: `${blocked} çekim engelli` }
      : { text: `${done}/${scenes.length}`, tone: done === scenes.length ? "ok" : "", title: "Hazır çekimler" };
  }
  if (jumps) badge.color = { text: String(jumps), tone: "bad", title: `${jumps} renk sıçraması` };
  if (ov.timeline) badge.deliver = { text: "film", tone: "ok", title: "Birleştirilmiş film hazır" };
  return badge;
}
