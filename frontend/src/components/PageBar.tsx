"use client";
/* Sayfa menüsü (alt bandın ortası). On sayfa beş ana sekmede toplanır (lib/groups.ts): Hikâye, Görünüm, Storyboard,
   Kurgu, Teslim. Sekmeler yerinden oynamaz: simge, grup adı ve altında o grupta açık (ya da en son açılan) sayfa yazar.
   Birden çok sayfası olan sekmeye basınca sayfaları açıklamalarıyla birlikte sekmenin üstünde açılan bir menüde
   listelenir (`GroupMenu`); tek sayfalı sekme doğrudan açar. Bant yüksekliği değişmez. Sekmeler bir menüdür, sıra
   yoktur: her sayfa her an açılır; yalnızca çekim gerektirenler senaryo işlenene dek kapalıdır. Simgenin köşesindeki rozet
   sayfanın özetidir (kilitli varlık, onaylı kare, hazır çekim…), nokta grupta bekleyen bir iş olduğunu söyler (kırmızı
   engelleyici, amber uyarı, gri sıradan). Shift+1…5 ana sekmeler arasında geçer (grupta en son açılan sayfaya). */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { GROUPS, groupEntry, groupOf, groupSignal, pageOpen, visiblePages, type Access, type BarPage, type GroupId, type PageGroup } from "@/lib/groups";
import { usePanel, type Page } from "@/lib/panel";
import { pageBadges, pageTones, stepReadiness, type PageBadge, type PageTone } from "@/lib/steps";
import { Icon } from "./icons";
import { useDismiss } from "./ui";

export const PAGE_LABEL: Record<Page, string> = {
  script: "Senaryo", voice: "Ses", style: "Stil", images: "Görsel", assets: "Varlıklar", board: "Storyboard", edit: "Kurgu", compare: "Karşılaştır",
  color: "Renk", deliver: "Teslim", settings: "Ayarlar", history: "Geçmiş",
};
/** Sekme menüsünde sayfanın ne işe yaradığı (tek satır). */
export const PAGE_DESC: Record<BarPage, string> = {
  script: "Senaryoyu yaz, çekimlere ayır", voice: "Replikler, anlatım ve dış sesler", style: "Işık dili, çizim türü ve palet",
  images: "Varlık görseli üret ve varlığa ata", assets: "Karakter, mekân ve nesne referansları", board: "Kareleri onayla, animatiği izle",
  edit: "Çekim havuzu, izleyici ve zaman çizelgesi", compare: "Adayları yan yana izle", color: "Renk betiği ve renk sıçramaları",
  deliver: "Film, müşteri incelemesi ve teslim paketi",
};
export { ADMIN_PAGES } from "@/lib/groups";
const TONE_TR = { bad: "engelleyici iş var", warn: "uyarı var", todo: "bekleyen iş var" } as const;

/** Her grupta en son açılan sayfa (oturum boyunca): ana sekme oraya döner. */
const lastInGroup: Partial<Record<GroupId, Page>> = {};

