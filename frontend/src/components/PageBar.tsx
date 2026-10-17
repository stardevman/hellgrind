"use client";
/* Sayfa menüsü (alt bandın ortası, DaVinci Resolve'un sayfa çubuğu gibi küçük simge + ad). Sayfalar bir menüdür, sıra
   yoktur: her sayfa her an açılır; yalnızca çekim gerektirenler senaryo işlenene dek kapalıdır.
   Simgenin köşesindeki rozet sayfanın özetidir (kilitli varlık, onaylı kare, hazır çekim…), nokta o sayfada bekleyen bir
   iş olduğunu söyler (kırmızı engelleyici, amber uyarı, gri sıradan). Shift+1…9 ve Shift+0 sayfalar arasında geçer. */
import { useMemo } from "react";
import { NEEDS_SHOTS, PAGES, usePanel, type Page } from "@/lib/panel";
import { pageBadges, pageTones, stepReadiness } from "@/lib/steps";
import { Icon } from "./icons";

export const PAGE_LABEL: Record<Page, string> = {
  script: "Senaryo", voice: "Ses", style: "Stil", images: "Görsel", assets: "Varlıklar", board: "Storyboard", edit: "Kurgu", compare: "Karşılaştır",
  color: "Renk", deliver: "Teslim", settings: "Ayarlar", history: "Geçmiş",
};
/** Kullanıcı modunda gizlenen sayfalar (Karşılaştır, Kurgu'daki "Adayları karşılaştır" ile yine açılır). */
export const ADMIN_PAGES: ReadonlySet<Page> = new Set(["compare"]);
const TONE_TR = { bad: "engelleyici iş var", warn: "uyarı var", todo: "bekleyen iş var" } as const;

export function Pages({ hasShots }: { hasShots: boolean }) {
  const { page, setPage, ov, admin } = usePanel();
  const badge = useMemo(() => pageBadges(ov), [ov]);
  const tone = useMemo(() => pageTones(stepReadiness(ov)), [ov]);
  return (
      <nav className="pb-c" aria-label="Sayfalar" data-tour="pages">
        <ul className="pages">
          {PAGES.map((x, i) => {
            if (!admin && ADMIN_PAGES.has(x)) return null;
            const Ico = Icon[x];
            const b = badge[x as keyof typeof badge];
            const t = tone[x as keyof typeof tone];
            const off = NEEDS_SHOTS.has(x) && !hasShots;
            return (
              <li key={x}>
                <button type="button" className="pg" aria-current={page === x ? "page" : undefined} disabled={off}
                  title={off ? `${PAGE_LABEL[x]}: önce senaryo işlenmeli` : `${PAGE_LABEL[x]} (Shift+${(i + 1) % 10})${t ? ` · ${TONE_TR[t]}` : ""}`}
                  onClick={() => setPage(x)}>
                  <span className="pg-i"><Ico />
                    {b && <span className={`badge ${b.tone || ""}`} title={b.title}>{b.text}</span>}
                    {t && !b && <i className={`pdot ${t}`} aria-label={TONE_TR[t]} />}
                  </span>
                  <span className="pg-l">{PAGE_LABEL[x]}</span>
                </button>
              </li>
            );
          })}
        </ul>
      </nav>
  );
}
