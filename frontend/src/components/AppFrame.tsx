"use client";
/* Stüdyo çerçevesi: üstte proje, bütçe ve durdurma düğmesi; ortada etkin sayfa; altta üretim sırasıyla sayfa çubuğu
   (Shift+1…8 ile geçilir). */
import { useEffect, type ReactElement } from "react";
import { api } from "@/lib/api";
import { PROJECT_STATUS_TR } from "@/lib/i18n";
import { NEEDS_SHOTS, PAGES, usePanel, type Page } from "@/lib/panel";
import { usd } from "@/lib/text";
import { AssetsPage } from "./AssetsPage";
import { BoardPage } from "./BoardPage";
import { ColorPage } from "./ColorPage";
import { ComparePage } from "./ComparePage";
import { DeliverPage } from "./DeliverPage";
import { EditPage } from "./EditPage";
import { Icon } from "./icons";
import { ScriptPage } from "./ScriptPage";
import { SettingsPage } from "./SettingsPage";
import { StylePage } from "./StylePage";
import { shortcutTarget } from "./ui";

const PAGE_LABEL: Record<Page, string> = {
  script: "Senaryo", style: "Stil", assets: "Varlıklar", board: "Storyboard", edit: "Kurgu", compare: "Karşılaştır",
  color: "Renk", deliver: "Teslim", settings: "Ayarlar",
};
const PAGE_VIEW: Record<Page, () => ReactElement | null> = {
  script: ScriptPage, style: StylePage, assets: AssetsPage, board: BoardPage, edit: EditPage, compare: ComparePage,
  color: ColorPage, deliver: DeliverPage, settings: SettingsPage,
};

function Budget() {
  const { ov } = usePanel();
  if (!ov) return null;
  const p = ov.project, limit = p.budget_limit_usd || 1;
  const left = Math.max(0, limit - p.spent_usd - p.reserved_usd);
  const pct = (x: number) => `${Math.min(100, (x / limit) * 100).toFixed(2)}%`;
  return (
    <div className="budget" aria-label="Bütçe">
      <div className="meter" role="img" aria-label={`Bütçenin yüzde ${Math.round((p.spent_usd / limit) * 100)} kadarı harcandı`}>
        <i className="spent" style={{ width: pct(p.spent_usd) }} />
        <i className="reserved" style={{ left: pct(p.spent_usd), width: pct(p.reserved_usd) }} />
        <i className="soft" style={{ left: `${(p.settings.soft_budget_ratio * 100).toFixed(1)}%` }} title="Yumuşak limit" />
      </div>
      <div className="fig"><b>{usd(p.spent_usd)}</b><span>harcanan{p.dry_run ? " (sanal)" : ""}</span></div>
      <div className="fig opt"><b>{usd(p.reserved_usd)}</b><span>ayrılan</span></div>
      <div className="fig opt"><b>{usd(left)}</b><span>kalan</span></div>
      <div className="fig"><b>{usd(p.budget_limit_usd)}</b><span>limit</span></div>
    </div>
  );
}

