/* Stüdyo arayüzünün tasarım sözleşmesi ve belgelenmiş Türkçe metinler (CLAUDE.md). Tarayıcı gerektirmez. */
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const root = join(__dirname, "..", "src");
const CSS = readFileSync(join(root, "app", "globals.css"), "utf8");
const LAYOUT = readFileSync(join(root, "app", "layout.tsx"), "utf8");
const SOURCES = readdirSync(join(root, "components")).map((f) => readFileSync(join(root, "components", f), "utf8")).join("\n")
  + readFileSync(join(root, "lib", "text.ts"), "utf8") + readFileSync(join(root, "lib", "steps.ts"), "utf8") + readFileSync(join(root, "lib", "tour.ts"), "utf8")
  + readFileSync(join(root, "lib", "voice.ts"), "utf8") + readFileSync(join(root, "lib", "client.ts"), "utf8")
  + readFileSync(join(root, "lib", "delivery.ts"), "utf8") + readFileSync(join(root, "lib", "safe.ts"), "utf8");

describe("design contract", () => {
  it.each([["bg", "19191c"], ["panel", "232327"], ["raised", "2b2b30"], ["line", "3a3a41"], ["text", "d9d9dd"],
    ["accent", "e8783a"], ["playhead", "ff4a4a"], ["ok", "3dcc91"], ["warn", "f2c744"], ["bad", "ff5c55"], ["work", "a88bff"]])("palette token %s", (name, hex) => {
    expect(CSS).toMatch(new RegExp(`--${name}:\\s*#${hex}`, "i"));
  });

  it("uses Inter for the interface and JetBrains Mono for timecodes", () => {
    expect(LAYOUT).toContain("family=Inter:");
    expect(LAYOUT).toContain("family=JetBrains+Mono:");
    expect(CSS).toMatch(/--sans:\s*"Inter"/);
    expect(CSS).toMatch(/--mono:\s*"JetBrains Mono"/);
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
    const mobile = CSS.slice(CSS.indexOf("@media (max-width: 1100px)"));
    expect(mobile).toContain("grid-template-columns: 1fr");
    expect(LAYOUT).toContain('lang="tr"');
  });

  it("styles every film strip state", () => {
    for (const s of ["refined", "rejected", "retired", "working", "failed", "selected", "compare-pick"]) expect(CSS).toContain(`.tile.${s}`);
    expect(CSS).toContain(".strip .gap");                 // test turu ile tam tur arasındaki ayraç
  });

  it("keeps every control in one bottom dock with small page icons and upward menus; the old step rail is gone", () => {
    expect(CSS).toMatch(/\.app \{[^}]*grid-template-rows: minmax\(0, 1fr\) auto/);
    expect(CSS).toMatch(/\.dock \.menu \{[^}]*bottom: calc\(100% \+ 6px\)/);
    expect(CSS).toMatch(/\.pg-i svg \{ width: 16px/);
    expect(CSS).not.toContain(".topbar");
    for (const sel of [".dock", ".pagebar", ".pb-l", ".pb-c", ".pb-r", ".pb-btn", ".pg[aria-current=\"page\"]", ".pg:disabled", ".pg .badge", ".pg .pdot",
      ".todos-btn", ".todos-pop", ".td-item", ".td-ok", ".gchip", ".pm-card", ".pm-card.new", ".budget-chip",
      'body[data-admin="0"] .adv', ".modal", ".gate-card", ".tour-spot", ".tour-card"]) expect(CSS).toContain(sel);
    for (const sel of [".stp", ".stephead", ".sh-tabs", ".nextup", ".appbar"]) expect(CSS).not.toContain(sel);
  });
});

describe("documented Turkish texts", () => {
  it.each([
    // Faz 9
    "Ücretsiz mod: hiçbir ücretli servis çağrılmaz", "Yeni proje", "Üretimi durdur", "Üretime devam et", "Senaryoyu işle ve üret",
    "Bunu kullan", "Filmde kullan", "elenen adayı göster", "çekimi tamamla", "Müziği değiştir", "Bu sahneyi baştan üret", "Rafine havuz", "Diyalog", "Olay akışı",
    "sahneyi birleştir", "Sonuca dön",
    // Faz 11–17
    "Sekansın tamamı", "Yalnızca bu çekim", "Bütçe tavanı", "Tavanı kaydet", "Stem yükle", "Tavan doldu", "Sekanssız",
    "Durağanı onayla", "Hareket testi üret", "Hareket testini onayla", "Kilitle", "Kilidi aç", "Yerel taslak üret",
    "Eksik referans", "Engellenen çekimleri yeniden dene", "Nesne bağla",
    "Işık öneki", "Hareket tarzı", "Yeni sürüm olarak kaydet", "Etkisini önizle", "Etkinleştir", "Etkilenen çekimleri hemen yeniden üret",
    "Örnekle doldur", "Kareleri hazırla", "PDF indir", "Onayla", "Değişiklik iste", "Notu kaydet", "Taslak geçişi başlat", "İyileştirme geçişi",
    "İyileştir", "Kaba kurgu", "Final", "kare onayı zorunlu", "Previs rehberi yükle", "Dış çekim yükle", "Elle düzeltme",
    "Revizyon yükle", "Orijinale dön", "Düzenlenmiş ilk kare", "Sürekliliği denetle", "Öneriyi uygula", "Öneriyi dene", "Geçişi göster", "Rengi uygula",
    "Kabul edilen çekim başına varyasyon", "Film dakikası başına", "sanal maliyet", "gerçek harcama", "Eleme nedenleri", "Son kararlar",
    "Profesyonel dışa aktarma", "EDL (CMX3600)", "OpenTimelineIO", "Çekim paketi (zip)", "Kürasyon kaydını indir",
    // Karşılaştırma
    "Karşılaştır", "Adayları karşılaştır", "Perde (1 ↔ 2)", "Bir kare geri", "Denetçide aç",
    // Stüdyo sayfaları ve kalite skoru
    "Senaryo", "Stil", "Varlıklar", "Storyboard", "Kurgu", "Renk", "Teslim", "Zaman çizelgesi", "İzleyici", "Klip şeridi",
    // Sayfa çubuğu, Yapılacaklar, Proje yöneticisi, görünüm anahtarı ve rehber turu
    "Proje yöneticisi", "Yeni film", "Yeni dizi", "Yeni bölüm", "Bağımsız filmler", "Henüz proje yok.", "Yapılacaklar", "Her şey tamam", "Müşteri onayları", "önce senaryo işlenmeli",
    "Kullanıcı", "Yönetici", "Yönetici girişi", "Atla", "İleri", "Bitir",
    "Kalite skoru: gerçek (OpenCLIP + SFace)", "yüzü",
    // Ayarlar (ücretli servisler)
    "Ayarlar", "Çalışma modu", "Ücretli moda geç", "Ücretsiz moda dön", "Bağlantıyı sına", "Anahtarı kaydet", "Hesap paneli",
    "Model ve uç noktalar", "Fiyat tablosu", "Doğrula ve kaydet", "Değişiklik kaydı",
    "MiniMax H3 denemesi", "Deneme senaryosu", "Yalnızca MiniMax H3 (", "RunPod uç noktası", "gerekmez",
    "MiniMax H3 · vast.ai", "yalnızca seçtiğiniz çekimlerde", "Video motoru", "makinenin boşta geçen süresi",
    // Animasyon dizisi: çizim türü ve kare tutma
    "Çizim türü", "On twos (12 çizim/sn görünümü)", "Kareler", "her çizim iki kare",
    // Ses-önce akışı
    "Ses", "Geçici", "Final", "Sesi kilitle", "Kilidi aç", "Kilitli", "sesten", "Animatik", "Kayıtları buraya sürükle",
    "Replik süresi çekimden belirgin farklı; görüntünün yeniden üretilmesi gerekebilir.", "Ses referansı", "Dudak senkronu",
    // Müşteri onay döngüsü
    "Müşteri", "İnceleme bağlantısı oluştur", "Müşteri onayladı", "Değişiklik istedi", "Sürümler", "İnceleme bağlantıları", "Notlar",
    "Değişiklik iste", "Notu ekle", "Karar", "Revizyon",
    // Teslim şartnamesi, QC ve hak zinciri
    "Teslim şartnamesi", "TV (Türkiye, 25 fps)", "Platform (yüksek)", "Şartnameyi kaydet", "QC raporu", "QC çalıştır", "Paket içeriği",
    "Paketi oluştur", "Hak zinciri", "Altyazı (SRT)", "M&amp;E mix", "Yazısız sürüm",
    // 2D yayın güvenliği
    "Güvenli alanlar", "action safe %93", "title safe %90", "Boy sıralaması", "Renk betiği", "Hedef",
  ])("%s", (text) => expect(SOURCES).toContain(text));
});
