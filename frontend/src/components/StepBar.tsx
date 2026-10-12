"use client";
/* Alt çubuk: sekiz sayfa, üretim sırasını izleyen dört adım altında gruplanır (Hazırlık, Onay, Üretim, Bitirme). Her
   adımın başlığında durum noktası; sağda "Sıradaki" bağlantısı ilk eksik işe götürür. Shift+1…8 sayfalar arasında geçer. */
import { useMemo } from "react";
import { NEEDS_SHOTS, PAGES, usePanel, type Page } from "@/lib/panel";
import { nextTodo, STEPS, stepReadiness, type StepStatus } from "@/lib/steps";
import { Icon } from "./icons";

export const PAGE_LABEL: Record<Page, string> = {
  script: "Senaryo", style: "Stil", assets: "Varlıklar", board: "Storyboard", edit: "Kurgu", compare: "Karşılaştır",
  color: "Renk", deliver: "Teslim", settings: "Ayarlar", history: "Geçmiş",
};
/** Basit görünümde gizlenen sayfalar (Karşılaştır, Kurgu'daki "Adayları karşılaştır" ile yine açılır). */
const ADVANCED_PAGES: ReadonlySet<Page> = new Set(["compare"]);
const STATE_TR = { todo: "sırada", current: "bu adımdasınız", done: "tamamlandı", blocked: "engelli" } as const;

function stepTitle(st: StepStatus, hint: string): string {
  const head = `${STEPS.find((s) => s.id === st.id)!.label}: ${STATE_TR[st.state]}`;
  return st.todos.length ? `${head}\n${st.todos.map((t) => `• ${t.text}`).join("\n")}` : `${head}\n${hint}`;
}

export function StepBar({ hasShots }: { hasShots: boolean }) {
  const { page, setPage, ov, advanced } = usePanel();
  const status = useMemo(() => stepReadiness(ov), [ov]);
  const next = nextTodo(status);
  const scenes = ov?.scenes ?? [];
  const c = ov?.project.asset_counts ?? {};
  const assetsTotal = Object.values(c).reduce((x, y) => x + y, 0);
  const blocked = scenes.filter((s) => s.status === "blocked").length;
  const jumps = ov?.project.continuity?.pairs.filter((x) => x.jump).length ?? 0;
  const badge: Partial<Record<Page, { text: string; tone?: string; title: string }>> = {};
  if (ov?.project.style_version) badge.style = { text: `s${ov.project.style_version}`, title: `Etkin stil sürümü ${ov.project.style_version}` };
  if (assetsTotal) badge.assets = { text: `${c.locked || 0}/${assetsTotal}`, tone: c.locked === assetsTotal ? "ok" : blocked ? "bad" : "", title: "Kilitli varlıklar" };
  if (scenes.length) {
    const approved = scenes.filter((s) => s.board_status === "approved").length;
    badge.board = { text: `${approved}/${scenes.length}`, tone: approved === scenes.length ? "ok" : "", title: "Onaylı storyboard kareleri" };
    const done = scenes.filter((s) => s.status === "done").length;
    badge.edit = blocked ? { text: String(blocked), tone: "bad", title: `${blocked} çekim engelli` }
      : { text: `${done}/${scenes.length}`, tone: done === scenes.length ? "ok" : "", title: "Hazır çekimler" };
  }
  if (jumps) badge.color = { text: String(jumps), tone: "bad", title: `${jumps} renk sıçraması` };
  if (ov?.timeline) badge.deliver = { text: "film", tone: "ok", title: "Birleştirilmiş film hazır" };

  return (
    <nav className="stepbar" aria-label="Üretim adımları" data-tour="steps">
      <ol className="stp-list">
        {STEPS.map((s, i) => {
          const st = status[i];
          const active = (s.pages as readonly string[]).includes(page);
          return (
            <li key={s.id} className={`stp ${st.state}${active ? " on" : ""}`} title={stepTitle(st, s.hint)}>
              <div className="stp-h">
                <span className="stp-n">{i + 1}</span>
                <span className="stp-l">{s.label}</span>
                <span className={`stp-dot ${st.state}`} aria-label={STATE_TR[st.state]} />
              </div>
              <div className="stp-pages">
                {s.pages.map((x) => {
                  if (!advanced && ADVANCED_PAGES.has(x)) return null;
                  const Ico = Icon[x];
                  const b = badge[x];
                  const k = PAGES.indexOf(x) + 1;
                  return (
                    <button key={x} type="button" aria-current={page === x ? "page" : undefined} disabled={NEEDS_SHOTS.has(x) && !hasShots}
                      title={`${PAGE_LABEL[x]} (Shift+${k})`} onClick={() => setPage(x)} data-tour={`page-${x}`}>
                      <Ico />{PAGE_LABEL[x]}
                      {b && <span className={`badge ${b.tone || ""}`} title={b.title}>{b.text}</span>}
                    </button>
                  );
                })}
              </div>
            </li>
          );
        })}
      </ol>
      <div className="nextup" aria-live="polite">
        {next ? (
          <button type="button" className={`nx ${next.todo.tone || ""}`} onClick={() => setPage(next.todo.page)}
            title={`${next.step.label} adımı · ${PAGE_LABEL[next.todo.page]} sayfasına git`}>
            <span className="nx-k">Sıradaki</span><span className="nx-t">{next.todo.text}</span><Icon.chevron />
          </button>
        ) : ov ? <span className="nx done"><Icon.check />Tüm adımlar tamamlandı</span> : null}
      </div>
    </nav>
  );
}