/** Ana sekmenin açacağı sayfa (Shift+1…5 kısayolu ve tek sayfalı sekme ortak kullanır). */
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
  const [menu, setMenu] = useState<GroupId | null>(null);
  const box = useRef<HTMLElement>(null);
  const close = useCallback(() => setMenu(null), []);
  useDismiss(menu !== null, close, box);
  useEffect(() => { if (current) lastInGroup[current.id] = page; }, [current, page]);
  useEffect(() => { setMenu(null); }, [page]);
  const go = (p: BarPage) => { setMenu(null); setPage(p); };
  return (
      <nav className="pb-c" aria-label="Sayfalar" data-tour="pages" ref={box}>
        <ul className="pages">
          {GROUPS.map((g, i) => {
            const Ico = Icon[g.main];
            const target = openGroup(g, access);
            const sig = groupSignal(g, access, badge, tone);
            const pages = visiblePages(g, access);
            const multi = pages.length > 1;
            const on = current?.id === g.id;
            const shown = on ? (page as BarPage) : target;
            const isOpen = menu === g.id;
            const what = multi ? `${g.label}: ${pages.map((p) => PAGE_LABEL[p]).join(", ")}` : g.label;
            return (
              <li key={g.id} className="pg-cell">
                {isOpen && <GroupMenu group={g} n={i + 1} pages={pages} access={access} current={page} badge={badge} tone={tone} onPick={go} />}
                <button type="button" className="pg" aria-current={on ? "page" : undefined} disabled={!target}
                  aria-expanded={multi ? isOpen : undefined} aria-controls={multi ? `pg-menu-${g.id}` : undefined}
                  title={!target ? `${g.label}: önce senaryo işlenmeli` : `${what} (Shift+${i + 1})${sig.tone ? ` · ${TONE_TR[sig.tone]}` : ""}`}
                  onClick={() => { if (!target) return; if (multi) setMenu(isOpen ? null : g.id); else go(target); }}>
                  <span className="pg-i"><Ico />{!sig.badge && <Signal tone={sig.tone} />}</span>
                  <span className="pg-t">
                    <span className="pg-l">{g.label}{sig.badge && <Signal badge={sig.badge} tone={sig.tone} />}</span>
                    {multi && shown && <span className="pg-s">{PAGE_LABEL[shown]}<Icon.chevron /></span>}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      </nav>
  );
}

/** Sekmenin üstünde açılan menü: grubun sayfaları, ne işe yaradıkları, rozet ve noktalarıyla. Açılınca odak açık sayfaya
    (yoksa ilk sayfaya) gider; Esc ya da dışarı tıklama kapatır, sayfa seçilince menü kapanır. */
function GroupMenu({ group, n, pages, access, current, badge, tone, onPick }: {
  group: PageGroup; n: number; pages: BarPage[]; access: Access; current: Page;
  badge: Partial<Record<string, PageBadge>>; tone: Partial<Record<string, PageTone>>; onPick: (p: BarPage) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = box.current?.querySelector<HTMLButtonElement>('button[aria-current="page"]:not(:disabled)')
      ?? box.current?.querySelector<HTMLButtonElement>("button:not(:disabled)");
    el?.focus();
  }, []);
  return (
    <div className="pg-pop" id={`pg-menu-${group.id}`} ref={box} role="group" aria-label={`${group.label} sayfaları`} data-tour="page-menu">
      <div className="pg-pop-h">{group.label}<kbd>Shift+{n}</kbd></div>
      {pages.map((p) => {
        const Ico = Icon[p];
        const off = !pageOpen(p, access);
        return (
          <button key={p} type="button" className="pg-row" aria-current={current === p ? "page" : undefined} disabled={off}
            title={off ? `${PAGE_LABEL[p]}: önce senaryo işlenmeli` : undefined} onClick={() => onPick(p)}>
            <Ico />
            <span className="pg-row-t"><b>{PAGE_LABEL[p]}</b><span>{off ? "Önce senaryo işlenmeli" : PAGE_DESC[p]}</span></span>
            <Signal badge={badge[p]} tone={tone[p]} inline />
          </button>
        );
      })}
    </div>
  );
}

/** Rozet ve nokta: rozet varsa nokta rozetin içinde, sağ ucunda durur (üst üste binmez). Sekmede rozet grup adının
    yanında, rozetsiz nokta simgenin sağ üst köşesindedir. */
function Signal({ badge, tone, inline }: { badge?: PageBadge; tone?: PageTone; inline?: boolean }) {
  if (badge) return (
    <span className={`badge ${badge.tone || ""}${inline ? " in" : ""}`} title={tone ? `${badge.title} · ${TONE_TR[tone]}` : badge.title}>
      {badge.text}{tone && <i className={`bdot ${tone}`} aria-label={TONE_TR[tone]} />}
    </span>
  );
  return tone ? <i className={`pdot ${tone}${inline ? " in" : ""}`} aria-label={TONE_TR[tone]} /> : null;
}
