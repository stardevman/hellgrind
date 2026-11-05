/* Sayfa grupları (alt bandın ortası). On sayfa beş ana sekmede toplanır: Hikâye (Senaryo, Ses), Görünüm (Stil, Görsel,
   Varlıklar), Storyboard, Kurgu (Kurgu, Karşılaştır) ve Teslim (Renk, Teslim). Birden çok sayfası olan grup açıkken
   sayfaları bandın içinde, sekmenin hemen yanında alt düğmeler olarak açılır. Sayfaların adresi (`?page=`) değişmez; gruplar yalnızca
   menüdür, sıra dayatmaz. Saf fonksiyonlar: tarayıcı gerekmez. */
import type { Page } from "./panel";
import type { PageBadge, PageTone } from "./steps";

export type GroupId = "story" | "look" | "board" | "edit" | "deliver";
/** Sayfa çubuğundaki sayfalar (Ayarlar ve Geçmiş bandın sağındadır). */
export type BarPage = Exclude<Page, "settings" | "history">;
export interface PageGroup {
  id: GroupId;
  label: string;
  /** Simgesi, rozeti ve ilk açılışı grubu temsil eden sayfa. */
  main: BarPage;
  pages: readonly BarPage[];
}

export const GROUPS: readonly PageGroup[] = [
  { id: "story", label: "Hikâye", main: "script", pages: ["script", "voice"] },
  { id: "look", label: "Görünüm", main: "assets", pages: ["style", "images", "assets"] },
  { id: "board", label: "Storyboard", main: "board", pages: ["board"] },
  { id: "edit", label: "Kurgu", main: "edit", pages: ["edit", "compare"] },
  { id: "deliver", label: "Teslim", main: "deliver", pages: ["color", "deliver"] },
];

/** Çekimi olmayan projede açılamayan sayfalar: senaryo sayfasına düşülür. */
export const NEEDS_SHOTS: ReadonlySet<Page> = new Set(["voice", "board", "edit", "compare", "color", "deliver"]);
/** Kullanıcı modunda gizlenen sayfalar (Karşılaştır, Kurgu'daki "Adayları karşılaştır" ile yine açılır). */
export const ADMIN_PAGES: ReadonlySet<Page> = new Set(["compare"]);

export const groupOf = (page: Page): PageGroup | undefined => GROUPS.find((g) => (g.pages as readonly Page[]).includes(page));

export interface Access { admin: boolean; hasShots: boolean }
/** Grupta görünen sayfalar (kullanıcı modunda yönetici sayfaları gizli). */
export const visiblePages = (g: PageGroup, a: Access): BarPage[] => g.pages.filter((p) => a.admin || !ADMIN_PAGES.has(p));
export const pageOpen = (p: Page, a: Access): boolean => !(NEEDS_SHOTS.has(p) && !a.hasShots);

/** Ana sekmeye tıklanınca açılacak sayfa: grupta en son açılan sayfa (açılabiliyorsa), yoksa grubun ana sayfası, o da
    kapalıysa gruptaki ilk açık sayfa. Hiçbiri açılamıyorsa null (sekme kapalı). */
export function groupEntry(g: PageGroup, a: Access, last?: Page): BarPage | null {
  const pages = visiblePages(g, a).filter((p) => pageOpen(p, a));
  const prev = pages.find((p) => p === last);
  if (prev) return prev;
  if (pages.includes(g.main)) return g.main;
  return pages[0] ?? null;
}

const ORDER: PageTone[] = ["todo", "warn", "bad"];
/** Ana sekmenin rozeti ve noktası: rozet ana sayfanınkidir (yoksa gruptaki ilk rozet); nokta rozeti olmayan
    sayfalardaki en ağır eksiktir, böylece rozet kırmızıyı gizlemez. */
export function groupSignal(g: PageGroup, a: Access, badges: Partial<Record<string, PageBadge>>, tones: Partial<Record<string, PageTone>>):
  { badge?: PageBadge; tone?: PageTone } {
  const pages = visiblePages(g, a);
  const from = badges[g.main] ? g.main : pages.find((p) => badges[p]);
  const badge = from ? badges[from] : undefined;
  let tone: PageTone | undefined;
  for (const p of pages) {
    if (p === from) continue;
    const t = tones[p];
    if (t && (!tone || ORDER.indexOf(t) > ORDER.indexOf(tone))) tone = t;
  }
  return { badge, tone };
}

/** Yapılacaklar ve ipuçlarında sayfanın yeri: "Grup › Sayfa"; tek sayfalı ya da sayfayla aynı adlı grupta yalnızca ad. */
export function pagePath(page: Page, label: string): string {
  const g = groupOf(page);
  return g && g.pages.length > 1 && g.label !== label ? `${g.label} › ${label}` : label;
}
