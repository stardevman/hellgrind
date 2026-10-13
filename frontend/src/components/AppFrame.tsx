"use client";
/* Stüdyo çerçevesi: üstte proje, bütçe, Ayarlar ve durdurma düğmesi; ortada etkin sayfa; altta dört üretim adımı ve
   sayfaları (Shift+1…8 ile geçilir). Kullanıcı / Yönetici seçimi Ayarlar'dadır: kullanıcı modu yalnızca filmi yapmak için
   gerekenleri gösterir; ücretli servisler, mod, fiyat tablosu, servis geçmişi ve skor ayrıntıları yönetici modundadır
   (şifre tanımlıysa girişle). Rehber turu ilk açılışta kendiliğinden başlar. */
import { useEffect, useRef, useState, type FormEvent, type ReactElement } from "react";
import { api } from "@/lib/api";
import { PROJECT_STATUS_TR } from "@/lib/i18n";
import { isExtraPage, NEEDS_SHOTS, PAGES, usePanel, type Page } from "@/lib/panel";
import { usd } from "@/lib/text";
import { AssetsPage } from "./AssetsPage";
import { BoardPage } from "./BoardPage";
import { ColorPage } from "./ColorPage";
import { ComparePage } from "./ComparePage";
import { DeliverPage } from "./DeliverPage";
import { EditPage } from "./EditPage";
import { HistoryPage } from "./HistoryPage";
import { Icon } from "./icons";
import { ScriptPage } from "./ScriptPage";
import { SettingsPage } from "./SettingsPage";
import { StepBar, StepHeader } from "./StepBar";
import { StylePage } from "./StylePage";
import { Tour } from "./Tour";
import { shortcutTarget } from "./ui";

const PAGE_VIEW: Record<Page, () => ReactElement | null> = {
  script: ScriptPage, style: StylePage, assets: AssetsPage, board: BoardPage, edit: EditPage, compare: ComparePage,
  color: ColorPage, deliver: DeliverPage, settings: SettingsPage, history: HistoryPage,
};

const Waiting = () => <p className="loading">Yükleniyor…</p>;

function BudgetChip() {
  const { ov, meta } = usePanel();
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc); };
  }, [open]);
  if (!ov) return null;
  const p = ov.project, limit = p.budget_limit_usd || 1;
  const left = Math.max(0, limit - p.spent_usd - p.reserved_usd);
  const pct = (x: number) => `${Math.min(100, (x / limit) * 100).toFixed(2)}%`;
  const used = Math.round(((p.spent_usd + p.reserved_usd) / limit) * 100);
  return (
    <div className="budget-wrap" ref={box} data-tour="budget">
      <button type="button" className={`budget-chip${p.soft_limited ? " soft" : ""}${p.frozen ? " frozen" : ""}`} aria-haspopup="dialog" aria-expanded={open}
        onClick={() => setOpen((v) => !v)} title={`Bütçenin yüzde ${used} kadarı harcandı ya da ayrıldı${p.dry_run ? " (sanal)" : ""}`}>
        <span className="meter" role="img" aria-label={`Bütçenin yüzde ${used} kadarı kullanıldı`}>
          <i className="spent" style={{ width: pct(p.spent_usd) }} />
          <i className="reserved" style={{ left: pct(p.spent_usd), width: pct(p.reserved_usd) }} />
          <i className="soft" style={{ left: `${(p.settings.soft_budget_ratio * 100).toFixed(1)}%` }} />
        </span>
        <span className="mono"><b>{usd(p.spent_usd)}</b> / {usd(p.budget_limit_usd)}</span>
      </button>
      {open && (
        <div className="menu budget-pop" role="dialog" aria-label="Bütçe">
          <dl className="kv">
            <dt>Harcanan</dt><dd className="mono">{usd(p.spent_usd)}{p.dry_run ? " (sanal)" : ""}</dd>
            <dt>Ayrılan</dt><dd className="mono">{usd(p.reserved_usd)}</dd>
            <dt>Kalan</dt><dd className="mono">{usd(left)}</dd>
            <dt>Limit</dt><dd className="mono">{usd(p.budget_limit_usd)}</dd>
            <dt>Yumuşak sınır</dt><dd className="mono">{usd(limit * p.settings.soft_budget_ratio)}</dd>
          </dl>
          <p className="faint small mt8">
            {meta?.dry_run === false ? "Ücretli mod: gerçek harcama; limitte üretim durur." : "Ücretsiz mod: harcama sanaldır, gerçek sağlayıcı fiyatlarıyla hesaplanır."}
            {" "}Yumuşak sınırdan sonra yeni tam dalga başlamaz.
          </p>
        </div>
      )}
    </div>
  );
}

