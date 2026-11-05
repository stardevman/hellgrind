/* Rehberler: her adım bir sayfaya ve o sayfadaki `data-tour` hedefine bağlıdır. Hedef bulunamazsa önce `fallback`,
   o da yoksa kart ekranın ortasında, spot ışığı olmadan gösterilir. İki rehber var: panel turu (TOUR_STEPS, ilk
   açılışta) ve kitaptan çizgi film (BOOK_STEPS, alt banttaki Rehberler menüsünden). Kitap rehberi uygulamayı kullanırken
   açık kalır: vurgulanan alanda iş yapılır, kart her adımın gerçek durumunu (yapıldı / bekliyor) gösterir. Metinler
   Türkçedir (CLAUDE.md kural 9). */
import type { Page, TourId } from "./panel";
import type { Meta, Overview, Scene } from "./types";

/** Adımın durumunu okumak için panelin o anki verisi. */
export interface TourCtx { ov?: Overview; meta?: Meta; admin: boolean }

export interface TourStep {
  /** Adımın açıldığı sayfa; boşsa sayfa değişmez (alt bant her sayfada görünür). */
  page?: Page;
  target: string;
  /** Hedef yoksa (ör. o an görünmeyen bir düğme) vurgulanacak yedek hedef. */
  fallback?: string;
  /** Ölçmeden önce tıklanacak data-tour öğesi (ör. denetçinin sekmesi, Ayarlar'daki bölüm). */
  click?: string;
  /** Denetçi gereken adım: çekim seçili değilse ilk çekim seçilir, denetçi kapalıysa açılır. */
  scene?: boolean;
  title: string;
  text: string;
  /** Yalnızca proje yokken ya da yalnızca proje varken gösterilir. */
  when?: "no-project" | "project";
  /** Yönetici modu gerekir (kartta belirtilir). */
  admin?: boolean;
  /** Adımın gerçek durumu: true yapıldı, false bekliyor, null bu adımda ölçülmez. */
  check?: (c: TourCtx) => boolean | null;
}

