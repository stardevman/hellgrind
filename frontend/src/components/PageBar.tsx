"use client";
/* Sayfa menüsü (alt bandın ortası, DaVinci Resolve'un sayfa çubuğu gibi küçük simge + ad). On sayfa beş ana sekmede
   toplanır (lib/groups.ts): Hikâye, Görünüm, Storyboard, Kurgu, Teslim. Birden çok sayfası olan grup açıkken o grubun
   sayfaları bandın içinde, sekmenin hemen yanında alt düğmeler olarak açılır (ör. Hikâye ile Görünüm arasında). Sekmeler bir menüdür, sıra yoktur: her
   sayfa her an açılır; yalnızca çekim gerektirenler senaryo işlenene dek kapalıdır. Ana sekmeye tıklamak grupta en son
   açılan sayfaya döner. Simgenin köşesindeki rozet sayfanın özetidir (kilitli varlık, onaylı kare, hazır çekim…), nokta
   grupta bekleyen bir iş olduğunu söyler (kırmızı engelleyici, amber uyarı, gri sıradan). Shift+1…5 ana sekmeler
   arasında geçer. */
import { Fragment, useEffect, useMemo } from "react";
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
            const pages = visiblePages(g, access);
            const open = current?.id === g.id;
            const what = pages.length > 1 ? `${g.label}: ${pages.map((p) => PAGE_LABEL[p]).join(", ")}` : g.label;
            return (
              <Fragment key={g.id}>
                <li>
                  <button type="button" className="pg" aria-current={open ? "page" : undefined} aria-expanded={pages.length > 1 ? open : undefined}
                    disabled={!target}
                    title={!target ? `${g.label}: önce senaryo işlenmeli` : `${what} (Shift+${i + 1})${sig.tone ? ` · ${TONE_TR[sig.tone]}` : ""}`}
                    onClick={() => { if (target && !open) setPage(target); }}>
                    <span className="pg-i"><Ico />
                      <Signal badge={sig.badge} tone={open && pages.length > 1 ? undefined : sig.tone} />
                    </span>
                    <span className="pg-l">{g.label}</span>
                  </button>
                </li>
                {open && pages.length > 1 && (
                  <li className="pg-sub" data-tour="subpages">
                    <ul aria-label={`${g.label} sayfaları`}>
                      {pages.map((p) => {
                        const SubIco = Icon[p];
                        const off = !pageOpen(p, access);
                        const t = tone[p];
                        return (
                          <li key={p}>
                            <button type="button" className="sp" aria-current={page === p ? "page" : undefined} disabled={off}
                              title={off ? `${PAGE_LABEL[p]}: önce senaryo işlenmeli` : `${PAGE_LABEL[p]}${t ? ` · ${TONE_TR[t]}` : ""}`}
                              onClick={() => setPage(p)}>
                              <span className="pg-i"><SubIco /><Signal badge={badge[p]} tone={t} /></span>
                              <span className="pg-l">{PAGE_LABEL[p]}</span>
                            </button>
                          </li>
                        );
                      })}
                    </ul>
                  </li>
                )}
              </Fragment>
            );
          })}
        </ul>
      </nav>
  );
}

/** Rozet ve nokta: rozet varsa nokta rozetin içinde, sağ ucunda durur (üst üste binmez); yoksa simgenin sağ üst köşesinde. */
function Signal({ badge, tone }: { badge?: PageBadge; tone?: PageTone }) {
  if (badge) return (
    <span className={`badge ${badge.tone || ""}`} title={tone ? `${badge.title} · ${TONE_TR[tone]}` : badge.title}>
      {badge.text}{tone && <i className={`bdot ${tone}`} aria-label={TONE_TR[tone]} />}
    </span>
  );
  return tone ? <i className={`pdot ${tone}`} aria-label={TONE_TR[tone]} /> : null;
}
