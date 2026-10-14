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
  { page: "voice", target: "voice", when: "project", title: "Ses: önce diyalog", text: "Sektörde diyalog animasyondan önce kaydedilir. Replikler sekans ve çekim altında; geçici ses (espeak) ya da final kayıt, ölçülen süre ve çekimdeki yeri yan yana. Fark 0,5 saniyeyi aşarsa amber uyarı çıkar." },
  { page: "voice", target: "voice-upload", when: "project", title: "Toplu ses yükleme", text: "Kayıtları buraya sürükle. S001_SH01_2.wav gibi adlandırılmış dosya ilgili repliğe kendiliğinden eşlenir; eşlenemeyenleri listeden elle ata." },
  { page: "voice", target: "voice-lock", when: "project", title: "Sesi kilitle", text: "Sekansın sesi kilitlenince replik düzenleme kapanır ve henüz üretilmemiş çekimlerin süresi ölçülen sesten hesaplanır (sesten). Kilidi açmak ayrı bir onay ister; ikisi de kürasyon kaydına düşer." },
  { page: "assets", target: "assets", when: "project", title: "Varlıklar", text: "Senaryoda geçen her karakter, mekân ve nesne burada toplanır. Referansı kilitlenmemiş varlık içeren çekim üretime girmez; model eksik bilgiyi uydurur. Kilitlemek için durağan görseli ve hareket testini onayla." },
  { page: "style", target: "style", when: "project", title: "Stil kılavuzu", text: "Işık dili, hareket tarzı ve doku kuralları tek yerden gelir; çekim bazında yazılmaz. Stil değişince hangi çekimlerin yeniden üretileceği önce önizlenir." },
  { page: "style", target: "style-medium", when: "project", title: "Çizim türü: 2D / 3D", text: "Dizinin çizim türü buradan seçilir; video istemindeki stil cümlesi ve karakter, nesne, mekân kuralları buna göre yazılır. Yeni sürümler 2D başlar. Tür değişikliği yeni bir stil sürümüdür: önce etkisini önizle, sonra etkinleştir." },
  { page: "board", target: "board-animatic", when: "project", title: "Animatik", text: "Animatik sekmesi onaylı kareleri geçici ya da final sesle, sesin süresine göre dizer. Toplam süre ve sekans süreleri görünür; ücretsiz modda yerel olarak birleştirilir." },
  { page: "board", target: "board", when: "project", title: "2. Storyboard onayı", text: "Her çekimin bir karesi vardır. Onaylamak ya da değişiklik istemek en ucuz revizyon noktasıdır; onay zorunluysa onaysız çekim üretilmez." },
  { page: "edit", target: "edit-bin", when: "project", title: "3. Kurgu", text: "Soldaki havuz çekimleri listeler, ortadaki izleyici seçili çekimi oynatır. Altındaki şerit aynı çekim için üretilen adaylardır: filmde oynayan \"filmde\" etiketlidir; bir adaya tıklayıp izle, beğenirsen \"Bu adayı filmde kullan\" ya da zaman çizelgesindeki çekimin üstüne sürükle." },
  { page: "edit", target: "inspector", when: "project", title: "Denetçi", text: "Seçili çekimin ayrıntıları sekmelerde: Çekim (durum, adaylar, \"Bunu kullan\"), Ses (replikler, müzik) ve Üretim (motor, yöntem, elle düzeltme). Her eylemden sonra hangi aşamaların çalışacağı yazılır; yalnızca ses değişirse görüntü motoru çağrılmaz." },
  { page: "deliver", target: "deliver", when: "project", title: "4. Teslim", text: "Tamamlanan çekimler tek filme birleşir; kaba kurgu ve final ayrı sekmelerde izlenir. EDL, OpenTimelineIO ve çekim paketi buradan indirilir." },
  { page: "deliver", target: "review-link", when: "project", title: "Müşteri incelemesi", text: "Teslim › Müşteri: her onay kapısının durumu ve revizyon turu burada. \"İnceleme bağlantısı oluştur\" filmi v001, v002 diye etiketler ve süreli, salt okunur bir bağlantı verir; müşteri oynatıcıda zaman kodlu not bırakır, onaylar ya da değişiklik ister. Bütçe ve motor bilgisi o sayfada görünmez." },
  { page: "deliver", target: "delivery-spec", when: "project", title: "Teslim şartnamesi", text: "Teslim › Teslim: çözünürlük, kare hızı, master kodeği, loudness hedefi, altyazı, M&E ve yazısız sürüm. Hazır ayarlar: TV (Türkiye, 25 fps), Web, Platform (yüksek). Final film bir sonraki birleştirmede bu biçimle çıkar." },
  { page: "deliver", target: "qc", when: "project", title: "QC raporu", text: "Siyah kare, donma, loudness, fotosensitif parlama ve çizgi titremesi geçti/kaldı ve zaman koduyla listelenir; PDF olarak indirilir. Kalan kontroller Kurgu zaman çizelgesinde işaretlidir." },
  { page: "deliver", target: "delivery-package", when: "project", title: "Teslim paketi", text: "Paket içeriği önce listelenir: master, izleme kopyası, stem'ler, M&E, altyazı, EDL/OTIO, QC raporu ve hak zinciri. Eksik hak zinciri belgesi uyarır ama paketi engellemez." },
  { page: "edit", target: "client-notes", when: "project", title: "Müşteri notları", text: "Müşterinin notları zaman çizelgesinde ilgili çekimin üstünde işaretçi olarak ve Notlar listesinde görünür; çözdüğün notu işaretle." },
  { target: "client-gates", when: "project", title: "Onay kapıları", text: "Her adımın müşteri onay kapıları (Senaryo, Tasarım, Animatik, Taslak kurgu, Final, Kabul) şeritte ve adımın yanında renkli işaretlerle durur: yeşil onaylandı, kırmızı değişiklik istendi, amber müşteride." },
  { target: "budget", title: "Bütçe ve Kill-Switch", text: "Alt banttaki bütçe göstergesi harcanan tutarı ve limiti gösterir; tıklayınca ayrılan, kalan ve yumuşak sınır açılır. \"Üretimi durdur\" tüm kuyruğu tek tıkla dondurur; devam edince tamamlanmış klipler yeniden üretilmez." },
  { target: "settings", title: "Ayarlar: kullanıcı ve yönetici", text: "Ayarlar'da kullanıcı ya da yönetici modunu seçersin. Kullanıcı modunda her sayfada yalnızca filmi yapmak için gerekenler kalır. Ücretli servisler, fiyat tablosu, servis geçmişi, motor seçimi ve plan ayrıntıları yönetici modundadır; yönetici şifresi tanımlıysa giriş ister." },
  { target: "steps", title: "Hepsi bu", text: "Shift+1…9 sayfalar arasında geçer; adımlar alt bantta, her adımın eksikleri ve sıradaki iş hemen üstündeki şeritte durur." },
];

export const tourStepsFor = (hasProject: boolean): TourStep[] =>
  TOUR_STEPS.filter((s) => !(s.when === "no-project" && hasProject) && !(s.when === "project" && !hasProject));