export const TOUR_STEPS: readonly TourStep[] = [
  { target: "pages", title: "Hoş geldin", text: "Alt bandın ortasındaki beş sekme bir menüdür: Hikâye (Senaryo, Ses), Görünüm (Stil, Görsel, Varlıklar), Storyboard, Kurgu ve Teslim (Renk, Teslim). Sıra zorunlu değildir; her sayfa her an açılır, yalnızca çekim gerektirenler senaryo işlenene dek kapalıdır. Simgenin köşesindeki rozet özetidir, nokta orada bekleyen bir iş olduğunu söyler." },
  { page: "script", target: "subpages", title: "Alt sekmeler", text: "Birden çok sayfası olan sekme açıkken sayfalarını bandın hemen üstündeki ince şerit gösterir. Ana sekmeye tıklamak o grupta en son açtığın sayfaya döner." },
  { target: "home", title: "Proje yöneticisi", text: "Alt bandın solundaki ev simgesi ya da proje adı Proje yöneticisini açar: diziler sezon sezon, bağımsız filmler, \"Yeni film\" ve \"Yeni dizi\". Bir karta tıklamak o projeyi açar; hiç proje yokken bu ekran kendiliğinden gelir." },
  { target: "todos", title: "Yapılacaklar", text: "Alt banttaki Yapılacaklar düğmesi eksik işleri çözüldükleri sayfaya göre listeler: kırmızı engelleyici, amber uyarı. Bir maddeye tıklamak o sayfayı açar; bekleyen iş kalmayınca yeşil onay görünür." },
  { target: "project", title: "Dizi, sezon ve bölüm", text: "Kırıntı yolu Dizi › Sezon › Bölüm biçimindedir: dizi adı Proje yöneticisini, sezon ve bölüm parçaları kendi menülerini açar (sezonlar, numaralı bölümler, \"Yeni bölüm\"). Bölüm bir projedir: kendi sanal bütçesi, onay kapıları ve teslimi vardır. Bağımsız film tek parça görünür." },
  { page: "script", target: "script-form", when: "no-project", title: "1. Senaryoyu işle", text: "Bir ad, bütçe ve üretim yoğunluğu yeter. \"Örnek senaryo\" düğmesi hazır bir metin yükler; \"Senaryoyu işle ve üret\" senaryoyu çekimlere ayırır ve üretimi başlatır." },
  { page: "script", target: "script-text", title: "Senaryo", text: "Sahne başlıkları (İÇ., DIŞ.), aksiyon paragrafları ve büyük harfli konuşmacı adları çekimlere dönüşür. Biçim kuralları sayfanın sağında durur. Yalnızca değişen çekimler yeniden üretilir." },
  { page: "voice", target: "voice", when: "project", title: "Ses: önce diyalog", text: "Sektörde diyalog animasyondan önce kaydedilir. Replikler sekans ve çekim altında; geçici ses (yerel Okur modeli) ya da final kayıt, ölçülen süre ve çekimdeki yeri yan yana. Fark 0,5 saniyeyi aşarsa amber uyarı çıkar." },
  { page: "voice", target: "voice-upload", when: "project", title: "Toplu ses yükleme", text: "Kayıtları buraya sürükle. S001_SH01_2.wav gibi adlandırılmış dosya ilgili repliğe kendiliğinden eşlenir; eşlenemeyenleri listeden elle ata." },
  { page: "voice", target: "voice-lock", when: "project", title: "Sesi kilitle", text: "Sekansın sesi kilitlenince replik düzenleme kapanır ve henüz üretilmemiş çekimlerin süresi ölçülen sesten hesaplanır (sesten). Kilidi açmak ayrı bir onay ister; ikisi de kürasyon kaydına düşer." },
  { page: "images", target: "images-prompt", when: "project", title: "Görsel stüdyosu", text: "Varlık görselini dışarıya çıkmadan burada üret: metinden ya da kaynak görsellerden düzenleyerek. Bir varlık seçersen tarifi ve değişmezleri isteme eklenir; stil kılavuzu ışık dilini ve paleti verir. Beğendiğin görseli sağdaki \"Varlığa ata\" ile referans yap; ücretsiz modda yerel taslak çizilir ve fiyat sanal olarak düşer." },
  { page: "assets", target: "assets", when: "project", title: "Varlıklar", text: "Senaryoda geçen her karakter, mekân ve nesne burada toplanır. Referansı kilitlenmemiş varlık içeren çekim üretime girmez; model eksik bilgiyi uydurur. Kilitlemek için durağan görseli ve hareket testini onayla." },
  { page: "assets", target: "assets-scope", when: "project", title: "Dizi kütüphanesi", text: "Dizi bölümünde Kapsam süzgeci gelir: Dizi kütüphanesi bütün bölümlerde kullanılabilen varlıklardır, Bu bölüm yalnızca bu bölümdekiler. Dizi varlığı bölüme kopyalanmaz, bağlanır; senaryoda aynı adı geçen varlık kendiliğinden bağlanır." },
  { page: "assets", target: "series-upgrade", when: "project", title: "Yeni sürüm var", text: "Dizi varlığının referansı ya da tarifi değişince görüntüsü üretilmiş bölümler eski sürümde sabit kalır; hiçbir çekim kendiliğinden yeniden üretilmez. \"Etkisini gör\" yeniden üretilecek çekimleri ve tahmini maliyeti gösterir, \"Yeni sürüme geç\" bölümü son sürüme taşır." },
  { target: "episode-lock", when: "project", title: "Sürüm sabit", text: "Müşteri Kabul kapısını onaylayınca bölüm teslim edilmiş sayılır ve o film sürümüne kilitlenir: kırıntıda \"Sürüm sabit · v004\" görünür, üretim, senaryo ve stil kapanır. Kilidi yalnızca yönetici, onay ifadesiyle açar; karar kürasyon kaydına düşer." },
  { page: "style", target: "style", when: "project", title: "Stil kılavuzu", text: "Işık dili, hareket tarzı ve doku kuralları tek yerden gelir; çekim bazında yazılmaz. Stil değişince hangi çekimlerin yeniden üretileceği önce önizlenir." },
  { page: "style", target: "style-medium", when: "project", title: "Çizim türü: 2D / 3D", text: "Dizinin çizim türü buradan seçilir; video istemindeki stil cümlesi ve karakter, nesne, mekân kuralları buna göre yazılır. Yeni sürümler 2D başlar. Tür değişikliği yeni bir stil sürümüdür: önce etkisini önizle, sonra etkinleştir." },
  { page: "board", target: "board-animatic", when: "project", title: "Animatik", text: "Animatik sekmesi onaylı kareleri geçici ya da final sesle, sesin süresine göre dizer. Toplam süre ve sekans süreleri görünür; ücretsiz modda yerel olarak birleştirilir." },
  { page: "board", target: "board", when: "project", title: "2. Storyboard onayı", text: "Her çekimin bir karesi vardır. Onaylamak ya da değişiklik istemek en ucuz revizyon noktasıdır; onay zorunluysa onaysız çekim üretilmez." },
  { page: "edit", target: "edit-bin", when: "project", title: "3. Kurgu", text: "Soldaki havuz çekimleri listeler, ortadaki izleyici seçili çekimi oynatır. Altındaki şerit aynı çekim için üretilen adaylardır: filmde oynayan \"filmde\" etiketlidir; bir adaya tıklayıp izle, beğenirsen \"Bu adayı filmde kullan\" ya da zaman çizelgesindeki çekimin üstüne sürükle." },
  { page: "edit", target: "inspector", when: "project", title: "Denetçi", text: "Seçili çekimin ayrıntıları sekmelerde: Çekim (durum, adaylar, \"Bunu kullan\"), Ses (replikler, müzik) ve Üretim (motor, yöntem, elle düzeltme). Her eylemden sonra hangi aşamaların çalışacağı yazılır; yalnızca ses değişirse görüntü motoru çağrılmaz." },
  { page: "deliver", target: "deliver", when: "project", title: "4. Teslim", text: "Tamamlanan çekimler tek filme birleşir; kaba kurgu ve final ayrı sekmelerde izlenir. EDL, OpenTimelineIO ve çekim paketi buradan indirilir." },
  { page: "board", target: "safe-areas", when: "project", title: "Güvenli alanlar", text: "Yayın güvenli alanları (action safe %93, title safe %90) storyboard karelerinin ve izleyicinin üstüne çizilir. G tuşu her sayfada açıp kapatır." },
  { page: "color", target: "color-script", when: "project", title: "Renk betiği", text: "Her sekansın baskın paleti yan yana, altında çekimlerinki; sağda stil kılavuzunun hedef paleti. ΔE, sekans paletinin hedefe uzaklığıdır: yüksekse sekans hedef paletten kaymıştır." },
  { page: "assets", target: "lineup", when: "project", title: "Boy sıralaması", text: "Karakterler gerçek boylarıyla yan yana durur; boy, varlığın Gerçek boyut alanından okunur (1,70 m gibi bir ölçü yazmak en kesinidir). Varlıklar › Boy sıralaması." },
  { page: "deliver", target: "review-link", when: "project", title: "Müşteri incelemesi", text: "Teslim › Müşteri: her onay kapısının durumu ve revizyon turu burada. \"İnceleme bağlantısı oluştur\" filmi v001, v002 diye etiketler ve süreli, salt okunur bir bağlantı verir; müşteri oynatıcıda zaman kodlu not bırakır, onaylar ya da değişiklik ister. Bütçe ve motor bilgisi o sayfada görünmez." },
  { page: "deliver", target: "delivery-spec", when: "project", title: "Teslim şartnamesi", text: "Teslim › Teslim: çözünürlük, kare hızı, master kodeği, loudness hedefi, altyazı, M&E ve yazısız sürüm. Hazır ayarlar: TV (Türkiye, 25 fps), Web, Platform (yüksek). Final film bir sonraki birleştirmede bu biçimle çıkar." },
  { page: "deliver", target: "qc", when: "project", title: "QC raporu", text: "Siyah kare, donma, loudness, fotosensitif parlama ve çizgi titremesi geçti/kaldı ve zaman koduyla listelenir; PDF olarak indirilir. Kalan kontroller Kurgu zaman çizelgesinde işaretlidir." },
  { page: "deliver", target: "delivery-package", when: "project", title: "Teslim paketi", text: "Paket içeriği önce listelenir: master, izleme kopyası, stem'ler, M&E, altyazı, EDL/OTIO, QC raporu ve hak zinciri. Eksik hak zinciri belgesi uyarır ama paketi engellemez." },
  { page: "edit", target: "client-notes", when: "project", title: "Müşteri notları", text: "Müşterinin notları zaman çizelgesinde ilgili çekimin üstünde işaretçi olarak ve Notlar listesinde görünür; çözdüğün notu işaretle." },
  { target: "todos", when: "project", title: "Onay kapıları", text: "Müşteri onay kapıları (Senaryo, Tasarım, Animatik, Taslak kurgu, Final, Kabul) Yapılacaklar listesinin altında renkli çipler olarak durur: yeşil onaylandı, kırmızı değişiklik istendi, amber müşteride." },
  { page: "settings", target: "budget", title: "Bütçe ve Kill-Switch", text: "Ayarlar › Bütçe ve durum harcanan, ayrılan ve kalan tutarı, limiti, yumuşak sınırı ve çalışma modunu gösterir. Alt banttaki \"Üretimi durdur\" tüm kuyruğu tek tıkla dondurur; devam edince tamamlanmış klipler yeniden üretilmez." },
  { target: "settings", title: "Ayarlar: kullanıcı ve yönetici", text: "Alt bandın sağındaki dişli Ayarlar'ı açar. Ayarlar'da kullanıcı ya da yönetici modunu seçersin. Kullanıcı modunda her sayfada yalnızca filmi yapmak için gerekenler kalır. Ücretli servisler, fiyat tablosu, servis geçmişi, motor seçimi ve plan ayrıntıları yönetici modundadır; yönetici şifresi tanımlıysa giriş ister." },
  { target: "pages", title: "Hepsi bu", text: "Shift+1…5 ana sekmeler arasında geçer, G güvenli alanları açıp kapatır; bütün denetimler alt bantta durur. Bu turu ve kitaptan çizgi film rehberini alt banttaki Rehberler (?) düğmesinden yeniden açabilirsin." },
];

