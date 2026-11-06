"use client";
/* Panel asistanı: alt bantta sohbet düğmesi, bandın üstünde sağda açılan sohbet paneli. Asistan (Claude) projeyi, skorları
   ve kareleri görür; istenince çekim metnini düzeltir. Üreten her eylem (yeniden üretim, yeni aday, aday seçimi) sohbet
   içinde onay kartıyla gelir: "İzin ver", "Bu oturum için tümüne izin ver", "Vazgeç". Yanıt arka uçta yazılır; panel
   oturumu yanıt bitene dek yoklar. Seçili çekim ve açık sayfa her mesajla gönderilir ("bu çekim" denince anlaşılır). */
import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import useSWR from "swr";
import { api, fetcher } from "@/lib/api";
import { actionOutcome, ago, costLabel, SESSION_KEY, type AssistantAction, type AssistantSessionView,
  type AssistantState } from "@/lib/assistant";
import { usePanel } from "@/lib/panel";
import { Icon } from "./icons";

function readSession(projectId: string): string | null {
  try { return localStorage.getItem(SESSION_KEY(projectId)); } catch { return null; }
}
function writeSession(projectId: string, id: string | null) {
  try { if (id) localStorage.setItem(SESSION_KEY(projectId), id); else localStorage.removeItem(SESSION_KEY(projectId)); } catch { /* depolama kapalı */ }
}

/** Alt banttaki düğme ve açılan panel. */
export function AssistantButton() {
  const { projectId } = usePanel();
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const esc = (e: globalThis.KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [open]);
  if (!projectId) return null;
  return (
    <>
      <button type="button" className="pb-btn" aria-label="Asistan" aria-expanded={open} data-tour="assistant"
        aria-current={open ? "page" : undefined} onClick={() => setOpen(!open)}
        title="Asistan (Claude): projeyi ve çekimleri inceler, sorunları bulur, isteğinle düzeltir. Üreten her eylem için önce izin ister.">
        <Icon.chat />
      </button>
      {open && <AssistantPanel key={projectId} projectId={projectId} onClose={() => setOpen(false)} />}
    </>
  );
}

function AssistantPanel({ projectId, onClose }: { projectId: string; onClose: () => void }) {
  const { data: state, mutate: mutateState } = useSWR<AssistantState>(`/projects/${projectId}/assistant`, fetcher);
  const [sessionId, setSessionId] = useState<string | null>(() => readSession(projectId));
  const [listOpen, setListOpen] = useState(false);
  const { data: view, mutate, error } = useSWR<AssistantSessionView>(sessionId ? `/assistant/sessions/${sessionId}` : null, fetcher,
    { refreshInterval: (d) => (d?.busy ? 1200 : 0), shouldRetryOnError: false });
  const select = useCallback((id: string | null) => { setSessionId(id); writeSession(projectId, id); setListOpen(false); }, [projectId]);
  useEffect(() => { if (error && sessionId) select(null); }, [error, sessionId, select]);   // silinmiş sohbet

  const newChat = async () => {
    const v = await api.post<AssistantSessionView>(`/projects/${projectId}/assistant/sessions`);
    select(v.id);
    void mutateState();
    return v.id;
  };

  return (
    <section className="asst" role="dialog" aria-label="Asistan" aria-modal="false">
      <header className="asst-h">
        <Icon.chat />
        <div className="asst-title">
          <b>Asistan</b>
          <button type="button" className="asst-pick" aria-expanded={listOpen} onClick={() => setListOpen(!listOpen)}
            title="Önceki sohbetler"><span>{view?.title || "Yeni sohbet"}</span><Icon.chevron /></button>
        </div>
        <button type="button" className="pb-btn" onClick={() => { void newChat(); }} aria-label="Yeni sohbet" title="Yeni sohbet"><Icon.plus /></button>
        <button type="button" className="pb-btn" onClick={onClose} aria-label="Kapat" title="Kapat (Esc)"><Icon.close /></button>
      </header>
      {listOpen && (
        <div className="asst-list" role="menu" aria-label="Sohbetler">
          {!state?.sessions.length && <p className="faint small">Henüz sohbet yok.</p>}
          {state?.sessions.map((x) => (
            <button key={x.id} type="button" role="menuitem" aria-current={x.id === sessionId ? "true" : undefined} onClick={() => select(x.id)}>
              <span className="grow">{x.title}</span><small className="faint">{x.busy ? "yazıyor…" : ago(x.updated_at)}</small>
            </button>
          ))}
        </div>
      )}
      {state && !state.available && <div className="asst-warn" role="status">{state.reason}</div>}
      <Messages view={view} onChanged={() => { void mutate(); }} />
      {view?.allow_all && (
        <div className="asst-allow" role="status">
          <span className="grow">Bu sohbette üreten eylemler sormadan başlıyor.</span>
          <button type="button" className="btn ghost" onClick={() => { void api.post(`/assistant/sessions/${view.id}/allow-all?on=false`).then(() => mutate()); }}>
            İzni kaldır</button>
        </div>
      )}
      {view?.error && <div className="note err" role="alert">{view.error}</div>}
      <Composer busy={Boolean(view?.busy)} model={state?.model}
        send={async (text, page, sceneId) => {
          const id = sessionId ?? await newChat();
          const v = await api.post<AssistantSessionView>(`/assistant/sessions/${id}/messages`, { text, page, scene_id: sceneId ?? null });
          await mutate(v, { revalidate: true });
          void mutateState();
        }} />
    </section>
  );
}