function TopBar() {
  const { projects, projectId, selectProject, setCreating, page, setPage, meta, ov, act } = usePanel();
  const q = meta?.quality;
  const frozen = Boolean(ov?.project.frozen);
  const kill = () => act(() => api.post(`/projects/${projectId}/killswitch`, frozen ? { action: "resume" } : { action: "freeze", reason: "panelden durduruldu" }), "global");
  const current = projects?.find((p) => p.id === projectId);
  const remove = async () => {
    if (!projectId || !current) return;
    if (!window.confirm(`"${current.title}" projesi ve tüm sahneleri, varyasyonları ve kayıtları kalıcı olarak silinsin mi? Bu geri alınamaz.`)) return;
    if (await act(() => api.del(`/projects/${projectId}`), "global")) selectProject(null);
  };
  return (
    <header className="topbar">
      <div className="logo"><Icon.logo />Hell Grind</div>
      <select className="project" aria-label="Proje seçici" value={projectId ?? ""} onChange={(e) => selectProject(e.target.value || null)}>
        {projects?.length
          ? projects.map((p) => <option key={p.id} value={p.id}>{p.title} — {PROJECT_STATUS_TR[p.status] || p.status}</option>)
          : <option value="">Henüz proje yok</option>}
      </select>
      <button className="btn" type="button" onClick={() => { setCreating(true); setPage("script"); }}><Icon.plus />Yeni proje</button>
      {current && <button className="btn ghost" type="button" onClick={remove} title="Projeyi kalıcı olarak sil">Projeyi sil</button>}
      <span className="vsep" />
      <Budget />
      <span className="grow" />
      {q && (
        <span className={`chip${q.mode === "real" && q.ready ? " free" : q.mode === "dry" ? "" : " paid"}`}
          title={(q.mode === "dry" ? "Kalite skoru: sahte (ücretsiz mod)"
            : q.ready ? "Kalite skoru: gerçek (OpenCLIP + SFace)" : "Kalite skoru: gerçek skorlayıcı kullanılamıyor") + (q.reason ? ` — ${q.reason}` : "")}>
          {q.mode === "dry" ? "Skor: sahte" : q.ready ? "Skor: gerçek" : "Skor: kullanılamıyor"}
        </span>
      )}
      <button type="button" className={`chip ${meta ? (meta.dry_run ? "free" : "paid") : ""}`} style={{ background: "none", cursor: "pointer" }}
        title={(meta?.dry_run === false ? "Ücretli mod açık: gerçek harcama yapılır" : "Ücretsiz mod: hiçbir ücretli servis çağrılmaz") + " — Ayarlar"}
        onClick={() => setPage("settings")}>
        {!meta ? "Mod yükleniyor…" : meta.dry_run ? "Ücretsiz mod" : "Ücretli mod açık"}
      </button>
      <button type="button" className="btn ghost" aria-current={page === "settings" ? "page" : undefined} onClick={() => setPage("settings")}
        title="Ücretli servisler, mod ve fiyat tablosu"><Icon.gear />Ayarlar</button>
      {ov && (
        <button type="button" className={`kill${frozen ? " resume" : ""}`} onClick={kill}
          title={frozen ? "Kuyruk kaldığı yerden sürer; tamamlanmış klipler yeniden üretilmez" : "Kill-Switch: kuyruktaki işler iptal olur, yeni iş başlamaz"}>
          {frozen ? "Üretime devam et" : "Üretimi durdur"}
        </button>
      )}
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

function PageBar({ hasShots }: { hasShots: boolean }) {
  const { page, setPage, ov } = usePanel();
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
    <nav className="pagebar" aria-label="Sayfalar">
      {PAGES.map((x, i) => {
        const Ico = Icon[x];
        const b = badge[x];
        return (
          <span key={x} style={{ display: "contents" }}>
            {i > 0 && <span className="step" aria-hidden="true">›</span>}
            <button type="button" aria-current={page === x ? "page" : undefined} disabled={NEEDS_SHOTS.has(x) && !hasShots}
              title={`${PAGE_LABEL[x]} (Shift+${i + 1})`} onClick={() => setPage(x)}>
              <Ico />{PAGE_LABEL[x]}
              {b && <span className={`badge ${b.tone || ""}`} title={b.title}>{b.text}</span>}
            </button>
          </span>
        );
      })}
    </nav>
  );
}

export function AppFrame() {
  const { projectId, ov, creating, offline, page, setPage, projects } = usePanel();
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
  const target: Page = page === "settings" && !creating ? "settings"
    : creating || !projectId || (ov && NEEDS_SHOTS.has(page) && !hasShots) ? "script" : page;
  const View = PAGE_VIEW[target];
  return (
    <div className="app">
      <TopBar />
      <Alerts />
      <main className="workspace">
        {loading && target !== "settings" ? <p className="loading">Yükleniyor…</p> : <View key={`${projectId}:${target}`} />}
      </main>
      <PageBar hasShots={hasShots} />
    </div>
  );
}
