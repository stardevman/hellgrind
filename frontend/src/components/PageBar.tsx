"use client";
/* Sayfa menüsü (alt bandın ortası, DaVinci Resolve'un sayfa çubuğu gibi küçük simge + ad). On sayfa beş ana sekmede
   toplanır (lib/groups.ts): Hikâye, Görünüm, Storyboard, Kurgu, Teslim. Birden çok sayfası olan grup açıkken bandın hemen
   üstünde ince bir alt sekme şeridi (`SubPages`) o grubun sayfalarını gösterir. Sekmeler bir menüdür, sıra yoktur: her
   sayfa her an açılır; yalnızca çekim gerektirenler senaryo işlenene dek kapalıdır. Ana sekmeye tıklamak grupta en son
   açılan sayfaya döner. Simgenin köşesindeki rozet sayfanın özetidir (kilitli varlık, onaylı kare, hazır çekim…), nokta
   grupta bekleyen bir iş olduğunu söyler (kırmızı engelleyici, amber uyarı, gri sıradan). Shift+1…5 ana sekmeler
   arasında geçer. */
import { useEffect, useMemo } from "react";
import { GROUPS, groupEntry, groupOf, groupSignal, pageOpen, visiblePages, type Access, type BarPage, type GroupId, type PageGroup } from "@/lib/groups";
import { usePanel, type Page } from "@/lib/panel";
import { pageBadges, pageTones, stepReadiness, type PageBadge, type PageTone } from "@/lib/steps";
import { Icon } from "./icons";

export const PAGE_LABEL: Record<Page, string> = {
  script: "Senaryo", voice: "Ses", style: "Stil", images: "Görsel", assets: "Varlıklar", board: "Storyboard", edit: "Kurgu", compare: "Karşılaştır",
  color: "Renk", deliver: "Teslim", settings: "Ayarlar", history: "Geçmiş",
};
export { ADMIN_PAGES } from "@/lib/groups";
const TONE_TR = { bad: "engelleyici iş var", warn: "uyarı var", todo: "bekleyen iş var" } as const;

/** Her grupta en son açılan sayfa (oturum boyunca): ana sekme oraya döner. */
const lastInGroup: Partial<Record<GroupId, Page>> = {};

/** Ana sekmenin açacağı sayfa (tıklama ve Shift+1…5 kısayolu ortak kullanır). */
export const openGroup = (g: PageGroup, a: Access): BarPage | null => groupEntry(g, a, lastInGroup[g.id]);

function useSignals() {
  const { ov } = usePanel();
  const badge = useMemo(() => pageBadges(ov), [ov]);
  const tone = useMemo(() => pageTones(stepReadiness(ov)), [ov]);
  return { badge: badge as Partial<Record<string, PageBadge>>, tone: tone as Partial<Record<string, PageTone>> };
}

export function Pages({ hasShots }: { hasShots: boolean }) {
  const { page, setPage, admin } = usePanel();
  const { badge, tone } = useSignals();
  const access: Access = { admin, hasShots };
  const current = groupOf(page);
  useEffect(() => { if (current) lastInGroup[current.id] = page; }, [current, page]);
  return (
      <nav className="pb-c" aria-label="Sayfalar" data-tour="pages">
        <ul className="pages">
          {GROUPS.map((g, i) => {
            const Ico = Icon[g.main];
            const target = openGroup(g, access);
            const sig = groupSignal(g, access, badge, tone);
            const names = visiblePages(g, access).map((p) => PAGE_LABEL[p]);
            const what = names.length > 1 ? `${g.label}: ${names.join(", ")}` : g.label;
            return (
              <li key={g.id}>
                <button type="button" className="pg" aria-current={current?.id === g.id ? "page" : undefined} disabled={!target}
                  title={!target ? `${g.label}: önce senaryo işlenmeli` : `${what} (Shift+${i + 1})${sig.tone ? ` · ${TONE_TR[sig.tone]}` : ""}`}
                  onClick={() => { if (target && current?.id !== g.id) setPage(target); }}>
                  <span className="pg-i"><Ico />
                    {sig.badge && <span className={`badge ${sig.badge.tone || ""}`} title={sig.badge.title}>{sig.badge.text}</span>}
                    {sig.tone && <i className={`pdot ${sig.tone}`} aria-label={TONE_TR[sig.tone]} />}
                  </span>
                  <span className="pg-l">{g.label}</span>
                </button>
              </li>
            );
          })}
        </ul>
      </nav>
  );
}

/** Alt sekme şeridi: açık grubun sayfaları (grupta tek görünür sayfa varsa gösterilmez). */
export function SubPages({ hasShots }: { hasShots: boolean }) {
  const { page, setPage, admin } = usePanel();
  const { badge, tone } = useSignals();
  const access: Access = { admin, hasShots };
  const g = groupOf(page);
  const pages = g ? visiblePages(g, access) : [];
  if (!g || pages.length < 2) return null;
  return (
    <nav className="subpages" aria-label={`${g.label} sayfaları`} data-tour="subpages">
      <ul>
        {pages.map((p) => {
          const Ico = Icon[p];
          const off = !pageOpen(p, access);
          const b = badge[p];
          const t = tone[p];
          return (
            <li key={p}>
              <button type="button" className="sp" aria-current={page === p ? "page" : undefined} disabled={off}
                title={off ? `${PAGE_LABEL[p]}: önce senaryo işlenmeli` : `${PAGE_LABEL[p]}${t ? ` · ${TONE_TR[t]}` : ""}`}
                onClick={() => setPage(p)}>
                <Ico /><span>{PAGE_LABEL[p]}</span>
                {b && <span className={`badge ${b.tone || ""}`} title={b.title}>{b.text}</span>}
                {t && !b && <i className={`pdot ${t}`} aria-label={TONE_TR[t]} />}
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
