"use client";
/* Stüdyo çerçevesi, DaVinci Resolve düzeninde: üstte araç bandı (solda logo, ortada Dizi › Sezon › Bölüm, sağda
   Yapılacaklar, bütçe ve durdurma düğmesi; altında uyarılar), ortada etkin sayfa, altta sayfa çubuğu (solda Proje
   yöneticisi, ortada simgeli sayfa menüsü, sağda Ayarlar). Sayfalar bir sıra değil menüdür. Shift+1…9 sayfalar arasında
   geçer; G yayın güvenli alanlarını açıp kapatır. Kullanıcı / Yönetici seçimi Ayarlar'dadır: kullanıcı modu yalnızca filmi
   yapmak için gerekenleri gösterir; ücretli servisler, mod, fiyat tablosu, servis geçmişi ve skor ayrıntıları yönetici
   modundadır (şifre tanımlıysa girişle). Rehber turu ilk açılışta kendiliğinden başlar. */
import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactElement } from "react";
import { api } from "@/lib/api";
import { isExtraPage, NEEDS_SHOTS, PAGES, usePanel, type Page } from "@/lib/panel";
import { lockLabel } from "@/lib/series";
import { usd } from "@/lib/text";
import { AssetsPage } from "./AssetsPage";
import { BoardPage } from "./BoardPage";
import { Breadcrumb } from "./Breadcrumb";
import { ColorPage } from "./ColorPage";
import { ComparePage } from "./ComparePage";
import { DeliverPage } from "./DeliverPage";
import { EditPage } from "./EditPage";
import { HistoryPage } from "./HistoryPage";
import { Icon } from "./icons";
import { ScriptPage } from "./ScriptPage";
import { SettingsPage } from "./SettingsPage";
import { PageBar } from "./PageBar";
import { ProjectManager } from "./ProjectManager";
import { Todos } from "./Todos";
import { StylePage } from "./StylePage";
import { VoicePage } from "./VoicePage";
import { Tour } from "./Tour";
import { shortcutTarget, useDismiss } from "./ui";

const PAGE_VIEW: Record<Page, () => ReactElement | null> = {
  script: ScriptPage, voice: VoicePage, style: StylePage, assets: AssetsPage, board: BoardPage, edit: EditPage, compare: ComparePage,
  color: ColorPage, deliver: DeliverPage, settings: SettingsPage, history: HistoryPage,
};

const Waiting = () => <p className="loading">Yükleniyor…</p>;

function BudgetChip() {
  const { ov, meta } = usePanel();
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, close, box);
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

