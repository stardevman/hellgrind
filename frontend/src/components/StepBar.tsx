"use client";
/* Alt ray: yalnızca dört üretim adımı (numara, ad, durum noktası) ve sağda "Sıradaki" bağlantısı; 30 px. Adımın sayfaları
   ve eksik işleri çalışma alanının üstündeki şeritte (StepHeader) durur. Shift+1…8 sayfalar arasında geçer. */
import { useMemo } from "react";
import { isExtraPage, NEEDS_SHOTS, PAGES, usePanel, type Page } from "@/lib/panel";
import { nextTodo, pageBadges, STEPS, stepOf, stepReadiness, type StepStatus } from "@/lib/steps";
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
  const { page, setPage, ov } = usePanel();
  const status = useMemo(() => stepReadiness(ov), [ov]);
  const next = nextTodo(status);
  const current = isExtraPage(page) ? null : stepOf(page).id;
  return (
    <nav className="stepbar" aria-label="Üretim adımları" data-tour="steps">
      <ol className="stp-list">
        {STEPS.map((s, i) => {
          const st = status[i];
          const first = s.pages.find((x) => !NEEDS_SHOTS.has(x) || hasShots) ?? s.pages[0];
          return (
            <li key={s.id} className={`stp ${st.state}${current === s.id ? " on" : ""}`}>
              <button type="button" aria-current={current === s.id ? "step" : undefined} disabled={NEEDS_SHOTS.has(first) && !hasShots}
                title={stepTitle(st, s.hint)} onClick={() => setPage(first)}>
                <span className="stp-n">{i + 1}</span><span className="stp-l">{s.label}</span><span className={`stp-dot ${st.state}`} aria-label={STATE_TR[st.state]} />
              </button>
            </li>
          );
        })}
      </ol>
      <span className="grow" />
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

/** Çalışma alanının üstündeki şerit: adımın adı, sayfaları (sekme) ve bu adımın eksik işleri (tıklanınca ilgili sayfa). */
export function StepHeader({ hasShots }: { hasShots: boolean }) {
  const { page, setPage, ov, advanced } = usePanel();
  const status = useMemo(() => stepReadiness(ov), [ov]);
  const badge = useMemo(() => pageBadges(ov), [ov]);
  if (isExtraPage(page)) return null;
  const step = stepOf(page);
  const i = STEPS.indexOf(step);
  const st = status[i];
  return (
    <div className="stephead" data-tour="stephead">
      <span className="sh-step"><span className="stp-n">{i + 1}</span>{step.label}</span>
      <div className="sh-tabs" role="tablist" aria-label={`${step.label} sayfaları`}>
        {step.pages.map((x) => {
          if (!advanced && ADVANCED_PAGES.has(x)) return null;
          const Ico = Icon[x];
          const b = badge[x];
          return (
            <button key={x} type="button" role="tab" aria-selected={page === x} disabled={NEEDS_SHOTS.has(x) && !hasShots}
              title={`${PAGE_LABEL[x]} (Shift+${PAGES.indexOf(x) + 1})`} onClick={() => setPage(x)}>
              <Ico />{PAGE_LABEL[x]}{b && <span className={`badge ${b.tone || ""}`} title={b.title}>{b.text}</span>}
            </button>
          );
        })}
      </div>
      <span className="grow" />
      <div className="sh-todos" aria-label="Bu adımda eksikler">
        {st.state === "done" && ov ? <span className="todo ok"><Icon.check />Bu adım tamam</span>
          : st.todos.map((t) => (
            <button key={t.text} type="button" className={`todo ${t.tone || ""}`} onClick={() => setPage(t.page)} title={`${PAGE_LABEL[t.page]} sayfasına git`}>
              {t.text}
            </button>
          ))}
      </div>
    </div>
  );
}