function ProjectSwitcher({ onRemove }: { onRemove: () => void }) {
  const { projects, projectId, selectProject, setCreating, setPage } = usePanel();
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const current = projects?.find((p) => p.id === projectId);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc); };
  }, [open]);
  return (
    <div className="switcher" ref={box} data-tour="project">
      <button type="button" className="switch-btn" aria-haspopup="menu" aria-expanded={open} aria-label="Proje seçici" onClick={() => setOpen((v) => !v)}>
        <span className="sw-title">{current ? current.title : projects?.length ? "Proje seç" : "Henüz proje yok"}</span>
        {current && <span className="sw-status">{PROJECT_STATUS_TR[current.display_status ?? current.status] || current.status}</span>}
        <Icon.chevron />
      </button>
      {open && (
        <div className="menu" role="menu">
          {projects?.map((p) => (
            <button key={p.id} type="button" role="menuitem" className={p.id === projectId ? "on" : ""}
              onClick={() => { selectProject(p.id); setOpen(false); }}>
              <span className="mi-ck">{p.id === projectId && <Icon.check />}</span>
              <span className="mi-t">{p.title}</span>
              <span className="mi-s">{PROJECT_STATUS_TR[p.display_status ?? p.status] || p.status}</span>
            </button>
          ))}
          {projects?.length ? <div className="menu-sep" /> : null}
          <button type="button" role="menuitem" onClick={() => { setOpen(false); setCreating(true); setPage("script"); }}>
            <span className="mi-ck"><Icon.plus /></span><span className="mi-t">Yeni proje</span>
          </button>
          {current && (
            <button type="button" role="menuitem" className="danger" onClick={() => { setOpen(false); onRemove(); }}>
              <span className="mi-ck"><Icon.trash /></span><span className="mi-t">Projeyi sil…</span>
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function TopBar() {
  const { projects, projectId, selectProject, page, setPage, meta, ov, act, setHistory, admin } = usePanel();
  const q = meta?.quality;
  const frozen = Boolean(ov?.project.frozen);
  const kill = () => act(() => api.post(`/projects/${projectId}/killswitch`, frozen ? { action: "resume" } : { action: "freeze", reason: "panelden durduruldu" }), "global");
  const current = projects?.find((p) => p.id === projectId);
  const remove = async () => {
    if (!projectId || !current) return;
    if (!window.confirm(`"${current.title}" projesi ve tüm sahneleri, varyasyonları ve kayıtları kalıcı olarak silinsin mi? Bu geri alınamaz. Servis geçmişi (harcama kaydı) silinmez.`)) return;
    if (await act(() => api.del(`/projects/${projectId}`), "global")) selectProject(null);
  };
  return (
    <header className="topbar">
      <div className="tb-main">
        <div className="logo"><Icon.logo />Hell Grind</div>
        <ProjectSwitcher onRemove={remove} />
        <span className="grow" />
        {admin && q && (
          <span className={`chip${q.mode === "real" && q.ready ? " free" : q.mode === "dry" ? "" : " paid"}`}
            title={(q.mode === "dry" ? "Kalite skoru: sahte (ücretsiz mod)"
              : q.ready ? "Kalite skoru: gerçek (OpenCLIP + SFace)" : "Kalite skoru: gerçek skorlayıcı kullanılamıyor") + (q.reason ? ` — ${q.reason}` : "")}>
            {q.mode === "dry" ? "Skor: sahte" : q.ready ? "Skor: gerçek" : "Skor: kullanılamıyor"}
          </span>
        )}
        <BudgetChip />
        {admin && (
          <button type="button" className={`chip ${meta ? (meta.dry_run ? "free" : "paid") : ""}`} style={{ background: "none", cursor: "pointer" }}
            title={(meta?.dry_run === false ? "Ücretli mod açık: gerçek harcama yapılır" : "Ücretsiz mod: hiçbir ücretli servis çağrılmaz") + " — Ayarlar"}
            onClick={() => setPage("settings")}>
            {!meta ? "Mod yükleniyor…" : meta.dry_run ? "Ücretsiz mod" : "Ücretli mod açık"}
          </button>
        )}
        {admin && (
          <button type="button" className="btn ghost icon-only" aria-current={page === "history" ? "page" : undefined}
            onClick={() => { if (page !== "history") setHistory({}, true); }} aria-label="Geçmiş"
            title="Ücretli servis geçmişi: hangi servise, hangi modelle, ne için ne kadar harcandı"><Icon.receipt /></button>
        )}
        <button type="button" className="btn ghost icon-only" aria-current={page === "settings" ? "page" : undefined} onClick={() => setPage("settings")}
          aria-label="Ayarlar" data-tour="settings"
          title={admin ? "Kullanıcı / yönetici seçimi, ücretli servisler, mod ve fiyat tablosu" : "Ayarlar: kullanıcı / yönetici seçimi"}><Icon.gear /></button>
        {ov && (
          <button type="button" className={`kill${frozen ? " resume" : ""}`} onClick={kill}
            title={frozen ? "Kuyruk kaldığı yerden sürer; tamamlanmış klipler yeniden üretilmez" : "Kill-Switch: kuyruktaki işler iptal olur, yeni iş başlamaz"}>
            {frozen ? "Üretime devam et" : "Üretimi durdur"}
          </button>
        )}
      </div>
    </header>
  );
}

function Alerts() {
  const { ov, offline, notes } = usePanel();
  const p = ov?.project;
  const frozenEvent = ov?.events.find((e) => e.kind === "frozen") as { reason?: string } | undefined;
  const g = notes.global;
  return (
    <div className="alerts">
      {offline && <div className="alert offline" role="status">Sunucuya ulaşılamıyor; yeniden deniyorum…</div>}
      {p?.frozen && (
        <div className="alert frozen" role="alert">
          Üretim durduruldu{frozenEvent?.reason ? ` (${frozenEvent.reason})` : ""}. Çekimler donduruldu; devam edene kadar yeni iş başlamaz.
        </div>
      )}
      {p?.soft_limited && !p.frozen && (
        <div className="alert soft">Yumuşak bütçe sınırı aşıldı: yeni tam dalga başlatılmıyor, çekimler eldeki adaylarla tamamlanıyor.</div>
      )}
      {g?.text && <div className={`alert ${g.kind === "err" ? "frozen" : "offline"}`} role="status">{g.text}</div>}
    </div>
  );
}

/** Yönetici girişi: şifre tanımlıyken "Yönetici" seçilince açılır. Şifre yalnızca sunucuya gider; tarayıcıda saklanmaz. */
function AdminLogin() {
  const { loginOpen, setLoginOpen, login } = usePanel();
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!loginOpen) return;
    setPassword(""); setError("");
    input.current?.focus();
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setLoginOpen(false); };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [loginOpen, setLoginOpen]);
  if (!loginOpen) return null;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSending(true); setError("");
    try { await login(password); } catch (err) { setError((err as Error).message); setPassword(""); input.current?.focus(); } finally { setSending(false); }
  };
  return (
    <div className="modal-back" onMouseDown={(e) => { if (e.target === e.currentTarget) setLoginOpen(false); }}>
      <form className="modal" role="dialog" aria-modal="true" aria-labelledby="admin-login-t" onSubmit={submit}>
        <h2 id="admin-login-t"><Icon.lock />Yönetici girişi</h2>
        <p className="faint small">Ücretli servisler, çalışma modu, fiyat tablosu ve servis geçmişi yönetici modundadır.</p>
        <label className="field">Şifre
          <input ref={input} type="password" autoComplete="current-password" required maxLength={400} value={password}
            onChange={(e) => setPassword(e.target.value)} />
        </label>
        {error && <div className="note err" role="alert">{error}</div>}
        <div className="row">
          <span className="grow" />
          <button type="button" className="btn ghost" onClick={() => setLoginOpen(false)}>Vazgeç</button>
          <button type="submit" className="btn primary" disabled={sending || !password}>{sending ? "Giriş yapılıyor…" : "Giriş yap"}</button>
        </div>
      </form>
    </div>
  );
}

/** Kullanıcı modunda servis geçmişi açılırsa (eski bağlantı, oturum süresi doldu) sayfa yerine gösterilir. */
function AdminGate() {
  const { setPage, setAdmin, adminState } = usePanel();
  return (
    <div className="gate">
      <div className="gate-card">
        <span className="gate-ico"><Icon.lock /></span>
        <h2>Servis geçmişi yönetici modunda açılır</h2>
        <p className="faint">Ücretli servislerin bağlantıları, çalışma modu, fiyat tablosu ve harcama kayıtları yalnızca yöneticiye görünür.</p>
        <div className="row">
          <button type="button" className="btn" onClick={() => setPage("edit")}>Stüdyoya dön</button>
          <button type="button" className="btn primary" onClick={() => setAdmin(true)}>{adminState?.required ? "Yönetici girişi" : "Yönetici moduna geç"}</button>
        </div>
      </div>
    </div>
  );
}

export function AppFrame() {
  const { projectId, ov, creating, offline, page, setPage, projects, admin, adminState } = usePanel();
  const hasShots = Boolean(ov?.scenes.length);

  useEffect(() => {                                       // Shift+1…8: sayfalar arasında geçiş
    const onKey = (e: KeyboardEvent) => {
      if (!e.shiftKey || !shortcutTarget(e)) return;
      const i = Number(e.code.replace("Digit", "")) - 1;
      if (!Number.isInteger(i) || i < 0 || i >= PAGES.length) return;
      e.preventDefault();
      setPage(PAGES[i]);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setPage]);

  const loading = (!projects && !offline) || (projectId && !ov && !creating && !offline);
  const target: Page = isExtraPage(page) && !creating ? page
    : creating || !projectId || (ov && NEEDS_SHOTS.has(page) && !hasShots) ? "script" : page;
  const View = target === "history" && !admin ? (adminState ? AdminGate : Waiting) : PAGE_VIEW[target];
  return (
    <div className="app">
      <TopBar />
      <Alerts />
      <StepHeader hasShots={hasShots} />
      <main className="workspace">
        {loading && !isExtraPage(target) ? <p className="loading">Yükleniyor…</p>
          : <View key={isExtraPage(target) ? target : `${projectId}:${target}`} />}
      </main>
      <StepBar hasShots={hasShots} />
      <Tour />
      <AdminLogin />
    </div>
  );
}
