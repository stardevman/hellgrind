"use client";
/* Yapılacaklar (alt bant): eksik işler, çözüldükleri sayfaya göre gruplanmış bir açılır listedir; maddeye tıklamak o
   sayfayı açar. Altında müşteri onay kapıları (Senaryo, Tasarım, Animatik, Taslak kurgu, Final, Kabul) durur. Sıra
   dayatmaz: dört üretim grubu yalnızca arka planda eksikleri hesaplar (lib/steps.ts). */
import { useCallback, useMemo, useRef, useState } from "react";
import { gateChips } from "@/lib/client";
import { usePanel } from "@/lib/panel";
import { nextTodo, STEPS, stepReadiness, todosByPage } from "@/lib/steps";
import { Icon } from "./icons";
import { pagePath } from "@/lib/groups";
import { PAGE_LABEL } from "./PageBar";
import { useDismiss } from "./ui";

export function Todos() {
  const { ov, setPage } = usePanel();
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, close, box);
  const status = useMemo(() => stepReadiness(ov), [ov]);
  const groups = useMemo(() => todosByPage(status), [status]);
  const gates = useMemo(() => (ov ? STEPS.flatMap((s) => gateChips(ov, s.id)) : []), [ov]);
  const all = groups.flatMap((g) => g.todos);
  const worst = all.some((t) => t.tone === "bad") ? "bad" : all.some((t) => t.tone === "warn") ? "warn" : "";
  return (
    <div className="todos-wrap" ref={box} data-tour="todos">
      <button type="button" className={`todos-btn ${worst}`} aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen((v) => !v)}
        title={all.length ? `${all.length} bekleyen iş` : "Bekleyen iş yok"}>
        <Icon.tasks /><span className="td-l">Yapılacaklar</span>
        {all.length > 0 ? <b className="td-n mono">{all.length}</b> : <Icon.check />}
      </button>
      {open && (
        <div className="menu todos-pop" role="dialog" aria-label="Yapılacaklar">
          {all.length === 0 ? (
            <p className="td-ok"><Icon.check />Her şey tamam{ov ? ": bekleyen iş yok" : ""}</p>
          ) : groups.map((g) => {
            const Ico = Icon[g.page];
            return (
              <div key={g.page} className="td-group">
                <div className="td-h"><Ico />{pagePath(g.page, PAGE_LABEL[g.page])}</div>
                {g.todos.map((t) => (
                  <button key={t.text} type="button" className={`td-item ${t.tone || ""}`} onClick={() => { setPage(t.page); close(); }}
                    title={`${pagePath(t.page, PAGE_LABEL[t.page])} sayfasına git`}>
                    <span className="grow">{t.text}</span><Icon.chevron />
                  </button>
                ))}
              </div>
            );
          })}
          {gates.length > 0 && (
            <div className="td-group">
              <div className="td-h">Müşteri onayları</div>
              <div className="gate-chips" aria-label="Müşteri onay kapıları" data-tour="client-gates">
                {gates.map((c) => <span key={c.gate} className={`gchip ${c.tone}`} title={c.title}>{c.label}</span>)}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Sıradaki iş (alt bant): ilk tamamlanmamış adımın ilk eksiği; tıklamak onu çözen sayfayı açar. Eksik yoksa görünmez. */
export function NextStep() {
  const { ov, setPage } = usePanel();
  const next = useMemo(() => (ov ? nextTodo(stepReadiness(ov)) : null), [ov]);
  if (!next) return null;
  const { todo } = next;
  const where = pagePath(todo.page, PAGE_LABEL[todo.page]);
  return (
    <button type="button" className={`next-btn ${todo.tone || ""}`} data-tour="next-todo" onClick={() => setPage(todo.page)}
      title={`Sıradaki iş: ${todo.text} (${where} sayfasını açar)`} aria-label={`Sıradaki iş: ${todo.text}. ${where} sayfasını açar`}>
      <Icon.arrow /><span className="nb-k">Sıradaki</span><span className="nb-t">{todo.text}</span>
    </button>
  );
}
