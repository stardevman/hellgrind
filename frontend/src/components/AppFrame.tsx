"use client";
/* Stüdyo çerçevesi: çalışma alanı en üstten başlar, bütün denetimler tek alt banttadır (DaVinci Resolve'un sayfa çubuğu
   gibi): solda Proje yöneticisi (ev) ve Dizi › Sezon › Bölüm kırıntısı (ikisi de Proje yöneticisini açar; proje hiç yokken yönetici
   kendiliğinden açılır); ortada küçük simgeli sayfa menüsü; sağda
   Yapılacaklar, Geçmiş (yönetici), durdurma düğmesi ve Ayarlar (bütçe, çalışma modu ve skor Ayarlar'dadır; ücretli mod
   açıkken dişlide kırmızı nokta durur). Uyarılar bandın hemen üstünde, menüler yukarı
   açılır. Sayfalar bir sıra değil menüdür. Shift+1…9 sayfalar arasında
   geçer; G yayın güvenli alanlarını açıp kapatır. Kullanıcı / Yönetici seçimi Ayarlar'dadır: kullanıcı modu yalnızca filmi
   yapmak için gerekenleri gösterir; ücretli servisler, mod, fiyat tablosu, servis geçmişi ve skor ayrıntıları yönetici
   modundadır (şifre tanımlıysa girişle). Rehber turu ilk açılışta kendiliğinden başlar. */
import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactElement } from "react";
import { api } from "@/lib/api";
import { isExtraPage, NEEDS_SHOTS, PAGES, usePanel, type Page } from "@/lib/panel";
import { lockLabel } from "@/lib/series";
import { AssetsPage } from "./AssetsPage";
import { ImagesPage } from "./ImagesPage";
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
import { Pages } from "./PageBar";
import { ProjectManager } from "./ProjectManager";
import { Todos } from "./Todos";
import { StylePage } from "./StylePage";
import { VoicePage } from "./VoicePage";
import { Tour } from "./Tour";
import { shortcutTarget } from "./ui";

const PAGE_VIEW: Record<Page, () => ReactElement | null> = {
  script: ScriptPage, voice: VoicePage, style: StylePage, images: ImagesPage, assets: AssetsPage, board: BoardPage, edit: EditPage, compare: ComparePage,
  color: ColorPage, deliver: DeliverPage, settings: SettingsPage, history: HistoryPage,
};

const Waiting = () => <p className="loading">Yükleniyor…</p>;

function Dock({ hasShots, onManager }: { hasShots: boolean; onManager: () => void }) {
  const { projectId, page, setPage, meta, ov, act, setHistory, admin } = usePanel();
  const frozen = Boolean(ov?.project.frozen);
  const frozenReason = (ov?.events.find((e) => e.kind === "frozen") as { reason?: string } | undefined)?.reason;
  const [confirmStop, setConfirmStop] = useState(false);
  const kill = () => act(() => api.post(`/projects/${projectId}/killswitch`, frozen ? { action: "resume" } : { action: "freeze", reason: "panelden durduruldu" }), "global");
  return (
    <footer className="dock">
      <Alerts />
      <div className="pagebar">
        <div className="pb-l">
          <button type="button" className="pb-btn" onClick={onManager} aria-label="Proje yöneticisi" data-tour="home"
            title="Proje yöneticisi: diziler, bölümler ve bağımsız filmler; yeni proje"><Icon.home /></button>
          <Breadcrumb onOpenManager={onManager} />
        </div>
        <Pages hasShots={hasShots} />
        <div className="pb-r">
          <Todos />
          {admin && (
            <button type="button" className="pb-btn" aria-current={page === "history" ? "page" : undefined}
              onClick={() => { if (page !== "history") setHistory({}, true); }} aria-label="Geçmiş"
              title="Ücretli servis geçmişi: hangi servise, hangi modelle, ne için ne kadar harcandı"><Icon.receipt /></button>
          )}
          {frozen && (
            <span className="chip paid" role="alert" title={`Üretim durduruldu${frozenReason ? ` (${frozenReason})` : ""}. Çekimler donduruldu; devam edene kadar yeni iş başlamaz.`}>
              Üretim durduruldu
            </span>
          )}
          {ov && (
            <button type="button" className={`pb-btn kill-ico${frozen ? " resume" : ""}`} onClick={frozen ? kill : () => setConfirmStop(true)}
              aria-label={frozen ? "Üretime devam et" : "Üretimi durdur"}
              title={frozen ? "Üretime devam et: kuyruk kaldığı yerden sürer; tamamlanmış klipler yeniden üretilmez" : "Üretimi durdur: önce onay sorulur"}>
              {frozen ? <Icon.play /> : <Icon.stop />}
            </button>
          )}
          <button type="button" className="pb-btn" aria-current={page === "settings" ? "page" : undefined} onClick={() => setPage("settings")}
            aria-label="Ayarlar" data-tour="settings"
            title={(meta?.dry_run === false ? "Ücretli mod açık: gerçek harcama yapılır. " : "")
              + (admin ? "Ayarlar: bütçe, çalışma modu ve skor; kullanıcı / yönetici seçimi, ücretli servisler ve fiyat tablosu" : "Ayarlar: bütçe ve kullanıcı / yönetici seçimi")}>
            <Icon.gear />{meta?.dry_run === false && <i className="gdot" aria-hidden />}
          </button>
        </div>
      </div>
      {confirmStop && <StopConfirm onCancel={() => setConfirmStop(false)} onConfirm={() => { setConfirmStop(false); void kill(); }} />}
    </footer>
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
  const none = Boolean(projects && projects.length === 0 && !creating);   // hiç proje yok: yönetici açık kalır
  useEffect(() => { if (none) setManager(true); }, [none]);

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

  useEffect(() => {                                       // Shift+1…9, Shift+0: sayfalar arasında geçiş
    const onKey = (e: KeyboardEvent) => {
      if (!e.shiftKey || !shortcutTarget(e) || !e.code.startsWith("Digit")) return;
      const n = Number(e.code.slice(5));
      const i = n === 0 ? 9 : n - 1;
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
      <main className="workspace">
        {loading && !isExtraPage(target) ? <p className="loading">Yükleniyor…</p>
          : <View key={isExtraPage(target) ? target : `${projectId}:${target}`} />}
      </main>
      <Dock hasShots={hasShots} onManager={() => setManager(true)} />
      {(manager || none) && <ProjectManager onClose={closeManager} locked={none} />}
      <Tour />
      <AdminLogin />
    </div>
  );
}