export const tourStepsFor = (hasProject: boolean, id: TourId = "panel"): TourStep[] =>
  (id === "book" ? BOOK_STEPS : TOUR_STEPS)
    .filter((s) => !(s.when === "no-project" && hasProject) && !(s.when === "project" && !hasProject));

/** Senaryo sayfasındaki "Kitap şablonu": konuşmasız, her kitap sayfası ayrı bir sahne (zincir yok, ilk kare sayfanın
    kendisi). Aynı başlık tek bir mekân varlığı açar; bağı çekimlerden kaldırılır. */
export const BOOK_TEMPLATE = `# SEKANS: Kitap

DIŞ. KİTAP - GÜN
Sayfa 1. Ponpon bahçede oturur, kuyruğunu yavaşça sallar ve gözlerini kırpar. Kamera sabit. [KARAKTER: Ponpon]

DIŞ. KİTAP - GÜN
Sayfa 2. Ponpon çitin üstünden hafifçe sıçrar, kelebeğe pati uzatır. Kamera sabit. [KARAKTER: Ponpon]

DIŞ. KİTAP - GÜN
Sayfa 3. Pamuk Nine pencereden bakar, başını sallayıp gülümser. Kamera sabit. [KARAKTER: Pamuk Nine]
`;

const scenes = (c: TourCtx): Scene[] => c.ov?.scenes ?? [];
const allScenes = (c: TourCtx, f: (s: Scene) => boolean) => (scenes(c).length ? scenes(c).every(f) : null);