function TopBar() {
  const { projects, projectId, selectProject, page, setPage, meta, ov, act, setHistory, admin } = usePanel();
  const q = meta?.quality;
  const frozen = Boolean(ov?.project.frozen);
  const frozenReason = (ov?.events.find((e) => e.kind === "frozen") as { reason?: string } | undefined)?.reason;
  const [confirmStop, setConfirmStop] = useState(false);
  const kill = () => act(() => api.post(`/projects/${projectId}/killswitch`, frozen ? { action: "resume" } : { action: "freeze", reason: "panelden durduruldu" }), "global");
  const current = projects?.find((p) => p.id === projectId);
  const remove = async () => {
    if (!projectId || !current) return;
    if (!window.confirm(`"${current.title}" ${current.season_id ? "bölümü" : "projesi"} ve tüm sahneleri, varyasyonları ve kayıtları kalıcı olarak silinsin mi? Bu geri alınamaz. Servis geçmişi (harcama kaydı) silinmez.`)) return;
    if (await act(() => api.del(`/projects/${projectId}`), "global")) selectProject(null);
  };
  return (
    <header className="topbar">
      <div className="tb-main">
        <div className="tb-l">
          <div className="logo" title="Hell Grind Stüdyo"><Icon.logo /><span>Hell Grind</span></div>
          {admin && (
            <button type="button" className={`chip ${meta ? (meta.dry_run ? "free" : "paid") : ""}`} style={{ background: "none", cursor: "pointer" }}
              title={(meta?.dry_run === false ? "Ücretli mod açık: gerçek harcama yapılır" : "Ücretsiz mod: hiçbir ücretli servis çağrılmaz") + " — Ayarlar"}
              onClick={() => setPage("settings")}>
              {!meta ? "Mod yükleniyor…" : meta.dry_run ? "Ücretsiz mod" : "Ücretli mod açık"}
            </button>
          )}
          {admin && q && (
            <span className={`chip${q.mode === "real" && q.ready ? " free" : q.mode === "dry" ? "" : " paid"}`}
              title={(q.mode === "dry" ? "Kalite skoru: sahte (ücretsiz mod)"
                : q.ready ? "Kalite skoru: gerçek (OpenCLIP + SFace)" : "Kalite skoru: gerçek skorlayıcı kullanılamıyor") + (q.reason ? ` — ${q.reason}` : "")}>
              {q.mode === "dry" ? "Skor: sahte" : q.ready ? "Skor: gerçek" : "Skor: kullanılamıyor"}
            </span>
          )}
        </div>
        <div className="tb-c"><Breadcrumb onRemove={remove} /></div>
        <div className="tb-r">
          <Todos />
          <BudgetChip />
          {admin && (
            <button type="button" className="btn ghost icon-only" aria-current={page === "history" ? "page" : undefined}
              onClick={() => { if (page !== "history") setHistory({}, true); }} aria-label="Geçmiş"
              title="Ücretli servis geçmişi: hangi servise, hangi modelle, ne için ne kadar harcandı"><Icon.receipt /></button>
          )}
          {frozen && (
            <span className="chip paid" role="alert" title={`Üretim durduruldu${frozenReason ? ` (${frozenReason})` : ""}. Çekimler donduruldu; devam edene kadar yeni iş başlamaz.`}>
              Üretim durduruldu
            </span>
          )}
          {ov && (
            <button type="button" className={`btn ghost icon-only kill-ico${frozen ? " resume" : ""}`} onClick={frozen ? kill : () => setConfirmStop(true)}
              aria-label={frozen ? "Üretime devam et" : "Üretimi durdur"}
              title={frozen ? "Üretime devam et: kuyruk kaldığı yerden sürer; tamamlanmış klipler yeniden üretilmez" : "Üretimi durdur: önce onay sorulur"}>
              {frozen ? <Icon.play /> : <Icon.stop />}
            </button>
          )}
        </div>
      </div>
      <Alerts />
      {confirmStop && <StopConfirm onCancel={() => setConfirmStop(false)} onConfirm={() => { setConfirmStop(false); void kill(); }} />}
    </header>
  );
}

function Alerts() {
  const { ov, offline, notes, admin } = usePanel();
  const p = ov?.project;
  const g = notes.global;
  const locked = p?.episode?.locked;
  const [unlocking, setUnlocking] = useState(false);
  return (
    <div className="alerts">
      {offline && <div className="alert offline" role="status">Sunucuya ulaşılamıyor; yeniden deniyorum…</div>}
      {locked && (
        <div className="alert locked" role="status">
          <Icon.lock /><span className="grow">Bölüm teslim edildi ({lockLabel(locked.label)}): Kabul’deki film sürümüne ve dizi varlıklarının o anki
            sürümlerine sabit. Üretim, senaryo, stil ve bölüme özel varlıklar kilitli.</span>
          {admin && <button type="button" className="btn ghost" onClick={() => setUnlocking(true)}>Kilidi aç…</button>}
        </div>
      )}
      {unlocking && <UnlockConfirm onClose={() => setUnlocking(false)} />}
      {p?.soft_limited && !p.frozen && (
        <div className="alert soft">Yumuşak bütçe sınırı aşıldı: yeni tam dalga başlatılmıyor, çekimler eldeki adaylarla tamamlanıyor.</div>
      )}
      {g?.text && <div className={`alert ${g.kind === "err" ? "frozen" : "offline"}`} role="status">{g.text}</div>}
    </div>
  );
}

const UNLOCK_PHRASE = "kilidi aç";

