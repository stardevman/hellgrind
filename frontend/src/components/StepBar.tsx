"use client";
/* Üretim adımları, hepsi üstte: üst bantta dört adım (numara, ad, durum noktası; StepRail), hemen altındaki şeritte
   bulunulan adımın sayfa sekmeleri, bu adımın eksikleri ve en sağda "Sıradaki" bağlantısı (StepHeader). Ayarlar ve
   Geçmiş gibi adım dışı sayfalarda şerit sayfanın adını ve "Sıradaki"yi gösterir. Shift+1…8 sayfalar arasında geçer. */
import { useMemo } from "react";
import { isExtraPage, NEEDS_SHOTS, PAGES, usePanel, type Page } from "@/lib/panel";
import { nextTodo, pageBadges, STEPS, stepOf, stepReadiness, type StepStatus } from "@/lib/steps";
import { Icon } from "./icons";

export const PAGE_LABEL: Record<Page, string> = {
  script: "Senaryo", style: "Stil", assets: "Varlıklar", board: "Storyboard", edit: "Kurgu", compare: "Karşılaştır",
  color: "Renk", deliver: "Teslim", settings: "Ayarlar", history: "Geçmiş",
};
/** Kullanıcı modunda gizlenen sayfalar (Karşılaştır, Kurgu'daki "Adayları karşılaştır" ile yine açılır). */
const ADMIN_PAGES: ReadonlySet<Page> = new Set(["compare"]);
const STATE_TR = { todo: "sırada", current: "bu adımdasınız", done: "tamamlandı", blocked: "engelli" } as const;

function stepTitle(st: StepStatus, hint: string): string {
  const head = `${STEPS.find((s) => s.id === st.id)!.label}: ${STATE_TR[st.state]}`;
  return st.todos.length ? `${head}\n${st.todos.map((t) => `• ${t.text}`).join("\n")}` : `${head}\n${hint}`;
}

/** Üst banttaki dört üretim adımı; tıklanınca adımın ilk açılabilir sayfası açılır. */
export function StepRail({ hasShots }: { hasShots: boolean }) {
  const { page, setPage, ov } = usePanel();
  const status = useMemo(() => stepReadiness(ov), [ov]);
  const current = isExtraPage(page) ? null : stepOf(page).id;
  return (
    <nav className="tb-steps" aria-label="Üretim adımları" data-tour="steps">
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
    </nav>
  );
}

/** "Sıradaki": bütün adımlardaki ilk eksik iş; tıklanınca onu çözecek sayfa açılır. */
function NextUp({ next }: { next: ReturnType<typeof nextTodo> }) {
  const { setPage, ov } = usePanel();
  return (
    <div className="nextup" aria-live="polite">
      {next ? (
        <button type="button" className={`nx ${next.todo.tone || ""}`} onClick={() => setPage(next.todo.page)}
          title={`${next.step.label} adımı · ${PAGE_LABEL[next.todo.page]} sayfasına git`}>
          <span className="nx-k">Sıradaki</span><span className="nx-t">{next.todo.text}</span><Icon.chevron />
        </button>
      ) : ov ? <span className="nx done"><Icon.check />Tüm adımlar tamamlandı</span> : null}
    </div>
  );
}

/** Üst bandın altındaki şerit: adımın sayfaları (sekme), bu adımın eksikleri (tıklanınca ilgili sayfa) ve "Sıradaki". */
export function StepHeader({ hasShots }: { hasShots: boolean }) {
  const { page, setPage, ov, admin } = usePanel();
  const status = useMemo(() => stepReadiness(ov), [ov]);
  const badge = useMemo(() => pageBadges(ov), [ov]);
  const next = nextTodo(status);
  if (isExtraPage(page)) {
    const Ico = page === "settings" ? Icon.gear : Icon.receipt;
    return (
      <div className="stephead" data-tour="stephead">
        <div className="sh-tabs" role="tablist" aria-label="Sayfa">
          <button type="button" role="tab" aria-selected><Ico />{PAGE_LABEL[page]}</button>
        </div>
        <span className="grow" />
        <NextUp next={next} />
      </div>
    );
  }
  const step = stepOf(page);
  const st = status[STEPS.indexOf(step)];
  // "Sıradaki" bu adımdaysa aynı eksik iki kez yazılmaz: şeritte yalnızca "Sıradaki" olarak görünür.
  const todos = st.todos.filter((t) => !(next && t.text === next.todo.text && t.page === next.todo.page));
  return (
    <div className="stephead" data-tour="stephead">
      <div className="sh-tabs" role="tablist" aria-label={`${step.label} sayfaları`}>
        {step.pages.map((x) => {
          if (!admin && ADMIN_PAGES.has(x)) return null;
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
          : todos.map((t) => (
            <button key={t.text} type="button" className={`todo ${t.tone || ""}`} onClick={() => setPage(t.page)} title={`${PAGE_LABEL[t.page]} sayfasına git`}>
              {t.text}
            </button>
          ))}
      </div>
      <NextUp next={next} />
    </div>
  );
}