/** Kitaptan çizgi film: kitap sayfaları ilk kare, hazır sesler (anlatım, müzik, efekt) filme dışarıdan yerleşir.
    Ponpon denemelerinin dersleri metinlerdedir: sayfa kırpılmadan yüklenir, mekân varlığı bağlanmaz, çekimler
    zincirlenmez, karede küçük ya da kesik görünen karakterin adı yazılmaz, videolarda konuşma yoktur. */
export const BOOK_STEPS: readonly TourStep[] = [
  { target: "pages", title: "Kitaptan çizgi film",
    text: "Bu rehber, kitap sayfalarından ve elindeki seslerden baştan sona bir çizgi film yapar. Kart açıkken uygulamayı kullanabilirsin: vurgulanan yerde işi yap, sonra İleri. Her kartta adımın gerçek durumu görünür. Hazırlık: sayfaları kırpmadan PNG ya da JPG olarak, sesleri (anlatım, müzik, efekt) WAV ya da MP3 olarak bir klasöre koy." },
  { page: "settings", click: "settings-view", target: "view-mode", fallback: "settings", title: "Yönetici modu",
    text: "Bazı ayarlar (video girdisi, yoğunluk, eşikler) yönetici modunda görünür. Burada Yönetici'yi seç; şifre tanımlıysa girişi yap.",
    check: (c) => c.admin },
  { page: "settings", click: "settings-fal", target: "env-VIDEO_INPUT_MODE", fallback: "settings", admin: true, title: "Video girdisi: ilk kare",
    text: "fal.ai › Model ve uç noktalar'da Video girdisini keyframe yap ve Kaydet. Böylece her çekim senin yüklediğin kitap sayfasından başlar; reference modunda kare yüklenemez.",
    check: (c) => (c.meta ? c.meta.video_input_mode === "keyframe" : null) },
  { page: "script", target: "script-form", title: "Yeni proje",
    text: "Proje yöneticisinden (sol alttaki ev) \"Yeni film\" de ya da buradan bir ad ve sanal bütçe ver. Ücretsiz modda hiçbir ücretli servis çağrılmaz; çekimler yerel taslak olarak üretilir, böylece akışı para harcamadan deneyebilirsin.",
    check: (c) => Boolean(c.ov) },
  { page: "script", target: "book-template", fallback: "script-text", title: "Senaryo: sayfa başına bir çekim",
    text: "\"Kitap şablonu\" düğmesi örneği yükler. Her sayfa için \"DIŞ. KİTAP - GÜN\" başlığı ve tek paragraf yaz: sayfada ne kıpırdayacak, kısa ve sakin bir eylem, \"Kamera sabit\". Sayfada açıkça görünen karakterleri [KARAKTER: Ad] ile işaretle; karede küçük ya da kesik duranın adını yazma, model onu büyütüp yeniden çizer. Konuşma yazma: anlatım dışarıdan gelecek. Sonra \"Senaryoyu işle ve üret\".",
    check: (c) => (c.ov ? c.ov.scenes.length > 0 : false) },
  { page: "script", target: "intensity", fallback: "script-form", admin: true, when: "project", title: "Yoğunluk ve eşikler",
    text: "Kitap çizimlerinde kalite kapısı fazla katıdır. Benzerlik eşiğini 0,65 civarına çek (Ponpon'da işe yaradı), insan karakterlerde Kimlik eşiğini düşür. H3 sesi kapalı kalsın: videoda konuşma ya da anlatıcı olmaz." },
  { page: "assets", target: "assets", when: "project", title: "Karakterler",
    text: "Senaryodaki her karakter burada bir kart. Referansı kilitlenmemiş karakteri içeren çekim üretime girmez; model eksik bilgiyi uydurur. Soldaki listeden karakteri seç.",
    check: (c) => { const r = scenes(c).flatMap((s) => s.assets.filter((a) => a.type === "character")); return r.length ? r.every((a) => a.status === "locked") : null; } },
  { page: "assets", target: "asset-ref-upload", fallback: "assets", when: "project", title: "Kitaptan referans görsel",
    text: "Referans türü Durağan görsel; dosya olarak kitapta karakterin en net, tam boy göründüğü sayfayı ya da kesitini yükle. Yüz ve kıyafet o görselden okunur; bölüm boyunca değişen bir durum (ör. saç kesildi) karta değil, o çekimin paragrafına yazılır." },
  { page: "assets", target: "asset-lifecycle", fallback: "assets", when: "project", title: "Onayla ve kilitle",
    text: "\"Durağanı onayla\", sonra \"Kilitle\". Ücretsiz modda hareket testi yoktur; ücretli modda kilitten önce kısa bir hareket testi üretilir ve onaylanır. Her karakter için tekrarla." },
  { page: "edit", scene: true, click: "insp-shot", target: "shot-assets", fallback: "inspector", when: "project", title: "Mekân bağını kaldır",
    text: "İlk kare zaten kitap sayfası olduğu için mekân varlığı gerekmez; bağlı kalırsa model sahneyi o mekâna göre yeniden kurar. Denetçi › Çekim › Varlıklar'da mekân etiketinin × düğmesine bas. Soldaki havuzdan çekim seçip her çekimde tekrarla.",
    check: (c) => allScenes(c, (s) => !s.assets.some((a) => a.type === "location")) },
  { page: "edit", scene: true, click: "insp-gen", target: "first-frame", fallback: "inspector", when: "project", title: "Kitap sayfası = ilk kare",
    text: "Denetçi › Üretim › \"Kendi ilk kareni yükle\": çekimin kitap sayfasını kırpmadan, sayfanın tamamını yükle. Yakınlaştırıp kırpma; küçük kalan karakteri model büyütür. Yüklenen kare onaylı sayılır. Her çekime kendi sayfasını ver.",
    check: (c) => allScenes(c, (s) => s.keyframe_source === "upload") },
  { page: "edit", scene: true, click: "insp-gen", target: "shot-transition", fallback: "inspector", when: "project", title: "Zincir yok: yeni sahne",
    text: "Çekime geçiş \"Yeni sahne\" olsun (şablondaki her başlık bunu kendiliğinden yapar). Zincirde video bir sonraki sayfanın karesinde biter ve karakter vaktinden önce hareket eder; kitapta her sayfa kendi başına durur." },
  { page: "edit", scene: true, click: "insp-gen", target: "shot-video", fallback: "inspector", when: "project", title: "Süre ve model sesi",
    text: "Çekimin süresini o sayfanın anlatımına göre ayarla (2–15 sn). Model sesi Kapalı: videolarda konuşma, anlatıcı ya da replik olmaz; sesin tamamı senin dosyalarından gelir." },
  { page: "edit", target: "retry-blocked", fallback: "edit-bin", when: "project", title: "Üret",
    text: "Karakterler kilitlenince \"Engellenen çekimleri yeniden dene\" üretimi başlatır (düğme yalnızca engelli çekim varken görünür). Ücretsiz modda yerel taslak klipler çıkar; gerçek üretim için ücretli mod Ayarlar › Çalışma modu'ndadır ve onayını ister.",
    check: (c) => (scenes(c).length ? scenes(c).some((s) => s.status === "done") : null) },
  { page: "edit", target: "viewer", fallback: "edit-bin", when: "project", title: "İzle ve seç",
    text: "İzleyici seçili çekimi oynatır, altındaki şerit adaylarıdır: beğendiğine tıklayıp \"Bu adayı filmde kullan\". Çekime sağ tıkla: Filmden çıkar, İyileştir, yeniden üret. Kötü çıkan çekimde önce paragrafı sadeleştir, karakter adını gerekmiyorsa çıkar." },
  { page: "voice", target: "voice-external", fallback: "voice", when: "project", title: "Sesleri yükle",
    text: "Anlatım ve dış sesler: anlatıcı kaydını Tür = Anlatım ile, müzik ve efektleri kendi türleriyle yükle. Başlangıç, sesin sekansın başından kaç saniye sonra çalacağıdır. Anlatım müziği kendiliğinden kısar. Kendi müziğin varsa otomatik müziği Kaldır. Hiçbir çekim yeniden üretilmez.",
    check: (c) => (c.ov ? c.ov.sequences.some((q) => q.tracks.some((t) => t.source === "upload")) : null) },
  { page: "edit", target: "timeline", fallback: "edit-bin", when: "project", title: "Zaman çizelgesi",
    text: "Görüntü, diyalog/anlatım ve müzik izleri film süresi üzerinde. Anlatım bir sayfanın görüntüsünü aşıyorsa o çekimin süresini uzat ya da sesin başlangıcını kaydır." },
  { page: "deliver", target: "assemble", fallback: "deliver", when: "project", title: "Filmi birleştir",
    text: "Tamamlanan çekimler sırayla filme birleşir, sesler üstüne yerleşir ve ses seviyesi dengelenir. Oynatıcıda izle.",
    check: (c) => (c.ov ? Boolean(c.ov.timeline) : null) },
  { page: "deliver", target: "exports", fallback: "deliver", when: "project", title: "Dışa aktar",
    text: "Çekim paketi (videolar ve ayrı ses stem'leri), EDL ve OpenTimelineIO buradan iner; DaVinci ya da Premiere'de son dokunuşları yapabilirsin. Teslim › Teslim paketi master MP4'ü de içerir." },
  { target: "help", title: "Hepsi bu",
    text: "Rehberi istediğin an alt banttaki Rehberler (?) düğmesinden aç; kaldığın adımdan devam eder. İyi bölümler!" },
];
