/* Tasarım sözleşmesi ve belgelenmiş Türkçe metinler (CLAUDE.md, Faz 9 ve Bölüm B). Tarayıcı gerektirmez. */
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const root = join(__dirname, "..", "src");
const CSS = readFileSync(join(root, "app", "globals.css"), "utf8");
const LAYOUT = readFileSync(join(root, "app", "layout.tsx"), "utf8");
const SOURCES = readdirSync(join(root, "components")).map((f) => readFileSync(join(root, "components", f), "utf8")).join("\n")
  + readFileSync(join(root, "lib", "text.ts"), "utf8");

describe("design contract", () => {
  it.each([["slag", "2A2D31"], ["plate", "33373C"], ["ash", "E6E1D8"], ["dust", "A39E95"], ["ember", "E4A23E"],
    ["verdigris", "62AE8E"], ["oxide", "C2573F"], ["steel", "86A5B8"]])("palette token %s", (name, hex) => {
    expect(CSS).toMatch(new RegExp(`--${name}:\\s*#${hex}`, "i"));
  });

  it("uses Archivo with the width axis", () => {
    expect(LAYOUT).toContain("family=Archivo:wdth,wght@");
    expect(CSS).toContain("font-stretch");
    expect(CSS).toContain('"Archivo"');
  });

  it("never uses the font shorthand for real values (the minifier merged font-stretch into an invalid one)", () => {
    expect(CSS).not.toMatch(/\bfont:(?!\s*inherit)/);
  });

  it("has no uppercase labels or small caps", () => {
    expect(CSS).not.toMatch(/text-transform|small-caps|uppercase/i);
  });

  it("keeps keyboard focus visible, honours reduced motion and collapses to one column on phones", () => {
    expect(CSS).toContain(":focus-visible");
    expect(CSS).toMatch(/@media \(prefers-reduced-motion: reduce\)[^}]*\{[^}]*animation: none/);
    const mobile = CSS.slice(CSS.indexOf("@media (max-width: 960px)"));
    expect(mobile).toContain("grid-template-columns: 1fr");
    expect(LAYOUT).toContain('lang="tr"');
  });

  it("styles every film strip state", () => {
    for (const s of ["refined", "rejected", "retired", "working", "failed", "selected", "tier-start"]) expect(CSS).toContain(`.tile.${s}`);
  });
});

describe("documented Turkish texts", () => {
  it.each([
    // Faz 9
    "Ücretsiz mod: hiçbir ücretli servis çağrılmaz", "Yeni proje", "Üretimi durdur", "Üretime devam et", "Senaryoyu işle ve üret",
    "Bunu kullan", "Müziği değiştir", "Bu sahneyi baştan üret", "Rafine havuz", "Çekim bilgileri", "Diyalog", "Olay akışı",
    "sahneyi birleştir", "Sonuca dön",
    // Faz 11–17
    "Sekansın tamamı", "Yalnızca bu çekim", "Bütçe tavanı", "Tavanı kaydet", "Stem yükle", "Tavan doldu", "Sekanssız",
    "Durağanı onayla", "Hareket testi üret", "Hareket testini onayla", "Kilitle", "Kilidi aç", "Yerel taslak üret",
    "Eksik referans", "Engellenen çekimleri yeniden dene", "Nesne bağla",
    "Işık öneki", "Hareket tarzı", "Yeni sürüm olarak kaydet", "Etkisini önizle", "Etkinleştir", "Etkilenen çekimleri hemen yeniden üret",
    "Örnekle doldur", "Kareleri hazırla", "PDF indir", "Onayla", "Değişiklik iste", "Taslak geçişi başlat", "İyileştirme geçişi",
    "İyileştir", "Kaba kurgu", "Final", "kare onayı zorunlu", "Previs rehberi yükle", "Dış çekim yükle", "Elle düzeltme",
    "Revizyon yükle", "Orijinale dön", "Düzenlenmiş ilk kare", "Sürekliliği denetle", "Öneriyi uygula", "Rengi uygula",
    "Kabul edilen çekim başına varyasyon", "Film dakikası başına sanal maliyet", "Eleme nedenleri", "Son kararlar",
    "Profesyonel dışa aktarma", "EDL (CMX3600)", "OpenTimelineIO", "Çekim paketi (zip)", "Kürasyon kaydını indir",
    // Karşılaştırma
    "Karşılaştır", "Adayları karşılaştır", "Perde (1 ↔ 2)", "Bir kare geri", "Denetçide aç",
  ])("%s", (text) => expect(SOURCES).toContain(text));
});
