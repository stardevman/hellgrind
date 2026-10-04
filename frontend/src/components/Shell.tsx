"use client";
import { useRef, type KeyboardEvent } from "react";
import { api } from "@/lib/api";
import { PROJECT_STATUS_TR } from "@/lib/i18n";
import { TABS, usePanel, type Tab } from "@/lib/panel";
import { usd } from "@/lib/text";
import { AssetsPane } from "./AssetsPane";
import { BoardPane } from "./BoardPane";
import { ComparePane } from "./ComparePane";
import { EfficiencyPane } from "./EfficiencyPane";
import { ScenesPane } from "./ScenesPane";
import { StartView } from "./StartView";
import { StylePane } from "./StylePane";

function TopBar() {
  const { projects, projectId, selectProject, setCreating, meta } = usePanel();
  const q = meta?.quality;
  return (
    <header className="topbar">
      <h1 className="brand">Hell Grind</h1>
      <select aria-label="Proje seçici" value={projectId ?? ""} onChange={(e) => selectProject(e.target.value || null)}>
        {projects?.length
          ? projects.map((p) => <option key={p.id} value={p.id}>{p.title} — {PROJECT_STATUS_TR[p.status] || p.status}</option>)
          : <option value="">Henüz proje yok</option>}
      </select>
      <button className="btn" type="button" onClick={() => setCreating(true)}>Yeni proje</button>
      <a className="btn small" href="/panel-klasik/" title="Önceki tek sayfalık panel (geçiş süresince)">Klasik panel</a>
      <span className="spacer" />
      {q && (
        <span className={`chip${q.mode === "real" && q.ready ? " free" : q.mode === "dry" ? "" : " paid"}`} title={q.reason || ""}>
          {q.mode === "dry" ? "Kalite skoru: sahte (ücretsiz mod)"
            : q.ready ? "Kalite skoru: gerçek (OpenCLIP + SFace)" : "Kalite skoru: gerçek skorlayıcı kullanılamıyor"}
        </span>
      )}
      <span className={`chip ${meta ? (meta.dry_run ? "free" : "paid") : ""}`}>
        {!meta ? "Mod yükleniyor…" : meta.dry_run ? "Ücretsiz mod: hiçbir ücretli servis çağrılmaz" : "Ücretli mod açık: gerçek harcama yapılır"}
      </span>
    </header>
  );
}

function BudgetRail() {
  const { ov, projectId, act } = usePanel();
  if (!ov) return null;
  const p = ov.project, limit = p.budget_limit_usd || 1;
  const left = Math.max(0, limit - p.spent_usd - p.reserved_usd);
  const pctOf = (x: number) => `${Math.min(100, (x / limit) * 100).toFixed(2)}%`;
  const virtual = p.dry_run ? " (sanal)" : "";
  const frozenEvent = ov.events.find((e) => e.kind === "frozen") as { reason?: string } | undefined;
  return (
    <>
      <section className="rail" aria-label="Bütçe">
        <div className="figures">
          <div className="fig"><b className="num">{usd(p.spent_usd)}</b><span>Harcanan{virtual}</span></div>
          <div className="fig"><b className="num">{usd(p.reserved_usd)}</b><span>Ayrılan</span></div>
          <div className="fig"><b className="num">{usd(left)}</b><span>Kalan</span></div>
          <div className="fig"><b className="num">{usd(p.budget_limit_usd)}</b><span>Limit</span></div>
        </div>
        <div className="actions">
          <button className="btn danger" type="button" disabled={p.frozen}
            onClick={() => act(() => api.post(`/projects/${projectId}/killswitch`, { action: "freeze", reason: "panelden durduruldu" }), "film")}>
            Üretimi durdur
          </button>
          <button className="btn good" type="button" disabled={!p.frozen}
            onClick={() => act(() => api.post(`/projects/${projectId}/killswitch`, { action: "resume" }), "film")}>
            Üretime devam et
          </button>
        </div>
        <div className="meter" role="img" aria-label={`Bütçenin yüzde ${Math.round((p.spent_usd / limit) * 100)} kadarı harcandı`}>
          <div className="spent" style={{ width: pctOf(p.spent_usd) }} />
          <div className="reserved" style={{ left: pctOf(p.spent_usd), width: pctOf(p.reserved_usd) }} />
          <div className="soft" style={{ left: `${(p.settings.soft_budget_ratio * 100).toFixed(1)}%` }} title="Yumuşak limit" />
        </div>
      </section>
      {p.frozen && (
        <div className="banner frozen" role="alert">
          Üretim durduruldu{frozenEvent?.reason ? ` (${frozenEvent.reason})` : ""}. Sahneler donduruldu; devam edene kadar yeni iş başlamaz.
        </div>
      )}
      {p.soft_limited && !p.frozen && (
        <div className="banner soft">Yumuşak bütçe sınırı aşıldı: yeni tam dalga başlatılmıyor, sahneler eldeki adaylarla tamamlanıyor.</div>
      )}
    </>
  );
}