/** Teslim edilmiş bölümün kilidini açma (yalnızca yönetici): onay ifadesi yazılır; karar kürasyon kaydına düşer. */
function UnlockConfirm({ onClose }: { onClose: () => void }) {
  const { projectId, act, ov } = usePanel();
  const [phrase, setPhrase] = useState("");
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    input.current?.focus();
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [onClose]);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void act(async () => { await api.post(`/projects/${projectId}/unlock`, { confirm: phrase }); onClose(); }, "global");
  };
  return (
    <div className="modal-back" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <form className="modal" role="alertdialog" aria-modal="true" aria-labelledby="unlock-t" aria-describedby="unlock-d" onSubmit={submit}>
        <h2 id="unlock-t"><Icon.lock />Teslim kilidi açılsın mı?</h2>
        <p id="unlock-d" className="faint small">
          {lockLabel(ov?.project.episode?.locked?.label)} kalkar; bölüm yeniden üretilebilir ve düzenlenebilir hâle gelir. Dizi varlıklarının sabit
          sürümleri korunur, hiçbir çekim kendiliğinden yeniden üretilmez. Karar kürasyon kaydına yazılır. Onaylamak için “{UNLOCK_PHRASE}” yazın.
        </p>
        <label className="field">Onay ifadesi
          <input ref={input} value={phrase} maxLength={50} onChange={(e) => setPhrase(e.target.value)} placeholder={UNLOCK_PHRASE} />
        </label>
        <div className="row">
          <span className="grow" />
          <button type="button" className="btn ghost" onClick={onClose}>Vazgeç</button>
          <button type="submit" className="btn danger" disabled={phrase.trim().toLocaleLowerCase("tr") !== UNLOCK_PHRASE}>Kilidi aç</button>
        </div>
      </form>
    </div>
  );
}

/** Üretimi durdurma onayı: düğme sistemi doğrudan durdurmaz. Esc ya da dışarı tıklama vazgeçer. */
function StopConfirm({ onCancel, onConfirm }: { onCancel: () => void; onConfirm: () => void }) {
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    cancel.current?.focus();
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onCancel(); };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [onCancel]);
  return (
    <div className="modal-back" onMouseDown={(e) => { if (e.target === e.currentTarget) onCancel(); }}>
      <div className="modal" role="alertdialog" aria-modal="true" aria-labelledby="stop-t" aria-describedby="stop-d">
        <h2 id="stop-t" className="bad"><Icon.stop />Üretim durdurulsun mu?</h2>
        <p id="stop-d" className="faint small">Kuyruktaki ve çalışan işler iptal edilir, yeni iş başlamaz. Tamamlanmış klipler korunur; devam edince kaldığı yerden sürer.</p>
        <div className="row">
          <span className="grow" />
          <button ref={cancel} type="button" className="btn ghost" onClick={onCancel}>Vazgeç</button>
          <button type="button" className="btn danger" onClick={onConfirm}>Üretimi durdur</button>
        </div>
      </div>
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
  const [manager, setManager] = useState(false);
  const closeManager = useCallback(() => setManager(false), []);

  const { safeAreas, setSafeAreas } = usePanel();
  useEffect(() => {                                       // G: yayın güvenli alanları
    const onKey = (e: KeyboardEvent) => {
      if (e.shiftKey || !shortcutTarget(e) || (e.key !== "g" && e.key !== "G")) return;
      e.preventDefault();
      setSafeAreas(!safeAreas);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [safeAreas, setSafeAreas]);

  useEffect(() => {                                       // Shift+1…9: sayfalar arasında geçiş
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
      <main className="workspace">
        {loading && !isExtraPage(target) ? <p className="loading">Yükleniyor…</p>
          : <View key={isExtraPage(target) ? target : `${projectId}:${target}`} />}
      </main>
      <PageBar hasShots={hasShots} onHome={() => setManager(true)} />
      {manager && <ProjectManager onClose={closeManager} />}
      <Tour />
      <AdminLogin />
    </div>
  );
}
