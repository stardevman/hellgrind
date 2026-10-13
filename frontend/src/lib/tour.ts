/* Rehber turu adımları: her adım bir sayfaya ve o sayfadaki `data-tour` hedefine bağlıdır. Hedef bulunamazsa kart
   ekranın ortasında, spot ışığı olmadan gösterilir. Metinler Türkçedir (CLAUDE.md kural 9). */
import type { Page } from "./panel";

export interface TourStep {
  /** Adımın açıldığı sayfa; boşsa sayfa değişmez (alt bant ve şerit her sayfada görünür). */
  page?: Page;
  target: string;
  title: string;
  text: string;
  /** Yalnızca proje yokken ya da yalnızca proje varken gösterilir. */
  when?: "no-project" | "project";
}

export const TOUR_STEPS: readonly TourStep[] = [
  { target: "steps", title: "Hoş geldin", text: "Bir senaryoyu filme dönüştürmek dört adımdır: Hazırlık, Onay, Üretim, Bitirme. Alt banttaki adımlar durumlarını gösterir; hemen üstündeki şeridin sağındaki \"Sıradaki\" bağlantısı seni bir sonraki eksik işe götürür." },
  { target: "stephead", title: "Adım şeridi", text: "Bu şerit bulunduğun adımın sayfalarını ve bu adımda eksik olanları listeler. Bir eksiğe tıklamak onu çözeceğin sayfayı açar; adım tamamlanınca yeşil onay görünür." },
  { target: "project", title: "Proje", text: "Projeler buradan seçilir ve oluşturulur. Her projenin kendi sanal bütçesi vardır; bütçe dolarsa üretim kendiliğinden durur." },
  { page: "script", target: "script-form", when: "no-project", title: "1. Senaryoyu işle", text: "Bir ad, bütçe ve üretim yoğunluğu yeter. \"Örnek senaryo\" düğmesi hazır bir metin yükler; \"Senaryoyu işle ve üret\" senaryoyu çekimlere ayırır ve üretimi başlatır." },
  { page: "script", target: "script-text", title: "Senaryo", text: "Sahne başlıkları (İÇ., DIŞ.), aksiyon paragrafları ve büyük harfli konuşmacı adları çekimlere dönüşür. Biçim kuralları sayfanın sağında durur. Yalnızca değişen çekimler yeniden üretilir." },
  { page: "assets", target: "assets", when: "project", title: "Varlıklar", text: "Senaryoda geçen her karakter, mekân ve nesne burada toplanır. Referansı kilitlenmemiş varlık içeren çekim üretime girmez; model eksik bilgiyi uydurur. Kilitlemek için durağan görseli ve hareket testini onayla." },
  { page: "style", target: "style", when: "project", title: "Stil kılavuzu", text: "Işık dili, hareket tarzı ve doku kuralları tek yerden gelir; çekim bazında yazılmaz. Stil değişince hangi çekimlerin yeniden üretileceği önce önizlenir." },
  { page: "board", target: "board", when: "project", title: "2. Storyboard onayı", text: "Her çekimin bir karesi vardır. Onaylamak ya da değişiklik istemek en ucuz revizyon noktasıdır; onay zorunluysa onaysız çekim üretilmez." },
  { page: "edit", target: "edit-bin", when: "project", title: "3. Kurgu", text: "Soldaki havuz çekimleri listeler, ortadaki izleyici seçili çekimi oynatır. Altındaki şerit aynı çekim için üretilen adaylardır: filmde oynayan \"filmde\" etiketlidir; bir adaya tıklayıp izle, beğenirsen \"Bu adayı filmde kullan\" ya da zaman çizelgesindeki çekimin üstüne sürükle." },
  { page: "edit", target: "inspector", when: "project", title: "Denetçi", text: "Seçili çekimin ayrıntıları sekmelerde: Çekim (durum, adaylar, \"Bunu kullan\"), Ses (replikler, müzik) ve Üretim (motor, yöntem, elle düzeltme). Her eylemden sonra hangi aşamaların çalışacağı yazılır; yalnızca ses değişirse görüntü motoru çağrılmaz." },
  { page: "deliver", target: "deliver", when: "project", title: "4. Teslim", text: "Tamamlanan çekimler tek filme birleşir; kaba kurgu ve final ayrı sekmelerde izlenir. EDL, OpenTimelineIO ve çekim paketi buradan indirilir." },
  { target: "budget", title: "Bütçe ve Kill-Switch", text: "Alt banttaki bütçe göstergesi harcanan tutarı ve limiti gösterir; tıklayınca ayrılan, kalan ve yumuşak sınır açılır. \"Üretimi durdur\" tüm kuyruğu tek tıkla dondurur; devam edince tamamlanmış klipler yeniden üretilmez." },
  { target: "settings", title: "Ayarlar: kullanıcı ve yönetici", text: "Ayarlar'da kullanıcı ya da yönetici modunu seçersin. Kullanıcı modunda her sayfada yalnızca filmi yapmak için gerekenler kalır. Ücretli servisler, fiyat tablosu, servis geçmişi, motor seçimi ve plan ayrıntıları yönetici modundadır; yönetici şifresi tanımlıysa giriş ister." },
  { target: "steps", title: "Hepsi bu", text: "Shift+1…8 sayfalar arasında geçer; adımlar alt bantta, her adımın eksikleri ve sıradaki iş hemen üstündeki şeritte durur." },
];

export const tourStepsFor = (hasProject: boolean): TourStep[] =>
  TOUR_STEPS.filter((s) => !(s.when === "no-project" && hasProject) && !(s.when === "project" && !hasProject));