function Messages({ view, onChanged }: { view: AssistantSessionView | undefined; onChanged: () => void }) {
  const end = useRef<HTMLDivElement>(null);
  const count = view?.items.length ?? 0;
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [count, view?.busy]);
  if (!view || !view.items.length) {
    return (
      <div className="asst-body">
        <div className="asst-empty">
          <p>Projeyle ilgili her şeyi sorabilirsin. Örneğin:</p>
          <ul>
            <li>“Proje ne durumda, hangi çekimlerde sorun var?”</li>
            <li>“S013’te havada uçan bir şey var, düzelt.”</li>
            <li>“Bu çekimin skoru neden düşük?”</li>
          </ul>
          <p className="faint small">Ücret gerektiren her eylem için önce izin ister.</p>
        </div>
      </div>
    );
  }
  return (
    <div className="asst-body" aria-live="polite">
      {view.items.map((it, i) => {
        if (it.role === "user") return <div key={i} className="asst-msg me">{it.text}</div>;
        if (it.role === "assistant") return <div key={i} className={`asst-msg ai${it.offline ? " offline" : ""}`}>{it.text}</div>;
        if (it.role === "tool") return <div key={i} className={`asst-tool${it.error ? " err" : ""}`} title={it.error || undefined}><Icon.search />{it.text}</div>;
        return <ActionCard key={i} a={it.action} onChanged={onChanged} />;
      })}
      {view.busy && <div className="asst-typing" role="status">Asistan yazıyor…</div>}
      <div ref={end} />
    </div>
  );
}

function ActionCard({ a, onChanged }: { a: AssistantAction; onChanged: () => void }) {
  const { refresh, note } = usePanel();
  const [sending, setSending] = useState(false);
  const decide = async (path: "approve" | "decline", body?: unknown) => {
    setSending(true);
    try {
      if (path === "approve") await api.post(`/assistant/actions/${a.id}/approve`, body);
      else await api.post(`/assistant/actions/${a.id}/decline`);
      refresh();
    } catch (e) {
      note("global", (e as Error).message, "err");
    } finally {
      setSending(false);
      onChanged();
    }
  };
  const outcome = actionOutcome(a);
  return (
    <div className={`asst-card ${a.status}`}>
      <b>{a.title}</b>
      <p>{a.summary}</p>
      <p className="faint small">{costLabel(a)}</p>
      {a.status === "pending" ? (
        <div className="asst-card-btns">
          <button type="button" className="btn primary" disabled={sending} onClick={() => { void decide("approve", { scope: "once" }); }}>İzin ver</button>
          <button type="button" className="btn" disabled={sending} onClick={() => { void decide("approve", { scope: "session" }); }}
            title="Bu sohbetteki sonraki üreten eylemler sormadan başlar; istediğin an kaldırabilirsin">Bu oturum için tümüne izin ver</button>
          <button type="button" className="btn ghost" disabled={sending} onClick={() => { void decide("decline"); }}>Vazgeç</button>
        </div>
      ) : <p className={`small ${a.status === "failed" ? "bad" : "faint"}`}>{outcome}</p>}
    </div>
  );
}

function Composer({ busy, model, send }: { busy: boolean; model?: string; send: (text: string, page: string, sceneId?: string) => Promise<void> }) {
  const { page, scene, note } = usePanel();
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const box = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { box.current?.focus(); }, []);
  const submit = async (e?: FormEvent) => {
    e?.preventDefault();
    const t = text.trim();
    if (!t || busy || sending) return;
    setSending(true);
    try { await send(t, page, scene?.id); setText(""); } catch (err) { note("global", (err as Error).message, "err"); } finally { setSending(false); }
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void submit(); }
  };
  return (
    <form className="asst-in" onSubmit={submit}>
      {scene && <small className="faint">Seçili çekim: <span className="mono">{scene.scene_code}</span></small>}
      <div className="row">
        <textarea ref={box} rows={2} value={text} maxLength={8000} onChange={(e) => setText(e.target.value)} onKeyDown={onKey}
          placeholder="Bir şey sor ya da iste… (Enter gönderir, Shift+Enter yeni satır)" aria-label="Asistana mesaj" />
        <button type="submit" className="btn primary" disabled={!text.trim() || busy || sending} aria-label="Gönder"><Icon.arrow /></button>
      </div>
      {model && <small className="faint">Model: <span className="mono">{model}</span></small>}
    </form>
  );
}