const TAB_LABEL: Record<Tab, string> = {
  scenes: "Sahneler", compare: "Karşılaştır", assets: "Varlıklar", board: "Storyboard", style: "Stil kılavuzu", efficiency: "Verimlilik",
};

function Tabs() {
  const { tab, setTab, ov } = usePanel();
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});
  const c = ov?.project.asset_counts ?? {};
  const total = Object.values(c).reduce((x, y) => x + y, 0);
  const approved = ov?.scenes.filter((x) => x.board_status === "approved").length ?? 0;
  const counts: Partial<Record<Tab, string>> = {
    assets: total ? `${c.locked || 0}/${total} kilitli` : "",
    board: ov?.scenes.length ? `${approved}/${ov.scenes.length} onaylı` : "",
    style: ov?.project.style_version ? `sürüm ${ov.project.style_version}` : "",
  };
  const onKey = (e: KeyboardEvent) => {                     // sekmeler arasında ok tuşlarıyla gezinme
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const next = TABS[(TABS.indexOf(tab) + (e.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length];
    setTab(next);
    refs.current[next]?.focus();
  };
  return (
    <nav className="tabs" role="tablist" aria-label="Proje görünümü" onKeyDown={onKey}>
      {TABS.map((t) => (
        <button key={t} ref={(el) => { refs.current[t] = el; }} className="tab" role="tab" id={`tab-${t}`} type="button"
          aria-controls={`pane-${t}`} aria-selected={tab === t} tabIndex={tab === t ? 0 : -1} onClick={() => setTab(t)}>
          {TAB_LABEL[t]}{counts[t] ? <> <span className="num muted">{counts[t]}</span></> : null}
        </button>
      ))}
    </nav>
  );
}

const PANES: Record<Tab, () => React.ReactElement | null> = {
  scenes: ScenesPane, compare: ComparePane, assets: AssetsPane, board: BoardPane, style: StylePane, efficiency: EfficiencyPane,
};

export function Shell() {
  const { projectId, ov, creating, offline, tab, projects } = usePanel();
  const loading = (!projects && !offline) || (projectId && !ov && !creating && !offline);
  const start = creating || !projectId || (ov && ov.scenes.length === 0);
  const Pane = PANES[tab];
  return (
    <>
      <TopBar />
      {projectId && ov && !creating && <BudgetRail />}
      {offline && <div className="banner offline" role="status">Sunucuya ulaşılamıyor; yeniden deniyorum…</div>}
      <main>
        {loading ? <p className="loading">Yükleniyor…</p>
          : start ? <StartView />
          : (
            <>
              <Tabs />
              <div id={`pane-${tab}`} role="tabpanel" aria-labelledby={`tab-${tab}`}><Pane key={projectId} /></div>
            </>
          )}
      </main>
    </>
  );
}
