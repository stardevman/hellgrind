# Hell Grind — Otonom AI Animasyon Fabrikası

Senaryodan film üreten otonom bir animasyon hattı. Bu aşamada hattın **tamamı yerel ve ücretsiz** bileşenlerle çalışır: hiçbir ücretli servis çağrılmaz, hiçbir kredi kartı gerekmez. Üretim, bir kontrol paneliyle izlenir ve yönetilir. Uzun vadeli hedef 90 dakikalık bir filmdir.

![Kontrol paneli](docs/panel.png)

## Neler yapar

1. **Senaryoyu okur.** Standart senaryo biçimini (sahne başlığı, konuşan karakter, aksiyon) kural tabanlı ayrıştırıcıyla çekimlere böler. Her çekim için kamera, duygu, süre ve aksiyon yoğunluğu çıkarır.
2. **Eksik referansla üretmez.** Model eksik bilgiyi uydurur: çekimde geçen her karakter, mekân ve nesnenin onaylı ve kilitli bir referansı yoksa o çekim *engellenir* ve hiçbir şey harcanmaz. Referanslar *Varlıklar* sayfasında yüklenir, kısa bir hareket testinden geçer ve yönetmen onayıyla kilitlenir.
3. **Tek bir stil dili kullanır.** Işık dili, hareket tarzı ve render kuralları projenin sürümlü *stil kılavuzundan* gelir ve her çekimin prompt'una aynı ifadeyle eklenir. Kılavuz değişince önce hangi çekimlerin yeniden üretileceği ve kaça mal olacağı gösterilir; yönetmen onaylamadan hiçbir şey üretilmez.
4. **Önce storyboard, sonra kaba kurgu.** Her çekimin storyboard karesi yönetmen onayından geçer (istenirse zorunlu). Taslak geçiş yalnızca test turuyla filmin kaba kurgusunu çıkarır; yalnızca zayıf bulunan çekimler tam turla iyileştirilir.
5. **Her çekim için yöntem seçtirir.** Çekim yapay zekâyla üretilebilir, bir previs (3D taslak) rehberinden çıkarılabilir ya da dışarıda bitirilip yüklenebilir. Üretilen klipler elle düzeltilip yeniden yüklenebilir; orijinal korunur.
6. **Çekimleri üretir.** Her çekim için önce hızlı bir *test turu*, sonra dalgalar halinde *tam tur* klip üretir. Ücretsiz modda klipler yerel bir prosedürel motorla çizilir; ücretli modda taslak **MiniMax H3 Max**, final **Seedance 2.5** ile fal.ai'de üretilir (kendi barındırdığımız MiniMax H3 / RunPod kapalıdır).
7. **Kalite kontrolünden geçirir.** Siyah ya da donmuş klipleri gerçek FFmpeg filtreleriyle bulur, eşiği geçemeyeni anında eler ve geriye en iyi birkaç klibi *rafine havuz* olarak bırakır.
8. **Rengi ve sürekliliği denetler.** Film geneli bir LUT ve çekim bazında renk ayarı uygular; ardışık çekimler arasındaki renk sıçramalarını bulur ve tek tıkla uygulanabilir bir düzeltme önerir.
9. **Sesi ve montajı yapar.** Türkçe konuşma (espeak-ng), diyalog altında müziğin kısılması, ses seviyesi dengeleme.
10. **Filmi birleştirir.** Hazır sahneleri sekanslarına göre sırayla tek videoda toplar. Müzik sekans düzeyindedir ve çekimler arasında kesilmeden akar. Tıklanabilir bir kesme listesi üretir.
11. **Yönetmenin zevkini kaydeder.** Her seçim, eleme, iyileştirme işareti ve onay bir *kürasyon olayı* olarak saklanır; *Teslim* sayfasının raporu aynı kaliteye kaç üretimle ulaşıldığını gösterir, film DaVinci Resolve ya da Premiere'e EDL, OpenTimelineIO ya da çekim paketi olarak aktarılır.
12. **Parayı korur.** Her çağrı önce bütçeden rezervasyon alır. Limit aşılacaksa proje anında donar (Kill-Switch). Ücretsiz modda harcama, gerçek sağlayıcı fiyatlarıyla *sanal* olarak tutulur.

Bir şeyi değiştirdiğinizde yalnızca etkilenen aşamalar yeniden çalışır:

| Değişen girdi | Yeniden çalışanlar | Görüntü motoru |
|---|---|---|
| İstem, kamera, motor, süre, karakter sürümü, bir varlığın referansı ya da etkin stil kılavuzu | Storyboard karesi, görüntü, ses, montaj | çağrılır |
| Bir diyalog satırı | Ses ve montaj | çağrılmaz |
| Küratör rafine havuzdan başka bir klip seçer | Ses ve montaj | çağrılmaz |
| Rafine #1 elle düzeltilir (revizyon yüklenir) ya da orijinaline dönülür | Ses ve montaj | çağrılmaz |
| Dış çekim yüklenir | Ses ve montaj | çağrılmaz, harcama yok |
| Çekimin renk ayarı ya da film LUT'u | Renk ve montaj | çağrılmaz |
| Bir çekime özel müzik ya da seviyesi | Yalnızca montaj | çağrılmaz |
| Sekans ya da film müziği, bestecinin stem'i | Yalnızca film birleştirme | çağrılmaz |
| Hiçbiri | Hiçbiri | çağrılmaz |

## Gereksinimler

- Docker ve Docker Compose
- Boş portlar: `8000` (panel ve API), `9000` ve `9001` (MinIO), `5555` (Flower), `5433` (PostgreSQL), `6379` (Redis)

FFmpeg, espeak-ng, yazı tipleri ve kalite skorlayıcısının modelleri Docker imajının içindedir; makinenize bir şey kurmanız
gerekmez. İmaj, gerçek kalite skorlayıcısı (CPU'luk torch ve ~1,5 GB model) yüzünden ~3,4 GB'tır; ilk kurulum modelleri
indirir. Skorlayıcısız hafif imaj (~0,9 GB; yalnızca sahte skorla ücretsiz mod): `docker compose build --build-arg QUALITY=0`.

## Kurulum

```bash
cp .env.example .env          # varsayılanlar ücretsizdir; ücretli anahtarlar boş kalır
docker compose up -d --build  # tüm servisleri derler ve başlatır
curl localhost:8000/healthz   # {"ok":true,"dry_run":true}
```

Servisler: `postgres`, `redis`, `minio`, `api`, `worker` (montaj, ses, kontrol), `worker-gen` (üretim: tek süreç, iş
parçacığı havuzu; kalite skorlayıcısı ~3,6 GB bir kez yüklenir), `beat`, `flower`. Veritabanı şeması ilk açılışta kurulur; sonraki şema değişiklikleri API açılışında `backend/migrations/*.sql` dosyalarından, veritabanı silinmeden uygulanır.

| Adres | Ne |
|---|---|
| http://localhost:8000 | Kontrol paneli (Next.js; `/panel/`) |
| http://localhost:8000/docs | API belgeleri |
| http://localhost:9001 | MinIO konsolu (kullanıcı `minio`, parola `minio12345`) |
| http://localhost:5555 | Flower (görev kuyruğu) |

## Kullanım

Panel bir yapım stüdyosu gibi düzenlenmiştir: üstte proje, sanal bütçe ve **Üretimi durdur** (Kill-Switch); altta, kurgu
yazılımlarındaki gibi, üretim sırasıyla sayfa çubuğu: **Senaryo › Stil › Varlıklar › Storyboard › Kurgu › Karşılaştır › Renk
› Teslim** (<kbd>Shift</kbd>+<kbd>1</kbd>…<kbd>8</kbd>). Sayfa düğmelerindeki rozetler durumu özetler (kilitli varlık, onaylı
kare, hazır ya da engelli çekim, renk sıçraması, film). Görünüm adres çubuğunda tutulur (proje, sayfa, çekim, varyasyon,
karşılaştırılan adaylar, varlık): bağlantıyı paylaşınca aynı görünüm açılır.

1. **Senaryo** sayfası: henüz proje yoksa **Yeni proje** formu açılır: proje adı, sanal bütçe, üretim yoğunluğu ve örnek
   senaryo hazır dolu olarak; sağda senaryo biçiminin kılavuzu.
   - *Önizleme*: en çok 4 test ve 8 tam klip (örnek film için yeterli, hızlı).
   - *Brifing*: 10 test ve en çok 50 tam klip.
   - *Üretim (ekonomik)*: 3 test (480p) ve 3 tam klip (720p), dalga 3, havuzda 1 aday, 1 düzeltme turu.
   Formun altındaki **Tahmini maliyet** senaryodan çekim başına ve toplam tutarı hesaplar (kare + test + tam tur; "en çok"
   kimlik kapısının yeniden denemeleri ve düzeltme turları dahil; kullanılan her model için bir soğuk başlatma). Mevcut
   projede tutar projenin çekimlerinden ve ayarlarından hesaplanır (`POST /api/estimate`).
   **Senaryoyu işle ve üret**'e basınca Kurgu sayfası açılır. Mevcut projede bu sayfa senaryoyu düzenletir; **Değişiklikleri
   işle ve üret** yalnızca değişen çekimleri yeniden üretir.
2. İlk seferde çekimlerin hepsi **Engellendi** durumundadır: karakterlerin (Kerem, Ayşe) ve mekânların (Terk Edilmiş
   Dökümhane, Çöken Köprü) henüz onaylı referansı yoktur. Denetçideki *Eksik referans* bağlantıları ilgili varlığa götürür.
3. **Varlıklar** sayfası (solda tür ve durum süzgeçleri, ortada varlık ızgarası, sağda seçili varlık):
   1. Referans yükleyin (karakter için 360° dönüş videosu, ifade sayfası ya da ses örneği; mekân ve nesne için durağan görsel).
      Elinizde dosya yoksa **Yerel taslak üret** prosedürel motordan bir taslak referans çıkarır.
   2. **Durağanı onayla**.
   3. **Hareket testi üret**: kısa, düşük çözünürlüklü bir klip. Durağanda iyi görünen bir varlık hareket halinde bozulabilir;
      testi izleyin. Kusurlu (siyah ya da donmuş) test onaylanamaz, **Hareket testini yenile** ile yenisini üretin.
   4. **Hareket testini onayla**, sonra **Kilitle**. Hareket testi onaylanmadan varlık kilitlenemez.
   İsterseniz önce **Stil** sayfasında **Örnekle doldur** (altın saat ışığı, çizgi film hareketi, suluboya mekân + temiz 3D
   nesneler) ile ilk sürümü kaydedip etkinleştirin; hareket testleri de bu stille üretilir.
4. Hepsi kilitlenince **Engellenen çekimleri yeniden dene**'ye basın. Üretim başlar.
5. **Kurgu** sayfası bir kurgu yazılımı gibidir:
   - Solda **çekim havuzu** (sekanslara göre, durum ve harcamayla) ve **olay akışı**.
   - Ortada **izleyici**: zaman kodu (24 kare/sn), kare kare ilerleme, döngü; *Çekim*, *Film* ve *Kaba kurgu* kaynakları.
     Kısayollar: <kbd>Boşluk</kbd>/<kbd>K</kbd> oynat-duraklat, <kbd>J</kbd>/<kbd>L</kbd> bir saniye, <kbd>←</kbd>/<kbd>→</kbd>
     bir kare, <kbd>↑</kbd>/<kbd>↓</kbd> önceki/sonraki çekim.
   - İzleyicinin altında seçili çekimin **adayları**: yeşil çerçeveli ve numaralı rafine havuzda; gri ve kırmızı çapraz çizgili
     kalite kontrolünde elendi; soluk havuza giremedi; mor ve nefes alan üretiliyor. Tıklanan aday (karantinadakiler dahil)
     izleyicide önizlenir.
   - Altta **zaman çizelgesi**: sekans bandı, görüntü izi (çekimler süreleri kadar, duygu renginde), diyalog, müzik ve efekt
     izleri. Cetvele tıklamak filmi oraya sarar; film oynarken oynatma kafası çekimler üzerinde ilerler ve denetçi o çekime geçer.
   - Sağda **denetçi** (bölümler açılıp kapanır): çekim bilgisi, **Bu sahneyi baştan üret**, **İyileştir**; rafine havuz ve
     **Bunu kullan** (yalnızca ses ve montaj yeniden çalışır); **Ses**: diyalog ve **Müziği değiştir** (*Sekansın tamamı*
     sekans müziğini değiştirir, hiçbir çekim yeniden üretilmez; *Yalnızca bu çekim* çekime özel müzik koyar); **Sekans**: bütçe
     tavanı, film kanalları ve **Stem yükle**; **Varlıklar** ve **Nesne bağla**; **Yöntem**; **Elle düzeltme**.
   Her eylemden sonra panel hangi aşamaların çalışacağını yazar.
6. **Karşılaştır** sayfası aynı çekimin 2–4 adayını yan yana, tek taşıma çubuğuyla **eşzamanlı** oynatır (denetçide
   **Adayları karşılaştır**; aday şeridinde <kbd>Shift</kbd>+tık ile istediğin klipleri seçebilirsin):
   - Varsayılan olarak rafine havuz açılır; üstteki şeritten elenenler dahil izlenebilen her aday eklenip çıkarılabilir.
   - **Izgara** ya da **Perde (1 ↔ 2)**: iki adayı üst üste koyup kaydırılabilir bir perdeyle karşılaştırır.
   - Kare kare ilerleme (<kbd>←</kbd> <kbd>→</kbd>, <kbd>Shift</kbd> ile 1 sn), <kbd>Boşluk</kbd> oynat/duraklat, 0,25×–1× hız,
     döngü; <kbd>1</kbd>–<kbd>4</kbd> bir adayı tek başına büyütür.
   - Ölçüm tablosu birleşik skoru (gerçek skorlayıcıyla stil, tarif ve yüz uyumunu da) yan yana koyar, her satırda öndekini
     işaretler ve farkı yazar. Havuzdaki bir aday **Bunu kullan** ile seçilir; seçim kürasyon kaydına düşer.
7. **Renk** sayfası: seçili çekimin önce/sonra görüntüsü, kaydırıcılarla pozlama, kontrast, doygunluk, gama ve sıcaklık
   (çift tık nötre döndürür), film LUT'u ve **Sürekliliği denetle**; alttaki klip şeridinde renk sıçramaları kırmızı çentikle
   işaretlidir ve **Öneriyi uygula** tek tıkla düzeltir.
8. **Teslim** sayfası: **Tamamlanan N sahneyi birleştir** (ya da kaba kurgu), film ve kesme şeridi; dışa aktarma kuyruğu (EDL,
   OpenTimelineIO, çekim paketi, storyboard PDF'i, kürasyon kaydı) ve verimlilik raporu.
9. Üst banttaki **Üretimi durdur** düğmesi Kill-Switch'tir: kuyruktaki işler iptal olur, yeni iş başlamaz. **Üretime devam et**
   kaldığı yerden sürdürür; tamamlanmış klipler yeniden üretilmez ve yeniden ücretlendirilmez.

API'yi doğrudan da kullanabilirsiniz:

```bash
curl -s localhost:8000/api/meta | jq .presets
curl -s -X POST localhost:8000/api/projects -H 'content-type: application/json' \
  -d '{"title":"Film","budget_limit_usd":60,"settings":{"probe_n":4,"full_n":8,"wave_size":4,"max_spend_per_10min":30}}'
curl -s localhost:8000/api/projects/<id>/overview | jq .scenes[0].status
```

### Senaryo biçimi

```
İÇ. TERK EDİLMİŞ DÖKÜMHANE - GECE          ← sahne başlığı (İÇ., DIŞ., İÇ/DIŞ., INT., EXT.)

Kızıl bir ışık kazanların arasından süzülüyor.   ← aksiyon: her paragraf yeni bir çekim
Kerem bir taksiye biner. [NESNE: Taksi]          ← nesne: yalnızca bu çekime bağlanır
[KARAKTER: Ayşe]                                 ← konuşmayan karakter: kadroya girer

KEREM                                       ← konuşan karakter: büyük harfli, en çok 4 kelime
(tedirgin)                                  ← parantez: duygu ipucu
Burada kimse yok mu?
```

Mekân sahne başlığından çıkar, gün zamanı atılır: `İÇ. TERK EDİLMİŞ DÖKÜMHANE - GECE` → *Terk Edilmiş Dökümhane*. `[NESNE: Taksi, Bavul]` işareti bir paragrafın içindeyse o çekime, tek başına bir satırdaysa sahnenin bütün çekimlerine bağlanır; işaret metinden ve replikten silinir. Karakter, mekân ve nesneler ilk görüldüklerinde *taslak* varlık olarak oluşur.

Film sekanslara bölünür: `# SEKANS: Havalimanı` satırı yeni bir sekans başlatır ve sonraki sahne başlıkları ona aittir (sekans satırını sahne başlığından önce yazın). Hiç sekans satırı yoksa bütün sahneler tek sekanstadır (*Sekans 1*). Örnek senaryo iki sekanslıdır: *Dökümhane* ve *Köprü*.

Kadro, konuşan karakterlerden ve `[KARAKTER: Ayşe]` ile işaretlenenlerden oluşur; kadrodaki bir karakter adı geçtiği her
çekime bağlanır. Konuşmayan bir karakteri işaretlemezseniz prompt "kimse yok" der ve görüntü modeli onu çizmez. İşaret
paragraf içindeyse o çekime, tek başına bir satırdaysa sahnenin bütün çekimlerine bağlanır. Projede zaten olan karakterler
işaretsiz de tanınır.

Senaryo İngilizce de yazılabilir: `INT.` / `EXT.`, `# SEQUENCE:`, `[CHARACTER: Kerem]`, `[PROP: Taxi]`; İngilizce duygu
(afraid, angry, whisper...) ve aksiyon kelimeleri (run, jump, explode...) de tanınır. Türkçe harf içermeyen senaryoda adlar
İngilizce kurallarla yazılır (`IRIS` → *Iris*).

Karakter adındaki uzantılar (`KEREM (D.S.)`) atılır. Duygu, Türkçe kök sözlüğünden çıkarılır (korku, öfke, gerilim...). Aksiyon kelimeleri (koş, atla, patla, düş, kop, sarsıl...) çekimi geniş açı ve yüksek aksiyon yoğunluğuna çevirir. Her çekim MiniMax H3'e yönlenir; Seedance yalnızca yönetmen denetçiden seçerse kullanılır (ücretsiz modda yalnızca sanal fiyatı değiştirir).

## Testler

İki katman vardır:

```bash
make test    # tam paket: Docker içinde, PostgreSQL, Redis ve MinIO ile (~4 dk)
make smoke   # uçtan uca duman testi: gerçek HTTP ve worker ile (~2 dk)
```

- **`make web-test`** panelin tip denetimini ve birim testlerini (metinler, karşılaştırma mantığı, tasarım sözleşmesi) çalıştırır; makinede Node 22 gerekir.
- **`make test`**, veritabanı ve nesne deposu gerektiren testleri de çalıştırır. Geliştirme verinize dokunmaz: `hellgrind_test` adlı ayrı bir veritabanı ve `hellgrind-test` adlı ayrı bir kova kullanır. Depo kökündeki `db/` klasörünü bağlar ki testler şemayı kurabilsin; `frontend/src`'yi de bağlar ki `test_panel_next.py` panelin çağırdığı her uç noktanın arka uçta o yöntemle var olduğunu ve her durum, duygu, varlık ve olay değerinin Türkçe karşılığı olduğunu denetleyebilsin.
- **CI** (`.github/workflows/ci.yml`) Ubuntu üzerinde FFmpeg ve espeak-ng ile `DRY_RUN=1 pytest -q` çalıştırır; ayrı bir iş paneli derler (`npm ci`, tip denetimi, birim testleri, `next build`). CI'da veritabanı servisi ve kalite skorlayıcısının modelleri yoktur; altyapı ya da gerçek model gerektiren testler kendiliğinden *atlanır*. Tam doğrulama `make test` iledir (imajda modeller hazırdır).
- **`make smoke`** şunları denetler: ücretli çağrılar engelli; ana projede kare onayı zorunludur: onaysız çekimlerde hiçbir varyasyon üretilmez, storyboard PDF'i dört çekimi sırayla içerir, taslak geçiş yalnızca test turu çalıştırır ve kaba kurguyu kendiliğinden birleştirir, iyileştirme geçişi yalnızca işaretli çekimi tam tura alır; film altın saat stil kılavuzuyla üretilir ve iki farklı çekimin prompt'u aynı önekle başlar; stil değişince önizleme dört çekimi ve sanal maliyeti listeler, onaydan önce hiçbir şey üretilmez, etkinleştirince çekimler yeni önekle yeniden üretilir; yeni projede örnek senaryonun dört çekimi de engellenir ve hiçbir şey harcanmaz; taslak varlık kilitlenemez; gerçek bir dönüş videosu yüklenince 8 kare örneklenir; varlıklar hareket testinden geçip kilitlenince çekimler üretilir, panelden bağlanan taslak bir nesneyi içeren çekim kilitlenene dek engelli kalır ve hiç harcama yapmaz; örnek senaryo 4 sahneye ayrılıp hepsi `done` olur, her sahnede rafine havuz 1–3 klip, bilerek bozulan siyah ve donmuş klipler elenmiştir; yalnızca müzik değişince yalnızca montaj çalışır ve harcama değişmez; küratör seçimi yalnızca ses ve montajı çalıştırır; değişiklik yoksa plan boştur; film 4 sahneyle birleşir ve sekans müziği kesmelerde sürer; dış çekim üretim harcaması olmadan biter ve rafine #1'in elle düzeltmesi yalnızca ses ve montajı çalıştırır; renk değişince yalnızca renk ve montaj çalışır, bilerek farklı renklendirilen çekim süreklilik uyarısı üretir ve önerilen düzeltme uygulanınca uyarı kalkar; sekans müziği değişince yalnızca film yeniden birleştirilir; panel eylemleri (seçim, iyileştirme işareti, kare onayı ve değişiklik isteği, varlık kilidi, revizyon, stil etkinleştirme, baştan üretim) kürasyon kaydına sahne, varyasyon ve skor bağlamıyla düşer; metrikler panelin gördüğü sayılarla tutarlıdır; EDL dört çekimi doğru in/out zaman kodlarıyla içerir; çekim paketi zip'i açılır ve her çekimin diyalog, müzik ve efekt stem'leri çekim süresindedir; projenin her sanal çağrısı servis geçmişinde bir satırdır, işlenen tutarların toplamı harcamaya eşittir ve CSV her çağrıyı içerir; bütün çekimler MiniMax H3'e yönlenir; sekans tavanı (0,10 USD) dolunca yalnızca o sekans durur, diğeri biter; 0,05 USD'lik projede sahne `frozen`, proje `paused` olur, harcama bütçeyi aşmaz ve yeni render isteği 423 döner (H3 bir klibi birkaç sentlik sanal GPU süresiyle ürettiği için bütçeler küçüktür). Başarıyla biterse **TÜM KONTROLLER GEÇTİ** yazar. Geliştirme veritabanına `duman-testi` başlıklı projeler ekler, hiçbir şeyi silmez. Ücretli mod açıksa hiçbir şey başlatmadan durur (çıkış kodu 2): önce panelden ücretsiz moda dönün.

## Varlık kütüphanesi ve referans eksiksizliği

Referans verilmeyen bir taksiye model rastgele yolcular ekler. Bu yüzden:

- Her karakter, mekân ve nesne bir **varlıktır**. Durum akışı: `draft` (taslak) → `still_approved` (durağan onaylı) → `motion_tested` (hareket testi onaylı) → `locked` (kilitli).
- **Üretim öncesi kontrol** (`hermes/lint.py`): görüntü üretimi planlanan bir çekimde gereken durumun altında bir varlık varsa çekim `blocked` olur, `last_error` alanına neyin eksik olduğu Türkçe yazılır ve hiçbir rezervasyon yapılmaz. Kontrol hem kuyruğa vermeden önce hem de worker'da üretim başlamadan önce yapılır.
- Prompt'a her zaman "yalnızca listelenen karakterler" kısıtı eklenir; negatif prompt'a kalabalık ve fazladan kişiler yazılır.
- **Karakter referans paketi:** 360° dönüş videosu, ifade sayfası, ses örneği ve `voice_id`. Dönüş videosundan eşit aralıkla 8 kare örneklenir; ilki yüz referansıdır. Vektörler ücretsiz modda karelerin HSV histogramlarından yerel olarak hesaplanır (gerçek yüz vektörleri ücretli aşamadadır). Ses kimliği karakterde kalır.
- Görsel bir referans değişince varlığın **sürümü** artar ve varlık taslağa döner; sürüm görsel özete girdiği için o varlığı kullanan çekimler yeniden üretilir. Onay ve kilit durumu özete girmez: kilitlemek tek başına yeniden üretim tetiklemez. Aynı dosyayı yeniden yüklemek hiçbir şeyi değiştirmez. Kilitli varlık değiştirilemez; önce kilidi açılır.
- **Hareket testi** her üretim gibi bütçe rezervasyonundan geçer (ücretsiz modda sanal, MiniMax H3'ün 480p GPU süresi fiyatıyla) ve gerçek anomali tespitinden geçer. Test varlığın sürümüne bağlıdır: referans ya da tarif değişince eski test onaylanamaz.
- API: `GET/POST /api/projects/{id}/assets`, `GET/PATCH /api/assets/{id}`, `POST /api/assets/{id}/references?kind=still|turnaround|expressions|voice_sample&filename=...` (dosya istek gövdesinde), `POST /api/assets/{id}/references/placeholder`, `POST /api/assets/{id}/approve-still`, `/motion-test`, `/approve-motion`, `/lock`, `/unlock`, `POST /api/scenes/{id}/assets` ve `DELETE /api/scenes/{id}/assets/{asset_id}`.

## Sekanslar ve film düzeyinde ses

- Her sahne bir sekansa aittir (`SEQ01`, `SEQ02`...). Panelde sahneler sekans başlıkları altında gruplanır; başlıkta çekim sayısı, harcama ve tavan görünür.
- **Müzik sekans düzeyindedir.** Senaryo yüklenince her sekans, çekimlerinin baskın duygusuna (sürelerine göre) uygun prosedürel bir müzik kanalı alır (−8 dB, yumuşak giriş ve çıkış). Çekimlere müzik eklenmez; çekime özel fon (ör. tek çekimlik efekt) isteğe bağlıdır. Faz 11'den önce oluşturulmuş projelerde çekim müzikleri yerinde kalır ve sekans müziği eklenmez (film çift müzik çalmasın).
- **Film birleştirme:** önce sahne klipleri kendi sesleriyle (diyalog ve varsa çekime özel fon) birleştirilir; sonra zaman çizelgesi kanalları sekans aralıklarına yerleştirilir (sekansın sonunu aşmaz), diyalog müziği sidechain ile bastırır ve en sonda loudnorm uygulanır. Kanalların içerik özetleri ve zamanlamaları film anahtarına girer.
- **Besteci desteği:** `POST /api/sequences/{id}/tracks?filename=tema.wav&start_s=4&gain_db=-6&fade_in_s=1` (dosya istek gövdesinde) zamanlanmış bir stem ekler; bir sekansta birden çok stem olabilir. JSON gövdeyle (`{"mood": "joy", "gain_db": -10}`) prosedürel müzik değişir; yüklenen stem'lere dokunulmaz. Film düzeyinde kanal için `POST /api/projects/{id}/tracks`. Kanallar `PATCH`/`DELETE /api/timeline-tracks/{id}` ile düzenlenir ya da kaldırılır. Her değişiklikte film kendiliğinden yeniden birleştirilir; hiçbir çekim yeniden üretilmez.
- **Sekans bütçe tavanı:** `PATCH /api/sequences/{id}` ile `budget_cap_usd`. Rezervasyon, proje limitinin yanında sekans tavanını da aynı işlemde denetler. Tavan dolunca yalnızca o sekans durur (`capped`), sahneleri `frozen` olur ve olay yazılır; proje ve diğer sekanslar üretmeye devam eder. Tavan yükseltilince ya da kaldırılınca sekans yeniden açılır. Projeyi *Üretime devam et* ile sürdürmek tavandaki sekansı açmaz.

## Storyboard, taslak geçiş ve kaba kurgu

Revizyonu en ucuz aşamada yapmak maliyeti en çok düşüren adımdır. Bu yüzden:

- **STORYBOARD aşaması** görüntü üretiminden önce gelir: her çekim için bir kare çizilir. Ücretsiz modda kare, prosedürel motorun çekim ortasındaki karesidir ve üzerine çekim kodu, kamera, süre ve replik yazılır; bedeli tek bir Flux karesinin fiyatıyla sanal olarak bütçeden düşer (ücretli Flux karesi Faz 18'de). Kare yalnızca görüntü yeniden üretilecekse ve çekimin görsel girdileri değiştiyse yeniden çizilir; bitmiş çekimler için kare istenmez.
- **Kare onayı:** *Storyboard* sayfasında her kare için **Onayla** ya da not bırakıp **Değişiklik iste**. Görsel girdileri değişen çekimin karesi yeniden çizilir ve onay yeniden istenir (stil değişikliği dahil). **Görüntü üretimi için kare onayı zorunlu** (`require_board_approval`, varsayılan kapalı) açıkken onaylanmamış çekimde hiçbir varyasyon üretilmez: üretim isteği yalnızca kareyi çizer ve çekimi *Kare onay bekliyor* durumunda bırakır; kontrol worker'da da yapılır. **Kareleri hazırla** görüntü üretmeden yalnızca kareleri çizer (varlık kontrolü uygulanmaz: storyboard varlıklar kilitlenmeden önce de hazırlanabilir).
- **Storyboard PDF'i:** `GET /api/projects/{id}/storyboard.pdf` (panelde **PDF indir**). Sayfa başına 6 kare; her sekans yeni sayfada başlar; çekim kodu, kamera, süre, diyalog, onay durumu ve not yazılır.
- **Taslak geçiş** (`POST /api/projects/{id}/draft-pass`, panelde **Taslak geçişi başlat**): görüntüsü üretilecek her çekim (onay zorunluysa yalnızca onaylılar) yalnızca test turuyla üretilir ve test turunda geçen ilk varyasyon kullanılır; tam tur hiç çalışmaz. Son taslak çekim bitince **kaba kurgu** kendiliğinden birleştirilir.
- **İyileştirme geçişi:** kaba kurguda zayıf bulunan çekimler sahne satırındaki **İyileştir** ile işaretlenir; `POST /api/projects/{id}/upgrade-pass` yalnızca işaretli taslak çekimlerde tam turu çalıştırır. Test turu klipleri aynı seed'lerle yeniden kullanılır: yalnızca tam tur dalgaları ücretlendirilir. Taslak klibi kaba kurgu için saklanır.
- Film bölümünde **Kaba kurgu** ve **Final** ayrı filmlerdir (`POST /api/projects/{id}/assemble?kind=draft`). Kaba kurgu taslak klipleri, final ise her çekimin şimdiki montajını kullanır.
- Proje ayarları sonradan `PATCH /api/projects/{id}` ile değiştirilebilir (ör. `{"settings": {"require_board_approval": true}}`).

## Üretim yöntemi, dış çekimler ve elle düzeltme

Karmaşık ya da çok özel hareketleri tarif ederek üretmek zordur. Profesyonel ekipler bu çekimleri Blender'da elle canlandırır, üretilen görselleri de elle düzeltir. Her çekimin bir **yöntemi** vardır (denetçide *Yöntem*):

- **Yapay zekâ üretimi** (varsayılan): kademeli üretim.
- **Previs rehberi:** `POST /api/scenes/{id}/guide?filename=previs.mp4` (dosya istek gövdesinde) 3D taslak videoyu yükler. Görüntü aşaması rehberden çıkar: ücretsiz modda rehber, etkin stil kılavuzunun ışık diline göre renklendirilip doğrudan kullanılır (bedeli bir video-to-video geçişinin sanal fiyatı); ücretli modda video-to-video stil aktarımı Faz 18'dedir ve para harcanmadan durur. Rehber dosyanın özeti görsel özete girer; varlık ve kare onayı kapıları uygulanır.
- **Dış çekim:** `POST /api/scenes/{id}/external?filename=cekim.mp4` dışarıda bitirilmiş videoyu yükler. Görüntü üretilmez ve storyboard çizilmez; klip anomali kontrolünden geçer (siyah ya da donmuş klip reddedilir), rafine #1 olur, sonra ses ve montajdan geçer. Dosyanın içerik özeti görsel özetin yerine geçer. Hiçbir üretim harcaması yapılmaz.
- Yöntem `POST /api/scenes/{id}/method` ile değişir. Dış çekime geçerken önceki üretilmiş klipler silinmez; üretime dönülürse yeniden kullanılır.
- **Yönlendirici önerisi:** aksiyon yoğunluğu çok yüksek (≥ 0,8) ve kamera hareketi özel (el kamerası, takip, yörünge, vinç) olan çekimde panel previs rehberini önerir; karar yönetmenindir.

**Elle düzeltme:** `POST /api/variations/{id}/revisions?kind=video|first_frame&filename=...&note=...` düzenlenmiş videoyu ya da ilk kareyi yükler. Her revizyon yeni bir sürümdür ve orijinal varyasyon olduğu gibi kalır; aynı dosyayı yeniden yüklemek yeni sürüm açmaz, kusurlu revizyon reddedilir. İlk kare düzeltmesi ücretsiz modda klibin başında kısa süre durup orijinale erir (ücretli modda düzenlenmiş kareden yeniden üretim Faz 18'dedir). Düzeltilen klip rafine #1 ise dudak senkronu anahtarı revizyon kimliğini içerir ve yalnızca dudak senkronu ile montaj yeniden çalışır. Geçmiş `GET /api/variations/{id}/revisions` ile görülür; `POST /api/variations/{id}/revisions/active` başka bir sürüme ya da orijinale döner.

## Renk aşaması ve süreklilik

- **GRADE aşaması** dudak senkronu ile montaj arasında çalışır: önce film geneli 3B LUT (`.cube`, FFmpeg `lut3d`), sonra çekimin ayarı: pozlama (EV), kontrast, doygunluk, gama (FFmpeg `eq`) ve renk sıcaklığı (Kelvin; 6500 nötr, düşük değer ısıtır). Ayar ve LUT bir özet olarak planlayıcıya girer: yalnızca renk değişince yalnızca renk ve montaj çalışır, görüntü ve dudak senkronu çalışmaz. Nötr ayar ve LUT yoksa aşama hiç çalışmaz.
- **Renk** sayfasında kaydırıcılı ayarlar, çekimin orta karesinden **önce/sonra** karşılaştırması (kaydetmeden önizleme) ve *Rengi uygula* / *Nötre dön* vardır. API: `POST /api/scenes/{id}/grade`, `GET /api/scenes/{id}/grade/preview.jpg?before=true` ya da ayarlarla.
- **Film LUT'u:** `POST /api/projects/{id}/lut?filename=film.cube` (dosya istek gövdesinde; biçim doğrulanır) ve `DELETE`. Bitmiş çekimlerin yalnızca rengi ve montajı yenilenir; görüntüsü üretilmemiş çekimler beklemez.
- **Süreklilik kontrolü** (`quality/consistency.py`): aynı sekanstaki ardışık çekimlerin orta karelerinden HSV histogramları çıkarılır ve Bhattacharyya mesafesi raporlanır. Histogram mesafesi kompozisyon farkını da ölçtüğü için karar, histogramlardan türetilen ortalama parlaklık, sıcaklık ve doygunluk farkıyla verilir (ölçekler 40, 40, 80: aynı paletteki farklı çekimler arasındaki farkın üstünde). Film birleşince kendiliğinden, panelde *Sürekliliği denetle* ile elle çalışır; montajı değişen çekimin geçişleri kendiliğinden yeniden ölçülür. API: `POST/GET /api/projects/{id}/continuity`.
- **Önerilen düzeltme:** sıçrama olan geçişte sonraki çekim için, önceki çekimin parlaklık ve sıcaklığına yaklaştıran ayar aranır. Aday ayarlar çekimin renklendirilmemiş karesine GRADE aşamasıyla birebir aynı filtreyle uygulanıp ölçülür; öneri bir tahmin değil, ölçülmüş bir sonuçtur. Panelde kesme şeridinin üstünde uyarı ve **Öneriyi uygula** düğmesi çıkar; sıçrama olan kesme kırmızı çizgiyle işaretlenir.

## Kürasyon kayıtları, verimlilik ve dışa aktarma

Rekabet avantajı aynı kaliteye daha az üretimle ulaşmak ve yönetmenin zevkini veriye dönüştürmektir.

- **Kürasyon kaydı:** durum değiştiren her API ucu (dolayısıyla her panel eylemi) başarılı olduğunda `curation_events` tablosuna bir satır yazar: eylem, sahne, varyasyon, istek, yanıtın özeti ve kalite modeli için bağlam (varyasyonun skoru, sırası, seed'i, anomalileri; sahnenin durumu, yöntemi, kesim katmanı). Başlıca eylemler: `promote` (havuzdan seçim), `upgrade_request`, `board_approve`, `board_reject`, `asset_lock`, `revision_upload`, `style_activate`, `force_rerender`. Reddedilen istekler (ör. dondurulmuş projede 423) kaydedilmez. Dışa aktarım: `GET /api/projects/{id}/curation.jsonl` (her satır bir karar); son kararlar `GET /api/projects/{id}/curation?limit=30`. İleride projeye özel kalite modeli bu veriyle eğitilecektir.
- **Verimlilik metrikleri** (`GET /api/projects/{id}/metrics`, panelde *Teslim* sayfasının raporu): kabul edilen çekim başına üretilen varyasyon (toplam ve sekans bazında), bitmiş film dakikası başına sanal maliyet, sekansların varyasyon ve harcama payları, eleme nedenleri (kalite kontrolü, havuza giremedi, yeni üretimle değişti, üretilemedi…), test turu başarı oranı ve ortalama düzeltme turu, kaba kurgudan finale iyileştirilen çekim oranı, harcama kalemleri. Varyasyon sayımına yalnızca çekim üretimleri girer (hareket testleri varlığa aittir, dış çekim bir üretim değildir); harcama projenin defterinden gelir.
- **Profesyonel dışa aktarma** (`GET /api/projects/{id}/export?format=...`, birleştirilmiş filmin kesme listesinden; film yoksa 409):
  - `edl`: CMX3600 EDL, 24 fps, non-drop. Her çekim bir olaydır; makara adı çekim kodudur (`S001SH01`), kayıt zaman kodları kesme listesinin in/out değerleridir, `FROM CLIP NAME` paketteki dosyayı gösterir.
  - `otio`: OpenTimelineIO JSON (`Timeline.1`; görüntü ve ses izleri, renk sıçraması olan çekimlerde işaretçi).
  - `package`: zip; `film.edl`, `film.otio`, `kesme_listesi.json`, `kurasyon.jsonl` ve her çekim için `NNN_KOD/` klasörü: `final.mp4`, `rafine/` (havuzdaki adaylar, revizyonlar dahil), `stems/dialogue.wav`, `music.wav`, `sfx.wav` (48 kHz, çekim süresinde; sekans müziği çekime düşen dilimiyle) ve `cekim.json`. EDL ile birlikte açıldığında stem'ler çekimlerin yerine oturur.

## Kalite skoru

Kalite kapısı her klibi (ücretli modda önce ilk kareyi) **birleşik skorla** ve gerçek anomali tespitiyle (FFmpeg: siyah, donmuş, okunamayan) yargılar. Skor iki türlüdür; panelin üst bandı hangisinin kullanıldığını gösterir:

- **Sahte** (ücretsiz modun varsayılanı): seed'e bağlı, tekrarlanabilir Gauss dağılımlı skor. Yerel prosedürel klipler gerçek bir sahneye benzemediği için ücretsiz akış (duman testi dahil) bununla çalışır.
- **Gerçek** (`app/quality/scorer.py`; ücretli modun varsayılanı, `QUALITY_SCORER=real` ile ücretsiz modda da): yerel modellerle ve ücretsizdir, ağ kullanmaz.
  - **Stil ve istem uyumu** (OpenCLIP, çok dilli XLM-RoBERTa ViT-B-32): klipten eşit aralıkla 4 kare alınır; her kare stil kılavuzunun ışık ve hareket diliyle ve çekimin (Türkçe) tarifiyle karşılaştırılır. Rakip metinler kusur tarifleri (bulanık, bozuk, siyah, tek renk, gürültü, yazı) ve genel altyazılardır ("an image", "a photo"): tarif görüntüyü genel bir altyazıdan daha iyi açıklamalıdır. Olumlu metnin softmax olasılığı alınır (0–1).
  - **Yüz kimliği** (YuNet yüz bulucu + SFace): karakterlerin onaylı referans görsellerindeki yüzler, karelerde bulunan yüzlerle karşılaştırılır; OpenCV'nin aynı kişi eşiği (kosinüs 0,363) çevresinde 0–1'e çevrilir. Karede yüz yoksa (geniş plan, arkası dönük) skoru düşürmez.
  - **Birleşik skor** = stil ve istem uyumu; yüz görünüyorsa %60 uyum + %40 kimlik (`SCORE_FACE_WEIGHT`). Eşikler proje ayarlarındaki `clip_threshold` (0,85) ve `excellent_threshold` (0,92) değerleridir.
  - Ölçüm dökümü varyasyonla saklanır (`params.score_detail`) ve panelde kare açıklamasında görünür: ör. "stil 0,91, tarif 0,97, Kerem yüzü 0,66".
- Ölçümler: altın saat stilindeki kareler 0,81–0,98, aynı sahnenin başka paletli kareleri çoğunlukla 0,25'in altında; bir portre kendi Türkçe tarifiyle 1,00, ilgisiz bir tarifle 0,005; siyah, tek renk ve gürültü ~0; aynı kişinin beş yıl arayla çekilmiş iki fotoğrafı kimlikte 0,997, başka biri 0,005. **Kalibrasyon gerçek üretimle henüz sınanmadı**: ilk ücretli partide eşikler, rakip metinler ve yüz payı gözden geçirilmeli; kürasyon kayıtları bunun verisidir. Yüz modelleri insan yüzüyle eğitilmiştir; çok stilize karakterlerde kimlik ölçümü zayıf kalabilir.
- **Lisanslar**: OpenCLIP modeli (LAION) ve YuNet MIT, SFace Apache 2.0; ticari kullanıma açıktır. Spesifikasyonda adı geçen InsightFace'in hazır modelleri (buffalo_l) yalnızca ticari olmayan araştırma lisanslı olduğu için kullanılmadı. Model dosyaları sürüme ve SHA-256 özetine sabitlenmiştir (`scripts/fetch_models.py`).

## Stil kılavuzu

Çekimlerin aynı filme ait görünmesini en çok tutarlı ışık dili sağlar. Profesyonel ekipler bir formül bulup (ör. suluboya mekân + altın saat ışığı) her çekimde aynı ifadeyle tekrarlar. Bu yüzden:

- Stil kılavuzu proje başına **sürümlüdür**: ışık öneki, hareket tarzı, render kuralları (varlık türü → `clean_3d`, `painterly`, `stylized_2d` ya da serbest metin), negatifler, palet ve notlar. Her düzenleme yeni bir sürümdür; aynı anda tek sürüm etkindir ve eski bir sürüm yeniden etkinleştirilebilir.
- **Prompt bestecisi** (`style/composer.py`) sağlayıcılara giden prompt'un tek kaynağıdır ve her zaman aynı sırayı izler: ışık öneki (ve palet), hareket tarzı, çekim tarifi ve kamera, her varlığın kendi render kuralıyla tarifi, "yalnızca listelenen karakterler" kısıtı, negatifler. Aynı sürümdeki her çekimin prompt'u aynı önekle başlar. Hareket testleri de besteciden geçer.
- Hareket eden şeyler temiz 3D, çevre boyalıdır (varsayılan kurallar): yoğun boyalı doku hareket halinde bozulur. Bir varlığın `render_style` alanı boşsa kılavuzun tür kuralı geçerlidir; doluysa yalnızca o varlık için istisnadır.
- Stil çekim bazında yazılamaz: bir sahnenin istemine `lighting`, `style`, `motion_style` gibi anahtarlar eklemek reddedilir.
- **Etkin sürüm görsel özete girer.** Bu yüzden değişiklik iki adımdır: `POST /api/projects/{id}/style/preview` hangi üretilmiş çekimlerin yeniden üretileceğini ve tahmini sanal maliyeti (beklenen: bir test turu + bir tam dalga; en kötü: tüm düzeltme turları + `full_n`) döner, hiçbir şey yazmaz. Yönetmen onaylarsa `POST /api/projects/{id}/style/activate` (`render: true` ile etkilenen çekimler hemen kuyruğa girer). Sürüm kaydetmek (`POST /api/projects/{id}/style`) hiçbir çekimi etkilemez.
- Ücretsiz modda bile stilin etkisi görünür: prosedürel motor ışık önekindeki anahtar kelimeleri yorumlar. `golden-hour` (ya da "altın saat") sıcak ve alçak bir güneş, uzun gölgeler ve ufka yakın sıcak parıltı verir; `night`/`moonlight` ay ışığı, `overcast` yumuşak ve soluk ışık. Tanınmayan bir önek render'ı hiç değiştirmez.

## Bütçe ve Kill-Switch

- Her çağrı önce `reserve` (atomik, limit aşılacaksa reddeder ve projeyi dondurur), sonra `commit` ya da hata durumunda `release` yapar.
- `spent ≥ limit × soft_budget_ratio` olunca *yumuşak limit* bayrağı kalkar: yeni tam dalga başlatılmaz, sahneler eldeki adaylarla tamamlanır.
- Hız bekçisi 30 saniyede bir son 10 dakikanın harcamasına bakar; `max_spend_per_10min` aşılırsa projeyi dondurur. Panel, yeni projeyi bu sınır bütçenin yarısı olacak şekilde açar: yerel motor saniyeler içinde üretir ve Seedance seçilmiş çekimler varsayılan 20 USD sınırını filmin ortasında doldurabilirdi.
- Bayat rezervasyon toplayıcı 5 dakikada bir, 60 dakikadan eski ve eşi olmayan rezervasyonları iade eder.
- Fiyatlar `backend/pricing.yaml` dosyasındadır: fal.ai fiyatları (Ekim 2026) ve MiniMax H3 için kiraladığınız makinenin saatlik ücreti ile video saniyesi başına GPU süresi (`self_hosted.h3`). Periyodik olarak güncelleyin.

## Servis geçmişi (panelin Geçmiş sayfası)

Ücretli servislere yapılan her çağrı ayrıntılı bir kayıt bırakır; ücretsiz modda aynı iş, ücretli modda harcanacak tutarla
**sanal** olarak kaydedilir. Üst banttaki **Geçmiş** düğmesi (ya da Ayarlar'daki servis sayfasından **Geçmiş**) açar:

- **Çağrılar:** zaman, işlem (video üretimi, ilk kare, storyboard karesi, varlık referansı, hareket testi, previs rehberinden
  video, düzenlenmiş kareden video, dudak senkronu, diyalog seslendirme, senaryo ayrıştırma), proje ve çekim ya da varlık,
  servis ve model, mod (gerçek ya da sanal), durum ve tutar. Proje, servis, mod, durum ve dönem süzgeçleri URL'de durur.
- **Ayrıntı:** sağlayıcıya gerçekten giden her istek (model ya da uç nokta, sağlayıcının istek kimliği, çözünürlük, kare sayısı,
  tohum, LoRA, belirteç ya da karakter sayısı, istem), tahmin ve işlenen tutar, süre, deneme numarası, bütçe rezervasyonu ve
  varsa hata (anahtar değerleri maskelenir). İstek kimliğiyle tutarı sağlayıcının panelinde doğrulayabilirsiniz; fal.ai yanıtı
  fatura tutarı döndürmediği için işlenen tutar fiyat tablosundan hesaplanır. MiniMax H3 işlerinde görev (t2va, fl2va,
  ref2va), adım sayısı ve sunucunun bildirdiği GPU süresi görünür; tutar bu süre × saatlik ücrettir.
- **Özet:** gerçek ve sanal harcama, başarısız ve süren çağrılar; servis, model, işlem, proje ve gün dağılımı.
- **CSV indir:** süzgece uyan bütün çağrılar (UTF-8, Excel uyumlu).

Kayıt bütçe defteriyle aynı işlemde yazılır (`service_calls` tablosu, `app/budget/history.py`): rezervasyon satırı açar,
commit ya da release kapatır; geçmiş ile bütçe birbirinden ayrışmaz. Proje silinse de kayıt kalır (gerçek para harcanmıştır).
Ayarlar sayfasındaki servis harcamaları da bu kayıttan gelir ve gerçek ile sanal ayrı gösterilir. Bu sistemden önceki defter
kayıtları ilk açılışta aktarılır: modu ayar değişiklik kaydından, modeli etiketten çıkarılır ve panelde "tahmini" diye
işaretlenir. API: `GET /api/history`, `GET /api/history/summary`, `GET /api/history.csv`.

## Ücretli servisler (panelin Ayarlar sayfası)

Üst banttaki **Ayarlar** düğmesi (ya da mod rozeti) bütün ücretli servisleri tek yerde gösterir ve düzenletir:

- **Servisler:** MiniMax H3 · vast.ai (video; kendi GPU sunucunuz), fal.ai (isteğe bağlı: Seedance, Flux kareleri, previs
  aktarımı, dudak senkronu), ElevenLabs (seslendirme) ve Anthropic (Hermes LLM). Her biri için anahtarın durumu (yalnızca maskeli: son 4 hane), **Bağlantıyı sına**, model ve uç nokta kimlikleri
  (varsayılana dönme ve model sayfası bağlantısıyla), motor seçimi (`TTS_ENGINE`, `HERMES_MODE`), kullanıldığı yerler, ilgili
  fiyatlar, bugüne kadarki harcama ve sağlayıcının hesap paneli bağlantısı.
- **Çalışma modu:** ücretsiz moddan ücretli moda geçiş, kutuya **ücretli moda geç** yazılarak onaylanır ve herhangi bir projede
  üretim sürerken reddedilir. Ücretsiz moda dönüş onaysızdır.
- **Fiyat tablosu:** `backend/pricing.yaml` panelde düzenlenir; kaydetmeden önce kodun kullandığı her fiyatın var ve pozitif
  olduğu denetlenir, yorumlar korunur. Bütçe rezervasyonları hemen yeni fiyatlarla yapılır.
- **Değişiklik kaydı:** kim neyi ne zaman değiştirdi (anahtar değerleri kaydedilmez).

Değişiklikler `.env` dosyasına (fiyatlar `backend/pricing.yaml`'a) yazılır; docker-compose bu dosyaları `api`, `worker`,
`worker-gen` ve `beat` konteynerlerine bağlar. Servisler **yeniden başlatılmadan** yeni ayarı kullanır: kayıt Redis'teki bir sayacı artırır, API
her istekte ve worker her görevden önce sayacı görüp `.env`'i yeniden okur. Panel yalnızca beyaz listedeki ücretli servis
ayarlarını yazabilir; veritabanı ve depo ayarlarına dokunamaz. Bağlantı sınaması yalnızca ücretsiz, salt okunur doğrulama
uçlarına gider (Anthropic model listesi, ElevenLabs hesap bilgisi, fal.ai kuyruk durumu, H3 sunucusunun `/health` ucu) ve
üretim yapmaz.

### MiniMax H3 ile ücretli deneme

ElevenLabs, Anthropic ve fal.ai olmadan, yalnızca kendi H3 sunucunuzla gerçek üretimi denemek için (sunucu kurulumu:
*Video motoru: MiniMax H3* bölümü):

1. **Ayarlar › MiniMax H3 · RunPod**: RunPod API anahtarını ve uç nokta kimliğini girip kaydedin (volume, worker imajı ve
   uç nokta kurulumu: `backend/runpod/README.md`), ardından **Bağlantıyı sına**: bakiye, uç noktanın veri merkezi (izinli
   liste) ve worker durumu görünmeli. Ücretli moda geçtikten sonra **Ayarları uç noktaya uygula** GPU tiplerini, veri merkezini,
   volume'u, en az 0 worker'ı ve boşta bekleme süresini uç noktaya yazar. Konuşma motoru `espeak`, Hermes `rules` ve fal.ai anahtarı boş kaldıkça diğer servisler
   çağrılmaz; Çalışma modu listesinde "gerekmez" görünür.
2. **Ayarlar › Çalışma modu**: kutuya "ücretli moda geç" yazıp onaylayın (RunPod anahtarı ve uç noktası ya da kalite
   skorlayıcısı hazır değilse düğme kapalıdır).
3. **Senaryo › Yeni proje**: yoğunluk olarak **MiniMax H3 denemesi** seçin. Bu seçim:
   - bütçeyi 3 USD yapar;
   - **Deneme senaryosu**nu doldurur: İngilizce, tek çekim, 3 saniye, diyalogsuz; Kerem `[CHARACTER: Kerem]` ile kadroya alınır;
   - çekim başına en çok 2 test ve 2 tam klip üretir, 480p, düzeltme turu yok;
   - varlık onay akışını ve hareket testlerini atlar (referans kontrolü yalnızca uyarır).
   Panel çekim başına en yüksek GPU maliyetini fiyat tablosundan hesaplar. **Senaryoyu işle ve üret**'e basın.
   Çekim FL2VA'dır: ilk kare için ComfyUI `keyframe.json`, fal.ai anahtarı ya da denetçiden yüklenen bir kare gerekir;
   hiçbiri yoksa çekim "ilk kare bekliyor" diye durur, kareyi yükleyip **Bu sahneyi baştan üret**'e basın.
4. **Kurgu** sayfasında adayları izleyin. Her klip ilk kareden H3'te üretilir ve videonun kendisi puanlanır.
5. Bitince **Ayarlar › Çalışma modu › Ücretsiz moda dön** ve kullanmıyorsanız makineyi vast.ai'de durdurun.

### Karakter referansıyla üretim

Bir karakterin görünüşünü bir görselle sabitlemek için: **Varlıklar** sayfasında karakteri seçip **Durağan görsel**
olarak yükleyin (ör. karakterin yüzünü net gösteren bir portre). Ücretli modda o karakterin geçtiği çekimlerin ilk
kareleri ve storyboard karesi Flux LoRA yerine **Flux Kontext** (`fal-ai/flux-pro/kontext`, görsel başına 0,04 USD) ile
bu görselden çizilir: yüz, saç, göz ve kıyafet korunur, sahne, poz ve kadraj prompt'tan gelir. Video yine bu kareden
(Seedance) üretilir. Görsel fal deposuna bir kez yüklenir (ücretsiz).

MiniMax H3 çekimlerinde (varsayılan) Flux kullanılmaz: karakterin durağan görseli ilk kareye Qwen-Image-Edit-2511 ile
girer (RunPod worker'ında, `edit_keyframe.json`; image 1 ana karakter), video bu kareden FL2VA ile üretilir. Çekim elle
Ref2VA'ya alınırsa çekimdeki bütün karakterlerin durağan görselleri (en çok 9) istekte referans olarak gider ve istemde
`<Picture 1> is Kerem` gibi eşlenir.

- Kontext tek görsel alır: çekimde birden çok referanslı karakter varsa ilki kullanılır. LoRA'sı olan karakterde LoRA
  kazanır.
- Kalite kapısı aynı görseli yüz kimliği için de kullanır (SFace); büyük portrelerde yüz bulunabilsin diye görsel
  ölçülmeden önce küçültülür.
- Durağan görsel yüklemek karakterin sürümünü artırır: o karakterin çekimleri bir sonraki üretimde yeniden çizilir.
- Uç nokta Ayarlar › fal.ai'den değiştirilebilir (`FAL_KONTEXT_ENDPOINT`), fiyatı `pricing.yaml`'da
  `image_per_image.flux_kontext`.

### Ücretli ses kapalı (deneme)

`paid_audio: false` proje ayarıyla (deneme ön ayarında kapalıdır) ücretli modda bile konuşma yerel espeak ile
üretilir ve dudak senkronu çağrılmaz: replikli çekimde rafine video olduğu gibi kullanılır. ElevenLabs ve fal dudak
senkronu bu projede hiç çağrılmaz. fal.ai anahtarı tanımlı değilse de dudak senkronu atlanır.

### Prompt çevirisi

Görüntü modelleri (MiniMax H3, Flux, Seedance) İngilizce istemi izler; Türkçe tarifle sahneyle ilgisiz görüntü üretirler. Ücretli
modda bestecinin çıktısındaki Türkçe metinler (çekim tarifi, mekân ve nesne adları ve tarifleri) sağlayıcıya gitmeden yerel
bir modelle (Helsinki-NLP opus-mt-tr-en, Apache 2.0, imajda sabit sürümle; ücretsiz, ağ kullanmaz) İngilizceye çevrilir.
Yalnızca Türkçe cümleler çevrilir; kamera ve stil ifadeleri ile İngilizce yazılmış senaryo olduğu gibi gider. Karakter adları
korunur ("Gül", "Işıl" kelime olarak çevrilmez). Senaryo, çekim tarifi ve görsel özet Türkçe kalır; çeviri hiçbir çekimi
yeniden üretmez. Model yoksa Türkçe tarifli ücretli üretim para harcanmadan durur. Gönderilen istem, varyasyonun kaydında
(`params.prompt`) durur. `PROMPT_TRANSLATION=off` çeviriyi kapatır, `on` ücretsiz modda da açar.

Proje ücretli moda geçmeden önce oluşturulursa çekimler hemen yerel motorla üretilir ve sonradan geçişte yeniden üretilmez;
o durumda Kurgu'da **Bu sahneyi baştan üret** kullanın. Sanal bütçe tavanı ücretli modda gerçek harcamanın da sınırıdır:
dolunca Kill-Switch üretimi durdurur.

> Panelin kimlik doğrulaması yoktur; yalnızca kendi makinenizde (localhost) çalıştırın. Ayarlar sayfası `.env` dosyasına
> yazabildiği için paneli ağa açmayın.

## Video motoru: fal.ai (taslak MiniMax H3 Max, final Seedance 2.5)

RunPod uç noktası ve volume silindi: kendi barındırdığımız MiniMax H3 **kapalıdır** (`H3_PLATFORM=kapali`, varsayılan;
dakikalık RunPod görevi ve bağlantı sınaması hiçbir RunPod çağrısı yapmaz, Ayarlar kartı "RunPod: kapalı (endpoint
silindi)" der). Kod duruyor: `H3_PLATFORM=runpod` ile yeniden açılır. Yönetmenin ücretli karşılaştırma testinde (aynı 5
referans, 10 sn, 16:9) kalite için **Seedance 2.5**, ucuz ve hızlı taslak için **MiniMax H3 Max** seçildi; Kling O3 elendi.

- **İş akışı:** yeni çekimler taslak motoruyla (`VIDEO_DRAFT_ENGINE=h3max`, proje ayarı `draft_engine` önce gelir) üretilir:
  çekim başına tek klip (kalite kapısından kalırsa en çok `fal_clip_attempts`, varsayılan 2). Beğenilen çekimde denetçideki
  **Seedance 2.5 ile yükselt** (ya da "İyileştir" işareti + iyileştirme geçişi) final motoruyla (`VIDEO_FINAL_ENGINE=seedance25`)
  yeniden üretir: aynı kare ve referanslar kullanılır (görsel özet motor ailesini görür, kare yeniden çizilmez), eski klip
  varyasyon olarak kalır. Eski `h3` çekimleri hata vermez; denetçi "Bu motor kapalı; H3 Max'e geç" önerir (tek tık).
- **Mod (çekim başına):** onaylı başlangıç karesi (yüklenen kare ya da onaylanmış storyboard karesi) varsa image-to-video
  (bitiş karesi varsa `end_image_url`), yoksa reference-to-video: karakter, nesne ve mekân referans görselleri bu sırayla
  numaralanır, istem her birini tarifi ve numarasıyla yazar (Seedance `@Image1`, H3 Max `Image 1`; tarifteki `[Image1]` de
  kabul edilir). Denetçideki koşul seçici (`h3_mode`) bu iki moda eşlenir; seçilen modun gerekçesi denetçide yazar.
- **Kuyruk:** submit → status → result; `request_id` çekime yazılır (`scenes.fal_request`), görev yeniden başlarsa ya da panel
  kapanıp açılırsa bekleyen iş yeniden gönderilmez, sürdürülür. 4xx (ör. 422) yeniden denenmez, mesaj denetçide olduğu
  gibi görünür; yalnızca 5xx'te bir yeniden gönderim; zaman aşımında (Seedance 10 dk, H3 Max 6 dk) aynı iş bir pencere daha
  beklenir, sonra iptal edilir. Yerel dosyalar fal deposuna SHA-256 önbellekli yüklenir; çıktı gelir gelmez indirilip yerel
  depoya yazılır. Kayda tohum, `expanded_prompt` (H3), `draft_id` (Seedance) ve tahmini faturalanan birimler düşer.
- **Maliyet (bütçe kapısı çağrıdan önce):** Seedance 2.5 token = yükseklik × genişlik × (girdi videosu sn + çıktı sn) × 24 /
  1024; 480p/720p 0,0214 $, 1080p 0,0234 $ / 1000 token, video referansı varsa × 0,6; rezervasyona %25 pay (test: formül
  216 bin, fatura 260,28 bin token = 5,57 $). H3 Max: süre × 0,05 / 0,08 / 0,16 $ (480P / 768P / 1080P) + her girdi görseli
  1.024 token (4.096 dahil, fazlası 0,02 $ / 1000), × 1,05 (test: 10 sn 768P + 5 görsel ≈ 0,86 $). `pricing.yaml › fal_video`.
- **Kuru çalıştır:** `POST /api/scenes/{id}/render?dry_run=true` ve `GET /api/projects/{id}/render-plan?dry_run=true`
  fal.ai'ye gitmeden uç noktayı, tam istek gövdesini (dosyalar yerel anahtar ve SHA-256 olarak; yükleme yapılmaz), referans
  numaralandırmasını, maliyet tahminini ve motor/mod gerekçesini döndürür.
- **Kareler fal.ai'de** (`KEYFRAME_PLATFORM=fal`): Qwen-Image-2512 (`fal-ai/qwen-image-2512`, 0,02 $/MP), Qwen-Image-Edit-2511
  (`fal-ai/qwen-image-edit-2511`, 0,03 $/MP; zincir, compose, kırp + iyileştir), açı değişimi Multiple Angles LoRA ile
  (`fal-ai/qwen-image-edit-2511/lora`, 0,035 $/MP; `loras[].path` Hugging Face adresi, istem "<sks> azimut yükseklik
  uzaklık" biçiminde). Hazır `fal-ai/qwen-image-edit-2511-multiple-angles` uç noktası açıyı sayıyla ister ve "<sks>" metnini
  almaz, bu yüzden kullanılmaz. Kapılar (kimlik, kopya, ölçek, renk, çerçeve, sıçrama) değişmedi. Lightning'in fal karşılığı
  yok: `lightning` = 10 adım + `acceleration: high` (fiyat adımdan bağımsız).
- **Lisans:** fal.ai'deki MiniMax H3 Max "Commercial use" etiketlidir: çıktılarına lisans ya da bölge kısıtı uygulanmaz
  (yalnızca kendi barındırdığımız MiniMax H3'ün kısıtları sürer).

### En düşük maliyet, taslak tamamlama ve upscale

- **Varsayılanlar en ucuz** (test aşaması; proje ayarlarından yükseltilir): H3 Max 480P; Seedance 2.5 taslak (`draft:
  true`, 480p önizleme + `draft_id`). `SEEDANCE25_RESOLUTION` yalnızca taslak kapalıyken kullanılır.
- **Fiyatlar uç noktaya göre:** H3 Max image-to-video 0,03 / 0,048 / 0,096 $/sn (480P / 768P / 1080P), reference-to-video
  0,05 / 0,08 / 0,16 $/sn + 4.096 token'ı aşan referans için 0,02 $ / 1000 token. Seedance taslağı 480p token fiyatıyla
  (0,0214 $ / 1000 token). draft/complete yalnızca 1080p, 0,0234 $ / 1000 token (≈ 1,164 $/sn; 10 sn ≈ 11,6 $), tahmine
  %20 pay.
- **Taslağı 1080p'ye tamamla:** taslağın `draft_id`'si ve 7 günlük geçerliliği sahneye yazılır, panelde kalan süre görünür.
  Onay ekranı tahmini (ör. "≈ 11,6 $") ve daha ucuz seçeneği ("Upscale ile 1080p (≈ 0,07 $)") gösterir; bütçeyi aşan ya da
  süresi dolmuş tamamlama reddedilir. Tamamlanan parça klibi yeniden çekimlere kesilir; taslak kesimleri varyasyon kalır.
- **Upscale (1080p):** klip, çekim ya da son montajdan önce bütün film; `UPSCALE_ENDPOINT` (varsayılan
  `fal-ai/bytedance-upscaler/upscale/video`: 30 fps'e kadar 1080p 0,0072 $/sn, 2K 0,0144, 4K 0,0288; 60 fps'te 2 kat, pro 10
  kat). Seçenekler: `fal-ai/kandinsky6-vsr` (0,00036 $ / çıktı MP / kare; en çok ~5 sn), `fal-ai/seedvr/upscale/video`
  (0,001 $ / MP), `fal-ai/flashvsr/upscale/video` (0,0005 $ / MP), `blackforestlabs/flux-video-upscale` (precise 1080p
  0,14 $/sn; en çok 20 sn). Upscale edilmiş klip ayrı varyasyondur (orijinal korunur), hedef fps kaynakla aynıdır (24).
  Upscale edilen çekimin otomatik renk eşlemesi sıfırlanır: eşleme upscale'den SONRA (film birleşince) yeniden yapılır;
  bütün film upscale'i son çekimin montajından sonra filmi yeniden birleştirir. Bütün klipler 1080p ise film de 1080p'dir.
- **İstem:** sahne kadrosu nesne olarak tanımlı canlıları da kapsar ("The cast of this scene: Nora, Pip and the Hens");
  çekime özel kadro yalnızca kendi satırında ("Only Nora and Pip in frame; the Hens are out of frame."); kamera dereceleri
  sade dile çevrilir ("from the left side", "low at ground level"); kamera cümlesi tekrar etmez. H3 Max istemi en çok 50.000
  karakter (şema), Seedance'ta sınır yok; aşılırsa önce tekrarlar, sonra görsel kısıtlar kısaltılır; planda karakter
  sayısı görünür.

### Passport Rush taksi testinden düzeltmeler (2026-10-12)

- Seedance 2.5 taslağında (`draft: true`) `generate_audio: false` fal'den 422 "Invalid parameters" aldı: taslakta ses her
  zaman açık gönderilir (fiyat aynı), modelin sesi indirilirken silinir; filmin sesi Hell Grind'den gelir.
- 4xx'te yeniden deneme yok: parçanın bütün çekimleri hemen `failed` olur, mesaj `last_error`'da; kilidi bekleyen kardeş
  görevler parçayı yeniden üretmez. Yönetmen yeniden render edince denenebilir.
- Referans modunda varlığın durağan görsel onayı yeterlidir (`asset_min_status` en çok `still_approved`); kuru çalıştırma
  dağıtımın referans denetimini aynen hesaplar (`lint`, `blocked`).
- Referans cümleleri tarifin tamamını ve varlığın değişmezlerini yazar ("Ada must always have: …").
- Parça üretilince bütün çekimlerin kazananı aynı anda değişir; kendi görevi olmayan çekimin ses/montajı kuyruğa verilir.
  Kazanan klibin motoru çekimin motorundan farklıysa denetçi bunu açıkça yazar.
- Klibi değişen çekimler (upscale, tamamlama, yükseltme) "eşleme bekliyor" olur; projede süren iş kalmayınca son
  montajdan sonra otomatik renk eşleme kendiliğinden çalışır (ücretli modda).
- Mekân referansı yalnızca görünüm içindir: her mekân satırında "Use Image N only for the look of the place
  (architecture, materials, colors, light); do not copy its camera position or viewpoint — stage the action as described
  in each shot." Çekimde kamera yüksekliği yazılmamışsa "camera at human eye level / street level". Mekânın tarifinde
  aerial, overhead, bird's-eye, from above, overpass geçiyorsa planda sarı uyarı: "Bu mekân referansı yukarıdan çekilmiş;
  sokak seviyesinde bir görsel yüklemeyi düşünün." (Wan 3.0 testi: üst geçitten çekilmiş otoyol referansı karakteri yola
  yukarıdan koşturdu.)

### Model karışımı ve parça bölme (karma kurgu testi)

- Bir whole_scene parçasının çekimleri yalnızca AYNI üretimden gelebilir. Yönetmen bir çekimde başka üretimin (ör. başka
  motorun) varyasyonunu seçerse uyarı: "Bu çekim başka bir üretimden; geçiş tutarsız olabilir. Bütün sahneyi bu motorla
  değiştir ya da bu çekimi ayrı üret." Tek tık: **Bütün sahneyi bu motorla değiştir** (`POST /api/scenes/{id}/generation/
  adopt`: o üretimin kesimleri bütün çekimlerin kazananı olur, yeniden üretim ve ücret yok) ya da sahneyi o motorla yeniden
  üret (`/scene-engine`, tahminli onay).
- Sahne içi model değişimi (`/shot-engine`, "Bu çekimi ayrı üret"): çekim ayrı üretilir (per_shot) ve önceki çekimin
  gerçek son karesiyle başlar (H3 Max `image_url`; Seedance `image_urls` başı + "starts exactly from @Image1"); sonraki
  çekim de aynı şekilde yeniden bağlanır. Model değişimi varsayılan olarak yalnızca sahne sınırında önerilir.
- Parça klibi sabit saniyelerden değil, klipteki gerçek kesmelerden bölünür (FFmpeg sahne değişimi skoru, eşik 0,3):
  kesmeler senaryodaki çekim sırasına eşlenir; sayı uymazsa en yakın (klip süresine ölçeklenmiş) planlı sınır kullanılır
  ve uyarı yazılır. Sınırlar planda ve Kurgu'da görünür.
- Karma ya da upscale'li kurguda klibi değişen her çekim (seçim, benimseme, upscale, tamamlama, yeniden üretim) "eşleme
  bekliyor" olur; montajdan sonra otomatik renk eşleme çalışır.

### Sahne sınırı karma testi düzeltmeleri

- **Montaj gerçek uzunluğu kullanır:** referans modu klipleri (parçadan kesilen segment, onun tamamlanmış ya da
  upscale'li kopyası) dudak senkronu ve montajda kırpılmaz; `duration_s` yalnızca plandır (ör. 3,917 sn'lik segment
  3,00'e inmez). Sahne müziği de gerçek uzunluğa göre üretilir. Kare yolu klipleri eskisi gibi hedef süreye kırpılır.
- **Senaryoyu yeniden kaydetmek:** metni değişmeyen çekimlerin süresi, kamerası, duygusu ve motoru korunur (yanıtta
  `unchanged`); yalnızca metni değişen çekim yeniden ayrıştırılır.
- **Seçim asla görüntü üretmez:** "Bunu kullan", yönetmen seçimi (override), ses, renk, revizyon ve otomatik renk eşleme
  yalnızca dudak senkronu, renk ve montaj çalıştırır (`post_only`). Plan `render=false` ile onaylanınca klibi olan çekim
  bitmiş hâline döner.
- **Stil değişikliği yalnızca işaretler:** `render:false` ile etkinleştirilen stil, tamamlanmış çekimleri "eski stil"
  olarak işaretler (`scenes.style_stale`, denetçide uyarı). Toplu ve otomatik dağıtımlar (render-all, senaryo yükleme)
  bu çekimleri yeniden üretmez; yönetmen sahne başına "Bu sahneyi baştan üret" ya da `render-all?restyle=true` ile ister.
  Eski sürüme dönülürse işaret kalkar.
- **Renk eşleme tek tur:** eşlemenin GRADE + MUX işleri yeni eşleme ya da kaba kurgu tetiklemez; turun son montajından
  sonra film bir kez birleşir ve bu birleşme eşlemeyi yeniden başlatmaz. Uygulanamayan çekimin sebebi olay mesajında
  (`color_auto_failed`: çekim + sebep). "Süreklilik kontrolü" düğmesi yalnızca ölçer.
- **Eksik montaj yok:** kendiliğinden birleştirme (kaba kurgu, upscale sonrası, eşleme sonrası, müzik değişince) yalnızca
  bütün çekimler bitmişken yapılır; değilse bekleyen çekimler olaya yazılır. Elle birleştirme her zaman mümkündür.
- **"Bu çekimi ayrı üret" onayı** maliyet tahminiyle açılır (`/shot-engine?dry_run=true`: yeniden üretilecek parçalar,
  toplam, kalan bütçe); onay gösterilen tutarla gider (`expected_usd`), bütçe kapısından geçer ve planı onaylar.

### Yönetmen istemi: LLM istem yazıcı, elle istem, sade kurallar istemi

fal playground'da elle yazılmış, yönetmen tarzı istemler (referans etiketleri eylemin içinde, somut sahneleme ve
oyunculuk, ses ayrı, ~2.000 karakter) otomatik istemden belirgin şekilde iyi sonuç verdi. Parçanın istemi artık üç
kaynaktan birinden gelir; plan hangisinin kullanıldığını gösterir (öncelik sırasıyla):

1. **Yönetmen istemi:** denetçideki planda "Yönetmen istemi yaz" (`PATCH /api/scenes/{id}` `{"prompt_override": "…"}`;
   boş metin siler). Parçanın ilk çekiminin `prompt_spec.override` alanına kaydedilir ve **olduğu gibi** gönderilir;
   yalnızca referans yazımı motora göre çevrilir (`[Image1]` / `@Image1` / `Image 1`). Kaydetmek planı ve onayı değiştirir,
   klibi yeniden üretmez (görsel özete girmez). Senaryo, stil ya da parça değişince silinmez; "eski olabilir" uyarısı çıkar.
   Kaydedince denetçide **"Bu istemle yeniden üret (≈$X)"** çıkar (`POST /api/scenes/{id}/prompt/rerender`, `dry_run=true`
   tahmin): tahminli onay planı onaylar ve bütün parçayı yeniden üretir; tahmin yükselmişse ya da bütçe yetmezse reddedilir.
2. **LLM istemi** (`PROMPT_WRITER=llm`, varsayılan): `PROMPT_WRITER_MODEL` (varsayılan `claude-opus-5-5`; boşsa
   `HERMES_MODEL`) parçanın istemini yazar (`ANTHROPIC_API_KEY`). Anthropic isteklerine örnekleme parametresi
   (temperature, top_p, top_k) hiçbir modelde gönderilmez (Opus 5.5 reddediyor). Girdi: senaryo paragrafları (mekân başlığı tekrarı olmadan), çekimlerin kamera/yükseklik/süre/duygu alanları,
   diyalog, kadro (kadrajda olan/olmayan), varlık tarifleri ve değişmezleri, referans numaraları (motorun biçiminde),
   stil (ışık, hareket), motor ve ses. Sistem istemi biçimi tarif eder (stil cümlesi + referans tanımları, "Shot k (a-bs),
   ölçek, kamera:" ve somut sahneleme, her eylemde referans etiketi, sonda animasyon + ses + en çok iki kısıt) ve
   yönetmenin iyi sonuç veren iki istemini örnek verir (`backend/app/engine/prompt_examples/`: `passport_rush.txt` fal
   playground istemi, `taxi_director.txt` taksi S001 yönetmen istemi). Doğrulama: çekim sayısı, sırası ve zamanları planla birebir, her referans en az bir çekimde eylem
   içinde, mekân bakış kuralı, "Mood:" ve "Visual constraints" yok, uzunluk 700–4.000 (hedef 1.500–2.500). Geçmezse bir
   kez hatalarla yeniden yazdırılır, yine geçmezse kurallar istemine düşülür ve uyarı verilir. Yazım `prompt_cache`
   tablosunda saklanır: girdi özeti değişmedikçe yeniden yazılmaz. Planda model, belirteç ve bedel görünür; yazılmadıysa
   tahmini bedel (`pricing.yaml › llm_models`: Opus 5.5 $4/$20, Sonnet 5.5 $2/$10, Haiku 5.5 $0,10/$0,50; 100k belirteci
   aşan istemde $0,50/$2,50, 1M belirteç başına). Çağrı hata verirse olayda tam mesaj, planda "LLM yazımı başarısız:
   <mesaj> — kurallar istemi kullanıldı" görünür; 10 dakika sonra ya da "İstemi LLM ile yaz" ile yeniden denenir. Yazım zamanı: parça plan onayına düşünce arka planda (`hg.write_prompt`), onay gerekmiyorsa üretimden
   hemen önce, ya da denetçide "İstemi LLM ile yaz" (`POST /api/scenes/{id}/prompt/write`, `dry_run=true` tahmin,
   `force=true` yeniden yaz). Ücretsiz modda ya da anahtar yokken çağrılmaz; kurallar istemi kullanılır, planda uyarı.
3. **Kurallar istemi** (LLM yokken): çekim satırlarında mekân başlığı ve "Mood:" yok; referans adları eylemde ilk
   geçtikleri yerde etiketli ("Ada (Image 1)"); kısıtlar iki cümle ("The cast of this scene is exactly …; no other
   people, animals or figures appear. No subtitles or text on screen; …").

### Referans + senaryo ile video (varsayılan; görsel üretimi yok)

`VIDEO_INPUT_MODE=reference` (varsayılan; `keyframe` eski kare yoludur, Qwen kodu duruyor): hiçbir kare ya da görsel
üretilmez (Qwen, Flux, storyboard ve şerit çizimi yok; kare kapıları devreye girmez). Video yalnızca varlıkların onaylı
referans görselleri ve senaryodan üretilir.

- **Tüm sahne tek istekte** (`SCENE_RENDER_MODE=whole_scene`, varsayılan): aynı sahne başlığının çekimleri (S001_SH01,
  S001_SH02 …) tek istekte, çekim listesi biçimli istemle ("Shot 1 (0-3s), medium shot, static camera: … Nora says: "…".
  Mood: fear."). Toplam süre H3 Max'te 15, Seedance 2.5'te 30 saniyeyi aşarsa sahne çekim sınırlarından parçalara bölünür.
  Üretilen parça klibi çekim sınırlarından kesilir; her çekim kendi klibini alır (ses, renk, montaj ve kurgu değişmez).
  `per_shot`: her çekim ayrı istek.
- **Referanslar:** karakterler → nesneler → mekân, sabit sırayla numaralı (H3 Max "… is Image 1", Seedance "… is @Image1");
  gerçek boyut istemde ("Pip is tiny, palm-sized; much smaller than a hen."). Sınır: H3 Max 9 referans görseli (şema; toplam
  12 dosya), Seedance 30 (toplam 50); aşarsa çekim metinlerinde en az geçen mekân/nesne düşer, planda uyarı çıkar. Görseli
  olmayan varlık "eksik referans": render engellenir.
- **Süreklilik:** parçanın ilk çekimi `continuous` ise önceki klibin son karesi ffmpeg ile çıkarılır (üretim değil): H3
  Max'te `image_url`, referanslar `reference_image_urls`'te; Seedance'ta kare `image_urls`'ün başına konur, istem "the shot
  starts exactly from @Image1" der ve referans numaraları bir kayar. Kesmelerde kare verilmez. Önceki parça henüz
  üretilmediyse önce o üretilir.
- **Plan onayı** (`require_plan_approval`; boşsa ücretli modda açık): render'dan önce denetçideki kuru çalıştırma planı
  (motor, referanslar ve numaraları, istem, süre, maliyet) onaylanır (`POST /api/scenes/{id}/plan/approve`). Onay planın
  özetine bağlıdır: istem, referans, süre ya da motor değişince yeniden istenir.
- **Video kapıları:** üretilen klipten 0,5 sn'de bir örnek kare: kimlik (insan SFace, diğerleri OpenCLIP), kopya karakter
  sayımı, ölçek (gerçek boyut); kalırsa `fal_clip_attempts` kadar yeniden üretilir, sonra yönetmene kalır. Sonuçlar klip
  başına denetçide. Renk sürekliliği ve otomatik renk eşleme montajdan sonra.
- **Seedance 2.5 ile yükselt:** tek adımlı onay (tahmini toplam gösterilir); referans modunda bütün sahne yükseltilir, bu
  onay final planlarının onayıdır. Tahmin kalan bütçeyi aşarsa reddedilir.

## Video motoru: MiniMax H3 (RunPod Serverless) — kapalı

Ücretli modda test turu, tam tur, hareket testi, karakter dönüş videosu ve düzenlenmiş ilk kareden video **MiniMax H3**
ile üretilir. Varsayılan platform **RunPod Serverless**: modeller bir Network Volume'da (CA-MTL-1), iş geldikçe worker açılır,
iş yokken sıfır worker (kurulum, fiyat ve bütçe: `backend/runpod/README.md`). vast.ai arka ucu **deneyseldir ve kapalıdır**
(Ayarlar › MiniMax H3 · RunPod › Platform: `vast`); aşağıdaki *Sunucu*, *Yedek makine*, *Sertifika sabitleme* ve *Makine
yönetimi* bölümleri yalnızca vast.ai içindir, bilinen sorunları *vast.ai: bilinen sorunlar*'dadır. Yeni her çekim H3'e yönlenir; bir çekimi **Seedance** (fal.ai) ile
üretmek isterseniz Kurgu › denetçi › **Motor** listesinden seçin. Seçim, senaryo yeniden işlense de korunur.

> **Lisans: önce okuyun.** MiniMax H3 Community License, modelin ve **çıktılarının** (filmin) Avrupa Birliği, Birleşik
> Krallık, Güney Kore ve ABD'de kullanılmasını, çalıştırılmasını, barındırılmasını, dağıtılmasını ve gösterilmesini ayrı
> izin olmadan kapsamaz (§V.4). Çıktılar MiniMax H3 ve türevleri dışındaki hiçbir yapay zekâ modelini geliştirmek için
> kullanılamaz (§V.3). Yıllık geliri 20 milyon USD'yi aşan ticari kullanım yazılı izin ister; ticari ürünün arayüzünde
> "MiniMax H3" görünmelidir. Bu bir hukuki görüş değildir: https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE

### Bölge: izinli liste

Yasaklı liste yerine **izinli liste** kullanılır (`backend/app/core/regions.py`, her arka uç): yalnızca açıkça onaylanmış
veri merkezleri ve ülkeler kabul edilir, konumu bilinmeyen makine ya da teklif hiçbir zaman seçilmez.

- **RunPod:** varsayılan **CA-MTL-1, CA-MTL-2, CA-MTL-3** (Kanada). **EUR-NO-1** (Norveç) ve **EUR-IS-*** (İzlanda) Ayarlar ›
  **Kuzey veri merkezleri** açıkken (varsayılan kapalı). Uç noktanın veri merkezi kısıtı RunPod'dan okunur; kısıt yoksa,
  izinli olmayan bir merkez varsa ya da okunamazsa üretim başlamaz.
- **vast.ai:** varsayılan yalnızca Kanada; Kuzey açıkken Norveç ve İzlanda.
- **Her zaman yasaklı** (izinli listede olsa da): AB, Birleşik Krallık, Güney Kore, ABD ve denizaşırı toprakları. ABD:
  Porto Riko, Guam, ABD Virjin Adaları, Amerikan Samoası, Kuzey Mariana Adaları, ABD Küçük Dış Adaları. AB en dış bölgeleri:
  Kanarya Adaları, Fransız Guyanası, Réunion, Guadeloupe, Martinik, Mayotte, Saint-Martin, Azorlar, Madeira. Panel daha önce
  iki kez Porto Riko önermişti: ülke kodu yasaklı listede olmadığı için geçiyordu.

### Kompozisyon: seed değil koşul

Aynı seed farklı çözünürlükte farklı kompozisyon verir; bu yüzden H3 çekimlerinde kompozisyon koşulla kontrol edilir.
Denetçideki **H3 koşulu** her çekim için seçilir:

- **İlk kareden (FL2VA, varsayılan; karakterli çekimlerde de):** çekimin storyboard karesinin yazısız hâli (ya da
  yönetmenin yüklediği kare) her varyasyonun ilk karesidir; test turu (480p) ve tam tur (720p) aynı kareden başlar. Ücretli
  modda kare RunPod worker'ında çizilir: karakterli çekimde **Qwen-Image-Edit-2511** ile karakterin onaylı referansından
  (`edit_keyframe.json`; image 1 ana karakter — çekim tarifinde adı ilk geçen —, image 2 / 3 ikinci karakter ya da mekân;
  istem "Elif is the person in image 1" diye başlar, ardından çekim tarifi, kamera ve proje stili), karaktersiz çekimde
  Qwen-Image ile metinden (`keyframe.json`). Ayarlar: `KEYFRAME_CHARACTER_PROVIDER` (qwen_edit varsayılan | qwen) ve
  `QWEN_EDIT_MODE` (Tam 40 adım cfg 4 / Orta 20 adım cfg 4 / Lightning 4 adım cfg 1; projede Senaryo › Yoğunluk'tan da
  seçilir). Kare videoyla aynı kalite kapısından geçer;
  Qwen-Image-Edit karesinde ayrıca karedeki yüz referansa SFace ile en az `identity_threshold` (0,60) benzemelidir. Geçemezse
  yeni seed'le en çok 2 kez yeniden çizilir (her deneme maliyetiyle harcama geçmişine yazılır); yine olmazsa çekim "Kare
  inceleme gerekiyor" olur, kare ve skor denetçide görünür ve video üretilmez (**Kareyi yeniden çiz** yeni seed'ler dener;
  **Kareyi elle onayla** kareyi gözle kabul edip video aşamasına geçer). Robot, hayvan gibi karakterler Varlıklar'da
  "İnsan değil" olarak işaretlenir: onlarda yüz aranmaz, görsel benzerlik (OpenCLIP ve DINOv2) ölçülür. Varlıklarda
  **Değişmez özellikler** (ör. Bolt: "exactly one bent antenna with a small bulb") her kare ve video istemine eklenir.
  "Kimlik kayabilir" uyarısı yalnızca karakterli çekimin karesi düz Qwen-Image'dan geldiyse çıkar. Seedance çekimleri de
  aynı kareyi image-to-video başlangıcı olarak kullanır. Kareyi yine de
  onaylayabilir ya da **Kendi ilk kareni yükle** ile kendi karenizi verebilirsiniz (yüklenen kare storyboard'un yerine geçer,
  çizim ve ücret yoktur). İlk kare kaynağı yoksa (ne `keyframe.json` ne fal.ai anahtarı) FL2VA çekimi üretilmez ve neden
  denetçide yazar.
- **Sahneler arası süreklilik (anahtar kare zinciri):** bir sekansta çekimlerin ilk kareleri sırayla çizilir (K1, K2 …).
  Kn, Qwen-Image-Edit ile image 1 = K(n-1) ve karakterlerin durağan referanslarıyla çizilir (dünya tutarlılığı: oda,
  nesneler, ışık); yeni sahne başlığı zinciri kırar (kare referanslardan). Kn çizilmeden K(n+1) çizilmez; kare kalite
  kapısı Kn ile K(n-1) arasında karakter bölgesi dışındaki mekân benzerliğini (DINOv2) ölçer (`continuity_threshold` 0,70;
  kesmede `cut_continuity_threshold` 0,60). Bir kare yeniden çizilince ona bağlı kareler ve klipler "eski" olur; denetçide
  **Sonraki N kareyi yeniden çiz** çıkar ve etkisi (kare, klip, tahmini maliyet) önce gösterilir.
  - **Geçiş otomatik sınıflanır:** aynı mekân + aynı kadro + aynı kamera ölçeği → *devam* (continuous); kadro ya da kadraj
    değişirse → *kesme* (cut). Senaryoda `CUT TO:` / `KESME:` kesmeyi, `CONTINUOUS:` / `DEVAM:` devamı zorlar; yönetmen
    denetçiden çekim bazında da seçer. Gerekçe denetçide yazar ("kadro aynı: Bolt+Mila", "kadraj değişti: geniş → orta").
  - **Zincirden gelen karakterler:** önceki çekimin karesinde olan, bu çekimde adı geçmeyen ve senaryoda çıktığı yazılmayan
    karakter (bu çekimin yazılı kadrosu öncekinin alt kümesiyse) kadroya otomatik eklenir: referansı, değişmezleri ve kimlik
    kapısıyla; panelde "zincirden gelen" etiketiyle. Kadro başka birine geçiyorsa ya da senaryo karakterin çıktığını
    söylüyorsa (`Mila leaves the attic.`) edit istemi karakterin kareden çıkarılmasını açıkça ister.
  - **Kesme karesi kadrajı açıkça ister** ("Reframe the camera for this shot: a medium shot of Bolt, 35 mm lens"); kapı
    karakter alanının istenen ölçeğe kabaca uyduğunu da denetler, tutmazsa yeniden çizer (son denemede "inceleme önerilir").
  - **Kesmede kamera nasıl değişir (yöntem):** Qwen-Image-Edit image 1'in kompozisyonunu korur, kamerayı metinle taşımaz
    (Kıvılcım SH03: üç denemede de SH02'nin kamerası). Yöntem çekim başına seçilir (Kurgu › denetçi › Kesme; boşsa otomatik):
    **kırp + iyileştir** (ölçek daralıyor, açı ≤ 15°): önceki çekimin son karesinde odak karakterin kutusu (DINOv2 yama
    haritası; insanda yüz kutusu) bulunur, karakter orada yoksa önce aynı kamerayla yerleştirildiği bir ara kare çizilir;
    16:9 kırpılır (en az 1,5 kat; yakın planda karakter alanı ≥ %25 hedeflenir, gerekirse gövde alttan kesilir), 1280x704'e
    büyütülür ve Qwen-Edit Lightning "iyileştirme" geçişiyle netleştirilir; jump-cut kapısı ≥ 1,5 kat yakınlaşmayı geçerli
    sayar. **Açı LoRA'sı** (açı ≥ 30°): Qwen-Image-Edit-2511 Multiple Angles LoRA (`<sks> front-left quarter view low-angle
    shot close-up`; en yakın 45° adım); Ayarlar'da `QWEN_EDIT_ANGLES=on` ve dosya volume'da `models/loras/` altında olmalı;
    kapalıysa ya da iki deneme kapıdan kalırsa kırp + iyileştir'e düşer. **Geniş plandan çizim** (ölçek genişliyor). Tahmin
    uç noktası iki yolu ayrı kalem olarak gösterir; iş girdisi (LoRA adı ve gücü, kırpma kutusu) harcama geçmişine yazılır.
  - **Kadraja duyarlı kapı:** yakın planda (yakın, çok yakın) stil, ışık dili metni yerine çekimin onaylı karesine göre
    ölçülür (OpenCLIP görüntü benzerliği × LAB palet yakınlığı; kırp + iyileştir karesinde kırpıma, son karede ilk kareye);
    Kıvılcım SH03 klipleri metinle 0,06–0,33, kareyle 0,94–1,0. Karede küçük kalan insan olmayan karakterde (yama alanı
    < 0,10; geniş planda Bolt 0,04–0,08) DINOv2 benzerliği "ölçülemedi" sayılır, OpenCLIP benzerliği yine geçmeli.
    Kırp + iyileştir karesinde eşiğe ±0,02 yakın benzerlik "inceleme önerilir" olur. Kırp + iyileştir çekiminin son karesi
    ve video istemi kamerayı döndürmez (yan/dikey açı yazılmaz, son kare aynı dar kadrajda istenir).
  - **Renk sürekliliği, kamera değişince:** kesmede kamera değiştiyse (cut-in, açı LoRA'sı, kesme kamerası) sabit şeritler
    farklı içerik görür (Kıvılcım SH02 → SH03 yakın planda mavi pencere şeritleri kaplıyor: ΔE 5,6, gözle atlama yok).
    O geçişlerde şerit ve merkez dışı bütün alan ölçülerinin küçüğü alınır, eşik 1,5 kat gevşer (ΔL > 4,5 ya da ΔE > 6);
    Renk sayfası hangi ölçünün kullanıldığını yazar. "Çekimleri otomatik eşleştir" her çalışmada olay yazar: değişen
    çekimler, referans çekim ve atlananların sebebi (elle ayarlı, referansa zaten yakın, sınırlar içinde iyileşmedi…).
  - **Kırp + iyileştir son karesi:** karakter alanı başlangıç karesinin en az %80'i olmalı (Kıvılcım SH03: 0,297 → 0,226
    geriye açılmıştı); üç denemede tutmazsa son kare kullanılmaz, video son karesiz üretilir.
  - **Videoda mükerrer karakter:** video kapısı her 0,5 saniyede bir kare alıp yüzleri sayar (kimliği ölçülemeyecek kadar
    küçük yüzler de); çekimdeki insan sayısından fazla yüz en az iki örnek karede görülürse klip elenir, tek karede
    görülürse "inceleme önerilir". Nora SH01: ikinci Nora'nın belirdiği klip 0,75 ve 1,25 sn'de 2 yüz verdi; diğer 23 klip
    hiçbir karede 1'den fazla değil.
  - **Kırp + iyileştir sınırı:** kırpım 2,5 kattan fazla büyütülecekse (geniş planda boy boy karakterden yakın plan; Nora
    SH02 3,3×) ya da iyileştirme kırpımın kompozisyonunu korumazsa, o ve sonraki denemeler açı LoRA'sına (kapalıysa metinle
    kamera tarifine) düşer. Açı karesi yolunda da son kare başlangıç kadrajında kalmalı (alan ≥ %80, aynı plan ölçeği).
  - **Sıfırdan kurulum (compose):** yönetmen notunda poz/eylem fiili (kneel, sit, hold, cup, lie, jump, hop, stand on,
    diz çök, otur…) ya da çekimde gerçek boyutu yazılı karakter varsa kesme karesi boş tuvalde sıfırdan kurulur:
    image 1-2 karakterlerin durağan görselleri (yalnızca tasarım), image 3 mekânın görseli (yalnızca ışık ve ortam);
    kamera, poz, eylem ve boyut ilişkisi istemden. Pip SH03: mekân image 1 iken Nora referanstaki gibi ayakta, Pip
    baldır boyunda çizilmişti. Çekim tarifindeki poz fiilleri yalnızca öneridir (denetçide tek tıkla seçilir): kırp +
    iyileştir ve açı LoRA'sı önceki karenin pozunu taşıyarak başarılı oluyor.
  - **Gerçek boyut:** varlık sayfasında karakter ya da nesneye "Gerçek boyut" yazılır (ör. "tiny, palm-sized; much smaller
    than a hen"): istemlere girer ("draw Pip at this size, not at the size in its reference image") ve ölçek kapısı
    karakterin boyunu karedeki insanın yüz boyuyla karşılaştırır ("tiny/palm/minik/avuç" ≤ 1,5 yüz boyu, "small/küçük"
    ≤ 3; Pip SH03'teki dev civciv 2,8, SH01'deki doğru boy 1,2); tutmazsa yeniden çizilir, üç denemede kare reddedilir.
  - **Yüklenen ilk kare:** panelde görünür; son karesi yüklenen kareden aynı plan ölçeğinde çizilir (alan ≥ %80).
  - **Kare değişince:** her klip ilk ve son karenin içerik kimliğiyle (koşul) kaydedilir; kare yeniden çizilince eski
    koşulun klipleri yeniden kullanılmaz ve düzeltme sayacı sıfırlanır.
  - **Yönetmen seçimi:** kapıdan kalmış ama bugünkü kareden üretilmiş aday denetçide "Yine de kullan" ile 1. sıraya
    alınır (karantinadan geri alınır; kapının ölçümleri kayıtta kalır); test turu klibi için "Aynı seed ile 720p üret"
    tam tur çözünürlüğünde yeni klip üretir ve onu 1. sıraya koyar.
  - **Video nerede biter:** sonraki çekim *devam* ise FL2VA iki kare arasında üretilir (ilk kare Kn, `last_frame` K(n+1)).
    Değilse (kesme, yeni sahne, sekans sonu) video sonraki kareye "yolculuk" yapmaz: **çekim sonu karesi** Kn_end, Kn'den
    Qwen-Image-Edit Lightning ile çekimin kendi eylemi tamamlanmış hâli olarak çizilir (~0,007 $; kimlik, değişmez ve süreklilik
    kapılarından geçer; geçemezse video son karesiz üretilir) ve `last_frame` odur. Proje ayarı `chain_end_frame` ile kapatılır.
  - **Varış skoru:** klibin son karesi hedefe (K(n+1) ya da Kn_end) ne kadar vardı: DINOv2 tam kare benzerliği × 0,7 + son
    karedeki kimlik × 0,3. Kaydedilir ve rafine havuz sıralamasına girer (birleşik skor × 0,7 + varış × 0,3);
    `arrival_review_threshold` (0,85, gerçek veriyle kalibre edilecek) altı "inceleme önerilir". Devam eden çekimde DINOv2
    `transition_threshold` (0,80) altıysa aday elenir.
  - **Dikiş modu** (`seam_mode`): `keyframe` (varsayılan; çekimler paralel, dikiş anahtar karelerle) ya da `exact` (devam
    eden çekimin ilk karesi önceki çekimin kazanan klibinin gerçek son karesi; sıralı üretim, önceki çekim bitene kadar
    beklenir). `seam_crossfade_frames` (0-6) devam dikişlerinde filmde kısa geçiş yapar. Süreklilik denetimi devam
    dikişlerinde içerik benzerliğini de ölçer ("içerik sıçraması").
  - **Storyboard önce:** ücretli modda zincirli projede kare onayı varsayılan olarak zorunludur. Storyboard sayfasında
    zincir şeridi bütün ilk kareleri ve son kareleri geçişleriyle gösterir; **Şeridi onayla ve videoları başlat** tek tıkla
    onaylar (inceleme bekleyen kare onaylanmaz). Sorunlu kare yeniden çizilince sonraki kareler de (etki onayından sonra)
    yeniden çizilir.
  - **Tasarım kayması:** her edit isteminde "<karakter> must look exactly like image N" + değişmezler; değişmezlerden
    otomatik negatifler ("exactly one bent antenna" → second antenna, extra antenna, extra bulb). Senaryodaki durum
    sözcükleri (dark, off, broken, closed…) olumlu isteme "State: eyes dark." olarak, zıtları negatife yazılır.
  - **Tek örnek:** önceki karede zaten olan karakter "eklenmez", korunur ("keep the single Mila already in image 1
    unchanged"; durağan görseli yalnızca kimlik referansıdır) ve her istem "Each character appears exactly once" der.
    Kalite kapısı her karakterin karede tek örneğini arar: aynı kimliğe eşleşen birden fazla yüz (insan) ya da birbiriyle
    örtüşmeyen birden fazla benzer kırpım (robot) → "<karakter> karede birden fazla", kare elenir.
  - **Durum önce gelir:** senaryodaki ve yönetmen notundaki durum ("Bolt is still switched off, eyes dark and unlit")
    istemin en sonuna, referans ve değişmezlerden sonra yazılır: "Exception for this shot, overriding the reference images
    and the invariants: …"; negatife zıtları ("glowing eyes, lit eyes", değişmezdeki renkle "blue glow"). Değişmezi
    durumdan bağımsız yazın: "round glass eyes (glow blue only when switched on)". Son kare çekimin kendi durumunu korur.
  - **Kesmede kamera (30° kuralı):** kesmede ardışık iki çekim arasında en az bir plan ölçeği adımı (geniş → orta → yakın)
    ya da ≥ 30° yan açı farkı olur. Senaryo kamerası buna uymuyorsa (Kıvılcım: hepsi "orta, 35 mm") çekime kadroya göre bir
    kesme kamerası atanır (`camera_plan`; ör. Bolt'a yakın ve soldan alçak açı, iki kişilik orta ve ters açı); denetçide
    "otomatik kesme kamerası" etiketi görünür, kamerayı elle seçerseniz atanmaz. Kesme karesi önceki kareden değil mekânın
    geniş görüntüsünden (mekân durağan görseli, yoksa sahnenin ilk geniş planı) çizilir ve istem kamerayı açıkça taşır
    ("Move the camera closer and 35° to the left, slightly low angle: a close-up of Bolt"). **Jump cut kapısı:** kesmenin
    karesi önceki çekimin son karesiyle aynı kamerayla çizilmişse (karakter dışı bölgede ORB + homografi: iç oran ≥ 0,5,
    ölçek ~1, kayma < %5; Kıvılcım'daki üç kesme 0,82-0,90 / 0,97-1,00 / ≤ %1,1 ile yakalanır) kare yeniden çizilir, son
    denemede "jump cut riski" notu düşer. Kesme karesinin mekân benzerliği eşiği 0,35'tir (yeni bakış açısı).
  - **Kesme karesinin kaynağı:** kamera yaklaşıyor ya da aynı ölçekte dönüyorsa image 1 önceki çekimin son karesidir ve
    kamera ona göre tarif edilir (Kıvılcım: SH02 ve SH03 aynı geniş plandan çizilince aynı yeni açıya gitmişti); kamera
    uzaklaşıyorsa mekânın geniş görüntüsünden çizilir ve istem önceki çekimin bakış açısını tekrarlamamasını söyler.
    Kesme karesi her zaman "yeni bakış açısı: mekânda olmayan büyük nesne var mı" inceleme notunu alır. Yakın plan kadraj
    kapısı karakter alanını DINOv2 yama haritasıyla ölçer (SH03'teki küçük Bolt 0,09: yakın plan değil, yeniden çizilir).
  - **Durum kapısı, göz bölgesi:** parlama yalnızca yanan göz olabilecek toplu ve yeterince büyük lekelerden ölçülür;
    cam gözdeki pencere yansıması, yağmur çizgisi ve parıltı sayılmaz. Renk aktarımı kapılardan önce yapılır, kapılar
    saklanan kareyi ölçer. Kritik durumda **Kareyi elle onayla** şerit akışında zinciri sürdürür (sonraki halkalar çizilir),
    video şerit onayıyla başlar.
  - **Zincir durduğunda:** incelemedeki ya da güncel olmayan bir halkadan sonra hiçbir halka (devam çekimi dahil) çizilmez;
    önceki halka değişince sonraki halkalar onay beklemeye döner ve eski notları silinir.
  - **Karakter girişi:** aynı mekânda önceki çekimde olmayan karakter beliriyorsa denetçide seçilir: kesme (varsayılan,
    kamera belirgin değişir) ya da devam + giriş eylemi (ilk karede görünmez, videoda sağdan girer).
  - **Kamera hareketi:** pan, dolly, tilt gibi hareketler istemde ayrı bir cümleyle açıkça yazılır ("Camera movement: the
    camera slowly pans …"); kapı klipteki arka plan kaymasını ölçüp kaydeder, görülmezse "inceleme önerilir".
  - **Renk sürekliliği:** süreklilik denetimi karakter dışı bölgenin LAB ölçüsüyle karar verir (ΔL > 3 ya da ΔE > 4;
    sıcaklık da aynı bölgeden). Kıvılcım: renklendirilmemiş SH01 → SH02 ΔL −5,9 (atlama), diğer ardışık çekimler ΔL ≤ 2,3.
    Film birleşip denetim bitince **otomatik çekim eşleştirme** çalışır: sekansta karakter dışı ölçüsü medyana en yakın çekim
    referans olur, elle ayarlanmamış diğer çekimlerin pozlama, kontrast, doygunluk ve sıcaklığı sınırlar içinde çözülür ve
    GRADE + MUX çalışır (Renk sayfasında "otomatik eşleştirildi" ve **Geri al**; geri alınan çekim bir daha eşleştirilmez).
    Kaynakta: her kare çizilince karakter dışı bölgesinin LAB ortalaması ve sapması zincirdeki önceki kareye taşınır
    (sınırlı Reinhard aktarımı); H3 ilk karenin rengini taşıdığından fark baştan küçülür.
  - **Durum referansı ve durum kapısı:** Qwen-Image-Edit referans görselini metinden çok izler (gözleri yanık Bolt stili
    "eyes dark" istemini yendi). Çekim bir durumdaysa (kapalı / açık) karakterin kimlik referansı o durumun görseli olur:
    Varlıklar sayfasında **Durum: kapalı** (`state_off`) ya da **Durum: açık** (`state_on`) yüklenir (sürüm artmaz, kilitli
    varlığa da eklenir); yüklenmemişse sekansta aynı durumda olan ve durum kapısından geçen önceki karenin karakter kırpımı
    otomatik referans olur (`state_refs/…`). Durum kapısı karakter kırpımında ölçer: ışık rengi değişmezde yazıyorsa
    ("round glass eyes (glow blue only when switched on)") parlama çekirdeğinin payıyla (Kıvılcım: iki göz karanlık 0,13‰,
    tek göz yanık 0,6-0,8‰, iki göz yanık 4,5-4,8‰; eşik 0,3‰), yoksa OpenCLIP metin sondalarıyla. Tutmazsa kare yeniden
    çizilir; son denemede "inceleme önerilir: durum (eyes dark) tutmadı". OpenCLIP sondası her durumda kaydedilir; Kıvılcım
    karelerinde yanık ve karanlık gözleri güvenilir ayıramadığı için karar parlama ölçüsündedir.
  - **Durum değişen kare:** "off" çekimde karakterin görsel yuvasında yalnızca durum görseli vardır (normal durağan
    görsel hiçbir yuvada olmaz; iş girdisi yuva → anahtar olarak servis geçmişine ve günlüğe yazılır). image 1'de zaten olan
    karakter "unchanged" denmeden aynı yer ve pozda tutulur, tasarımı referansa göre düzeltilir ve durumu açıkça değiştirilir
    ("replace Bolt's eyes with dark unlit glass as in image 3"). "off" durumda tarif ve değişmezlerde ışık sözcüğü geçmez
    ("round glass eyes (dark and unlit)"); negatife "one glowing eye, mismatched eyes, glowing eye" eklenir. Durumu değiştiren
    olay ("eyes flicker and light up") ilk kareyi olaydan önceki durumda (gözler karanlık), son kareyi sonraki durumda çizer.
    Zincirde kritik durum (sonraki olay buna dayanıyor) son denemede de tutmazsa kare "Kare inceleme gerekiyor"da kalır ve
    video başlamaz; kritik değilse "inceleme önerilir" ile geçer.
  - **Sayılan parçalar:** "exactly one bent antenna with a small bulb" gibi değişmezler negatife "two bulbs, double bulb"
    olarak girer; nesne parçaları otomatik sayılamadığı için kare "… sayılamıyor, gözle kontrol edin" notunu alır.
  - **Eski kuralla üretilmiş klip:** kazanan klibin son karesi bugünkü dikiş kuralının beklediğiyle aynı değilse (geçiş
    sınıfı değişti, son kare eklendi ya da değişti) klip "eski kareden" işaretlenir ve plan (şerit onayı dahil) görüntüyü
    yeniden üretir.
  - **Değişiklik iste:** not, çekimin istemlerine "Director note: …" olarak girer (görsel özete girmez: notlu kare
    yeniden çizdirmeden de onaylanabilir). **Kareleri hazırla**
    önce etkisini gösterir (yeniden çizilecek kareler, eskiyecek klipler, tahmini maliyet), sonra değişiklik istenen kareyi
    yeni seed'lerle ve zincirde ondan türeyen kareleri sırayla yeniden çizer; eksik son kareleri de çizer. Denetçide
    **Bu kareyi ve sonrakileri yeniden çiz** aynı işi tek kare için yapar.
  - **Görsel önbelleği:** yeniden çizilen kare aynı dosyaya yazılır; panel adresi kare sürümünü taşır (`&v=`) ve
    `storyboard/` dosyaları ETag ile her seferinde doğrulanır.
  - **Şerit incelemesinde boşta bekleme:** kare işinden sonra 15 dk boyunca uç noktanın boşta beklemesi
    `RUNPOD_REVIEW_IDLE_S` (300 sn) olur, sonra `RUNPOD_IDLE_TIMEOUT_S`'e döner; art arda yeniden çizimler soğuk başlatma
    (~195 sn) ödemez. Boşta süre geçerli değerle faturalanır.
  - **Süre:** H3 kare sayısını 17n+5'e yukarı yuvarlar (4 sn → 4,46 sn); montajda hedef süreye kesilir. Devam eden çekim
    baştan kesilir, varış ucu korunur.
- **Karakter referanslarıyla (Ref2VA, elle seçilir):** çekim bazında denetçiden seçilir; kare çizilmez. Çekimdeki karakterlerin
  referans seti (durağan görsel, ifade sayfası ve dönüş videosundan önden, yandan, arkadan üç kare; karakterler arasında
  paylaşılan en çok 9 görsel) ve varsa ilk kare gider; istemde `<Picture 1>, <Picture 2> show Kerem` gibi eşlenir. Ref2VA
  ayrı bir model bölümüdür: SGLang'de ikinci bir sunucu (`--model-variant ref2va`), ComfyUI'de `r2v.json` iş akışı.

**Negatifler:** model CFG damıtılmıştır, negatif istem almaz. Stil kılavuzunun ve çekimin negatifleri istemin sonuna
**pozitif kısıt** olarak eklenir ("bulanık" yerine "sharp focus with crisp, clean contours", "kalabalık" yerine "only the
listed characters are present"); istenmeyen şeyin adı isteme yazılmaz. Tanınmayan terimler servis geçmişinde "kısıta
çevrilemeyen negatifler" olarak görünür: kılavuzda pozitif bir ifadeyle yeniden yazın (`backend/app/style/constraints.py`).

### Sunucu: ComfyUI (varsayılan) ya da SGLang (deneysel)

Ayarlar › MiniMax H3 › **Arka uç**:

- **ComfyUI** (varsayılan; `/upload/image`, `/prompt`, `/history`, `/view`). Kendi ölçtüğünüz kurulum (int8_convrot, Turbo
  8 adım) bu arka uçla kullanılır. İş akışlarını ComfyUI'de **Workflow › Export (API)** ile kaydedip değişen değerleri yer
  tutucuyla değiştirin ve Ayarlar › MiniMax H3 › **ComfyUI iş akışları**ndan yükleyin (`t2v`, `i2v`, `r2v`, `keyframe`;
  ayrıntı: `comfy-workflows/README.md`). Hazır `i2v`, `r2v` ve `keyframe` iş akışları ComfyUI 0.39.0'ın resmî şablonlarından üretildi (`comfy-workflows/`). GPU süresi
  ComfyUI geçmişindeki başlangıç ve bitiş zamanından okunur.
- **SGLang** (deneysel; resmî sunucu, `/v1/videos`). Tek RTX 5090'da benim tarafımdan ölçülmedi: SGLang tarifindeki
  ölçümler BF16 + aktarım (5090, 480p, 20 adım: 112 sn) ve INT8 (4090, 768p, 20 adım: ~300 sn) içindir. Denemek için tek
  5090'da planım: resmî BF16 bileşenler, çalışırken `--quantization convrot_int8`, turbo LoRA (9 adım) ve katman katman
  aktarım:
  ```bash
  PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  sglang serve --model-path MiniMaxAI/MiniMax-H3 --model-variant fl2va --host 127.0.0.1 --port 18010 \
    --quantization convrot_int8 --performance-mode memory \
    --layerwise-offload-components dit,text_encoder,vae --layerwise-resident-layers video_vae=36 \
    --lora-path larryvrh/MiniMax-H3-Turbo-Lora --lora-weight-name minimax_h3_turbo_v4_step600_ema.safetensors \
    --lora-merge-mode auto --warmup-resolutions 1280x704 --warmup-num-frames 124
  ```

**Erişim (açık port yok):** ComfyUI makinede `127.0.0.1`'de dinler. Ya SSH tüneli
(`ssh -N -L 8188:127.0.0.1:8188 -p <SSH portu> root@<makine IP>`; adres `http://host.docker.internal:8188`) ya da vast.ai
Instance Portal'ın kimlik doğrulamalı vekili (`PORTAL_CONFIG` girdisi `localhost:8188:18188:/:ComfyUI`; adres
`https://<IP>:<8188'in dış portu>`, **Portal belirteci** `WEB_PASSWORD` ya da `OPEN_BUTTON_TOKEN`). Portal kendinden imzalı
sertifika kullanır: o adreste **Sunucu sertifikası** `pin` olmalıdır (bkz. *Sertifika sabitleme*).

**Makine gereksinimi:**

| | ComfyUI (int8, `setup.sh`) | SGLang (deneysel) |
|---|---|---|
| GPU | RTX 5090 32 GB | RTX 5090 32 GB (ya da 24 GB+ ve aktarım) |
| Sistem belleği | **128 GB** (en az 96): ilk gerçek denemede yalnızca H3 kapsayıcıda ~94 GB kullandı (sayfa önbelleği dahil); Qwen-Image ~30 GB ekler | en az 128 GB |
| Disk | **200 GB** (en az 150): H3 budanmış int8 FL2VA + Ref2VA DiT (2 × 21 GB), NVFP4 metin kodlayıcı (15,7 GB), VAE'ler (3,4 GB), turbo LoRA'lar (4 GB), Qwen-Image-2512 fp8 + metin kodlayıcı + VAE (~30 GB), ComfyUI ve PyTorch | en az 250 GB (resmî BF16 bileşenleri) |
| İndirme | **1 Gbps**: modeller ~105 GB (1 Gbps'te ~15 dk; yavaş bağlantıda saatler) | aynı |

Bu üç koşul Ayarlar › MiniMax H3'ten değiştirilir (`H3_MIN_RAM_GB`, `H3_MIN_DISK_GB`, `H3_MIN_INET_MBPS`; RAM 96'nın, disk
150'nin altına inmez). Dosya boyutları Hugging Face'teki dosya listelerinden alındı (Ekim 2026); RAM ölçümü sizin ilk
denemenizden.

### Yedek makine ve `setup.sh`

vast.ai'de durdurulan makinenin GPU'su başkasına kiralanabilir; o zaman makine yeniden açılmaz. **Otomatik başlatma** açıksa
ve makine 5 dakikada açılmazsa (ya da vast.ai başlatmayı reddederse) Hell Grind yedek makine arar:

1. **Teklif:** aynı GPU tipinde (**Yeni makinenin GPU'su** boşsa eski makineninki), **fiyat tavanı** (varsayılan 0,95 $/sa)
   altında, en az **RAM** (128 GB), **disk** (200 GB) ve **indirme hızı** (1000 Mbps; modeller ~105 GB), güvenilirliği
   %98+, doğrulanmış ve AB/BK/G. Kore/ABD dışındaki tekliflerden en ucuzu. Bölge süzgeci vast.ai'de ve burada iki kez
   uygulanır; konumu bilinmeyen teklif hiç seçilmez.
2. **Onay:** **Yedek makine** ayarı `ask` (varsayılan) ise öneri Ayarlar › MiniMax H3 › **Makine**'de **Onayla ve kirala**
   ile onay bekler; `auto` ise tavan altındaki teklif sormadan kiralanır; `off` yedek makineyi kapatır. Çekim bu arada
   Türkçe bir açıklamayla durur.
3. **Kiralama ve kurulum:** teklif `vastai/base-image` ile kiralanır; `backend/vast/setup.sh` isteğe gömülür (gzip +
   base64, vast.ai'nin 16 KB onstart sınırı içinde; ya da **setup.sh adresi**nden indirilir). Betik ComfyUI'yi (güncel),
   PyTorch'u (CUDA 12.8), MiniMax H3 modellerini (Comfy-Org: `minimax_h3_fl2va_pruned_int8_convrot`,
   `minimax_h3_ref2va_pruned_int8_convrot`, `qwen3vl_32b_minimax_h3_nvfp4_awq`, `minimax_h3_video_vae_fp16`,
   `minimax_h3_audio_vae_fp32`, FL2V turbo 8 adım ve Ref2V turbo 4 adım LoRA'ları) ve görsel modelini kurar, ComfyUI'yi
   `127.0.0.1:18188`'de bir supervisor servisi olarak başlatır. Portal `8188`'den kimlik doğrulayarak açar; parola mevcut
   portal belirteciniz (yoksa üretilip Ayarlar'a yazılır).
4. **Ayarlar:** yeni makine kimliği, adresi (`https://<IP>:<dış port>`), portal belirteci ve sertifika ayarı (`pin`) `.env`'e yazılır;
   bütün servisler yeniden başlatılmadan yeni makineyi kullanır. Kurulum sürerken boşta durdurma devre dışıdır (indirme
   süresi boşta bekleme olarak bütçeye yazılır).
5. **Eski makine:** yeni makine yanıt verince eski makine **yalnızca ürettiği her çıktı yerel depoda doğrulanmışsa** (bkz.
   *Çıktı güvencesi*) ve çalışmıyorsa **silinir**: diski ve üzerindeki her şey gider, disk ücreti durur. Eksik çıktı varsa
   makine silinmez ve Ayarlar'da işaretlenir. Kurulum başarısız olursa yeni makine durdurulur, eskisi silinmez.

**İlk makine (Yeni makine kirala):** Ayarlar › MiniMax H3 › **Makine** › **Yeni makine kirala** aynı akışı ilk makine için
çalıştırır: teklif aranır (yukarıdaki koşullar), öneri onayınıza sunulur, onaylarsanız kiralanır ve `setup.sh` ile kurulur;
kimlik, adres, belirteç ve `pin` Ayarlar'a yazılır. Hiçbir makine silinmez (Ayarlar'da bir makine varsa o da kalır). Ücretli
mod gerekir; ücretli moda geçmek için artık yalnızca vast.ai API anahtarı yeterlidir (makine yokken üretim başlamaz).

**Kurulumu izleme:** kiralama aşamaları ve süreleri (sırada → kiralanıyor → açılıyor → kuruluyor → hazır) Makine bölümünde
görünür. `setup.sh` günlüğü (`/var/log/hellgrind/setup.log`) portalın kimlik doğrulamalı **8189** portundan okunur
(`PORTAL_CONFIG` girdisi `localhost:8189:18189:/:Hellgrind kurulum`, makinede `python3 -m http.server` yalnızca
`127.0.0.1:18189`'da) ve panelde **Kurulum günlüğü** altında 5 sn'de bir tazelenir: aşama süreleri, her indirmenin boyutu,
süresi ve hızı, sonda **toplam kurulum süresi** ve portal sertifikasının SHA-256 parmak izi. Makinede doğrudan:
`tail -f /var/log/hellgrind/setup.log` (vast.ai konsolundaki terminal ya da SSH).

`setup.sh`'yi elle de kullanabilirsiniz: kendi makinenize kopyalayıp `bash setup.sh` (ortam: `HG_IMAGE_MODEL`,
`HG_H3_TEXT_ENCODER`, `HG_COMFY_PORT`, `HG_LOG_PORT`, `HF_TOKEN`). İlk kez kendi gözetiminizde deneyin: bu akış sahte bir
vast.ai'yle sınandı, gerçek bir kiralamada çalıştırılmadı.

### Çıktı güvencesi

Makinede üretilen hiçbir çıktı yalnızca makinede kalmaz:

- Her H3 işi (video ve storyboard karesi) gönderilir gönderilmez kayda geçer (`h3_outputs`: makine, sunucu, iş kimliği).
- İş bitince sunucunun yanıtı (çıktı dosyaları, GPU süresi) kayda yazılır ve çıktı **hemen** indirilir. İndirilen bayt sayısı
  sunucunun bildirdiği `Content-Length` ile karşılaştırılır (yarım indirme yeniden denenir).
- Ham çıktı ve iş meta verisi (sunucu yanıtı, kaynak bilgisi; JSON) yerel nesne deposuna içerik adresli yazılır
  (`h3-outputs/ab/<sha256>.mp4|.png|.json`); depodaki kopyaların boyutu ve SHA-256'sı yeniden okunarak doğrulanır. Ancak o
  zaman kayıt `stored` olur. Doğrulanamayan çıktı üretimi geçici hatayla durdurur; yeniden denemede aynı iş beklenir,
  yeniden üretilmez.
- Ham kopya, varyasyonun (meta verisi yazılmış, gerekirse kesilmiş) videosundan ayrıdır ve elenen varyasyonlarla silinmez:
  yerel depo kabaca iki kat büyür.
- Makine çalışırken boşta bekleme görevi, yarım kalmış indirmeleri (worker çöktü, indirme hatası) yeniden dener. ComfyUI
  yeniden başlayıp geçmişini kaybettiyse çıktı, işe özgü dosya önekinden (`{{filename_prefix}}` = `hellgrind/<tür>_<iş özeti>`)
  bulunur; bu yüzden iş akışlarınızın kaydetme düğümünde `{{filename_prefix}}` kullanın.
- Ayarlar › MiniMax H3 › Makine › **Çıktı güvencesi**: makine başına yerel depodaki, yalnızca makinedeki ve üretilemeyen
  çıktılar. Eksik çıktısı olan eski makine için **Eksikleri indir, sonra sil** (makine çalışmıyorsa başlatılır, saatlik ücret
  işler; hepsi doğrulanınca silinir) ya da **Kayıp say ve sil** (açık onayla).

### Sertifika sabitleme

**Sunucu sertifikası** ayarı (`H3_TLS_MODE`): `verify` (olağan doğrulama; gerçek sertifikalı adres), `pin` (panelden kiralanan
makinenin varsayılanı), `off` (doğrulama yok). `pin` kipinde ilk başarılı bağlantıda (kimlik doğrulamalı sağlık yanıtı 200)
portal sertifikasının SHA-256 parmak izi adres başına kaydedilir; sonraki her bağlantıda el sıkışmadan hemen sonra, istek ve
belirteç gönderilmeden önce karşılaştırılır. Farklı sertifika üretimi durdurur ve panelde uyarı çıkar: makineyi bilerek
yenilediyseniz `setup.sh` günlüğündeki parmak iziyle karşılaştırıp **Sabitlemeyi sıfırla**'ya basın. Sertifika el sıkışmadan
okunamazsa ya da kaydedilemezse bağlantı doğrulamasız sürer ve panelde uyarı görünür. vast.ai'nin makine yeniden
başlatılınca sertifikayı yenileyip yenilemediği gerçek bir makinede doğrulanmadı; yenilerse ilk bağlantı uyuşmazlıkla durur.

### Toplu üretim: önce kareler, sonra videolar

Birden çok sahne birlikte işlenirken (**Hepsini üret**, senaryo işleme, taslak geçiş, stil ve renk değişikliği) önce bütün
storyboard kareleri (H3 makinesinde görsel modeliyle) kuyruğa girer; videolar (H3) ancak son kare bitince kuyruğa alınır. GPU'da
model her çekimde değil bir kez değişir. Karesi başarısız olan sahnenin videosu düşer; dondurma (Kill-Switch) bekleyen videoları
da iptal eder. Bu arada aynı sahne ikinci kez kuyruğa verilmez ("toplu üretimde" yanıtı). Tek sahnelik üretim eskisi gibidir.

### vast.ai: bilinen sorunlar (ilk gerçek deneme, Ekim 2026)

vast.ai arka ucu **deneyseldir ve varsayılan olarak kapalıdır** (Ayarlar › MiniMax H3 › Platform: `runpod`). İlk gerçek
denemede görülen ve henüz düzeltilmeyen sorunlar; arka uç yeniden açılırsa önce bunlar ele alınmalı:

- **Pazaryeri modeli otonom hatta uymuyor:** durdurulan makinenin GPU'su başkasına kiralandı, makine yeniden başlatılamadı;
  her yeni makine ~35 dk kurulum (model indirme) istiyor.
- **Makine kendiliğinden "exited" oldu:** kurulumdan ~10 dk sonra. Panelde durdurma olayı ya da boşta bekleme maliyet kaydı,
  vast.ai denetim günlüğünde de durdurma kaydı yok. Boşta durdurma mı (olay yalnızca son etkin proje varken yazılıyor), yoksa
  kapsayıcı mı kendi kapandı, belirsiz.
- **Sertifika sabitleme `TypeError` ile başarısız oldu** (portal bağlantısında; ayrıntı ve düzeltme bekliyor).
- **Portal Host/Origin 403:** Instance Portal (Caddy) vekili ComfyUI arayüzünü Host/Origin denetimi yüzünden bozuyor;
  Cloudflare Quick Tunnel da güvenilir çalışmadı.
- **Kurulum günlüğü:** kapsayıcının RAM sınırı cgroup'tan okunamıyor (vast.ai kapsayıcıya sınır koymayabilir); günlükte
  sunucunun toplam RAM'i yazar, kiralanan pay teklifte görünür. Ortak panel kusurları (otomatik kaydırma, arka plandaki sekmede
  geride kalan sayaç, bitmiş 4/4 aşamasının "şu an" görünmesi, boş "boş disk" değeri) düzeltildi.

### Storyboard görsel modeli ve lisanslar

FL2VA'nın ilk karesi bir görsel modelinden gelir. Hell Grind kendi makinenizde yalnızca Apache 2.0 lisanslı modeller kurar
(**Storyboard görsel modeli** ayarı; lisanslar Hugging Face model kartlarından doğrulandı, Ekim 2026):

| Model | Lisans | ComfyUI dosyaları | Not |
|---|---|---|---|
| **Qwen-Image-2512** (önerilen, varsayılan) | Apache 2.0 | `qwen_image_2512_fp8_e4m3fn` (20,4 GB), `qwen_2.5_vl_7b_fp8_scaled` (9,4 GB), `qwen_image_vae` | İstem bağlılığı ve kompozisyonu güçlü; iş akışı `comfy-workflows/keyframe.json` (resmî şablon, 50 adım, cfg 4) |
| **Qwen-Image-Edit-2511** (karakterli ilk kare) | Apache 2.0 | `qwen_image_edit_2511_fp8mixed` (20,5 GB), Lightning LoRA `Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16` (0,85 GB), metin kodlayıcı ve VAE Qwen-Image ile ortak | Karakterin referansından kare; iş akışı `comfy-workflows/edit_keyframe.json` (resmî şablon `image_qwen_image_edit_2511`, workflow-templates v0.11.78) |
| FLUX.1 [schnell] | Apache 2.0 | `flux1-schnell-fp8` (17,2 GB) | 4 adımda hızlı, kalite ve istem bağlılığı daha düşük; `keyframe.flux-schnell.json` |
| FLUX.1 [dev] | FLUX.1 [dev] Non-Commercial License v1.1.1 | kurulmaz | Model ticari amaçla çalıştırılamaz (BFL'den ayrı lisans gerekir); çıktıları ticari kullanılabilir ama rakip model eğitiminde kullanılamaz |

`comfy-workflows/keyframe.json` hazırdır (Qwen-Image-2512); FLUX.1 [schnell] için `examples/keyframe.flux-schnell.json`'u Ayarlar'dan `keyframe` olarak yükleyin.

**fal.ai üzerinden Flux:** fal'ın FLUX.1 [dev] LoRA sayfası çıktıların ticari kullanıma açık olduğunu belirtir ("All
generated images are cleared for commercial use"); Flux Kontext [pro] BFL'in ücretli API modelidir. fal yolunu kullanırken
model ve çıktı lisansı fal'ın kullanım koşullarına bağlıdır; ticari dağıtımdan önce fal ve BFL koşullarını kendiniz
doğrulayın. Bu bir hukuki görüş değildir.

### Makine yönetimi ve bütçe

Ayarlar › MiniMax H3'e **vast.ai API anahtarı** (ücretli kilidin anahtarı) ve **makine kimliği** girilir:

- **Boşta durdurma:** kuyruk **Boşta durdurma** dakikası (varsayılan 10) boş kalırsa ve sunucunun kendi kuyruğu da boşsa
  makine vast.ai API'siyle durdurulur. Ücretsiz moda dönerken çalışan makine de durdurulur.
- **Boşta bekleme bedeli:** makine çalışırken kuyruk boşsa geçen süre saatlik ücretle son etkin projenin bütçesine yazılır
  ("GPU makinesi boşta"); Kill-Switch ve hız bekçisi bunu görür, bütçe yetmezse makine hemen durdurulur.
- **Otomatik başlatma** (isteğe bağlı): iş geldiğinde durmuş makine başlatılır ve sunucu hazır olana dek beklenir.
- **Elle:** Ayarlar'da **Makineyi başlat / durdur** (yalnızca ücretli modda). Durdurulmuş makinenin diski vast.ai'de
  ücretlenmeye devam eder; makinenin GPU'su başkasına kiralanmışsa yeniden başlatma başarısız olabilir.
- **Bölge:** makinenin konumu izinli listede değilse üretim başlamaz (bkz. *Bölge: izinli liste*).
- **Bağlantıyı sına:** vast.ai'den makinenin durumu, saatlik ücreti, konumu ve GPU'su (ücretsiz okuma) ve sunucunun sağlık
  ucu. İş başlatmaz.

**Fiyatlar** (`pricing.yaml` › `self_hosted.h3`, sizin ölçümünüz): saatlik 0,95 $ (0,79-0,95 aralığının üstü, tutucu),
480p'de video saniyesi başına 8 GPU saniyesi (864x480: 40,8 sn, model yüklemesi dahil), 720p'de 16,5 (1280x704: 85 sn).
720p kademesi bu yüzden 1280x704 tuvalde üretilir. Rezervasyon bu tahminle yapılır; işlenen tutar sunucunun bildirdiği GPU
süresi × saatlik ücrettir.

### Lisans koruması

- Her H3 klibinin kaydına ve MP4 meta verisine (`comment`, `copyright`) kaynak bilgisi yazılır: model, **Model sürümü**
  (Ayarlar'da), arka uç, görev, adım sayısı, sunucu ve vast.ai'nin bildirdiği konumu, lisans ve "başka modelleri eğitmek
  için kullanılamaz" notu.
- Hattın model eğitimine giden tek yolu kürasyon kaydıdır (projeye özel kalite modeli için). `curation.jsonl` dışa
  aktarılırken gerçek H3 kliplerine bağlı kararlar ve H3 klibi üretilmiş sahnelerin kararları çıkarılır
  (`X-Hellgrind-H3-Records` başlığında sayısı; `?include_h3=true` denetim içindir ve onları `training_allowed: false`
  işaretiyle verir). Çekim paketi H3 çıktısı içeriyorsa `MINIMAX_H3.txt` bildirimi eklenir.
- Hatta LoRA ya da başka model eğitimi yapan kod yoktur. H3 çıktılarını (ör. dönüş videosu karelerini) Flux LoRA gibi başka
  bir modelin eğitiminde kullanmayın; H3'ün kendi LoRA'sı lisansa göre H3 türevidir ve serbesttir.

### Bilinmesi gerekenler

- **Hız:** brifing yoğunluğu (10 test + 50'ye kadar tam klip) tek makinede çekim başına saatler sürebilir; önizleme ya da
  deneme yoğunluğuyla başlayın. Bir çekimin görsel aşaması 6 saati aşarsa görev kuyruğa yeniden düşebilir (Celery
  `visibility_timeout`).
- **Süre 4-15 sn, 24 fps:** daha kısa çekim 4 sn üretilir ve çekim süresine kesilir.
- **H3'ün kendi sesi kullanılmaz:** montaj yalnızca görüntüyü alır.
- **fal.ai isteğe bağlıdır:** anahtarı yoksa storyboard karesi H3 makinesinde (keyframe.json, Qwen-Image-2512) ya da yerel
  çizilir, dudak senkronu atlanır; Seedance, mekân ve nesne referans görseli üretimi ve previs aktarımı fal.ai anahtarı ister.

## Ücretli mod kilitli

`DRY_RUN=1` varsayılandır. Bu modda anahtar tanımlı olsa bile hiçbir ücretli API çağrılmaz:

- Ücretli kod yalnızca `backend/app/live/` altında durur ve yalnızca fonksiyon içinde yüklenir.
- Her giriş noktası `guard(anahtar_adı)` çağırır; `DRY_RUN=1` ya da boş anahtar `PaidCallBlocked` fırlatır. Kilit, bütçe rezervasyonundan **önce** çalışır; engellenen çağrı deftere iz bırakmaz.
- Testler bunu otomatik denetler: ücretli paketler `app/live` dışında import edilemez, anahtar değerleri `guard` dışında okunamaz, ücretsiz modda hiçbir ücretli paket yüklenmez, her giriş noktası tuzaklı bir ortamda hiçbir şeye dokunmadan engellenir.
- Gizli anahtarlar yalnızca `.env` içinde durur; `.env` commit'lenmez.

### Ücretli entegrasyonlar (Faz 18): yazıldı, hiç çalıştırılmadı

| Modül | Ne yapar |
|---|---|
| `live/vast_h3.py` | MiniMax H3, vast.ai makinesinde; arka uç SGLang (`/v1/videos`) ya da ComfyUI (`/prompt`, `/history`, `/view`). t2va, fl2va (ilk kareden), ref2va (karakter referans setiyle); negatifler pozitif kısıt olur; her klibe kaynak bilgisi yazılır. Tutar ölçülen GPU süresinden. Gönderilmiş iş yeniden denemede yeniden üretilmez. |
| `live/vast_box.py`, `live/vast_api.py` | Makine yönetimi: iş kaydı, boşta bekleme bedeli, boşta durdurma, otomatik başlatma, bölge denetimi, yedek makine (teklif, onay, kiralama, `vast/setup.sh` ile kurulum, eski makineyi silme); vast.ai API'si. |
| `live/fal_video.py` | İsteğe bağlı. Flux LoRA ile ilk kare (`fal-ai/flux-lora`), sonra Seedance (`bytedance/seedance-2.0/fast/image-to-video`; eski çekimlerde Wan 2.2 `fal-ai/wan/v2.2-a14b/image-to-video`); dudak senkronu `fal-ai/sync-lipsync/v2/pro`; previs aktarımı `fal-ai/wan/v2.2-a14b/video-to-video`. 429, 5xx ve zaman aşımı yeniden denenir. |
| `live/elevenlabs.py` | `eleven_multilingual_v2`; duygu `stability` ve `style` değerlerine eşlenir. |
| `live/hermes_llm.py` | LangGraph akışı: `Send` ile paralel çıkarma, doğrulama, en çok 2 tur onarım. Çıktı şeması kural tabanlı ayrıştırıcıyla aynı; replikler eksiltilemez ya da uydurulamaz. |

Uç nokta kimlikleri ve parametreler fal.ai ve ElevenLabs belgelerinden doğrulandı (2–3 Ekim 2026), paket imzaları gerçek paketlerden denetlendi. Testler sahte `fal_client`, sahte `httpx` ve sahte LLM kullanır; genel internete bağlantı testlerde yasaktır. Hiçbir ücretli çağrı hiçbir zaman yapılmadı. Her ücretli çağrı bütçe rezervasyonundan geçer (LLM çağrıları belirteç tahminiyle, sesler karakter sayısıyla, video ve dudak senkronu fiyat tablosuyla).

Ücretli modda akış:

- **Video:** varsayılan olarak MiniMax H3 doğrudan video üretir (bkz. *Video motoru: MiniMax H3*).
- **Önce görsel, sonra video (Seedance çekimleri):** her varyasyonun ilk karesi (Flux) ayrı üretilir, depoya konur ve videoyla aynı eşikle puanlanır (siyah ya da tek renk kare ve eşik altı skor elenir). Elenen karenin videosu hiç istenmez; panelde "ilk karede elendi, video üretilmedi" yazar. Video yalnızca geçen kareden üretilir; varyasyonun bedeli kare + videodur. Ücretsiz modda yerel motor doğrudan video çizer.
- **Storyboard karesi** bestecinin prompt'uyla tek bir Flux karesidir; üzerine çekim bilgisi yazılır. fal.ai anahtarı yoksa yerel kare çizilir (ücretsiz).
- **Previs rehberi** Wan 2.2 video-to-video ile stile aktarılır (`strength` 0,65: hareket ve kompozisyon rehberden, görünüm stil kılavuzundan). Ücretsiz modda rehber renklendirilip kullanılır ve aynı uç noktanın fiyatı sanal olarak düşer.
- **Referans üretimi** (*Varlıklar* sayfasında *Referans üret*): karakter için MiniMax H3'ten yörünge hareketli 4 sn'lik dönüş videosu (durağan görseli varsa ondan başlar), mekân ve nesne için tek Flux karesi (fal.ai). Ücretsiz moddaki yerel taslak da artık bu fiyattan düşer.
- **Hareket testi** varlığın onaylı referans görselinden başlar (H3'e ilk kare olarak verilir, Flux çağrılmaz): sınanan, onaylanan referansın kendisidir.
- **Düzenlenmiş ilk kare** yüklenince varyasyonun motoruyla (H3'te fl2va) prompt, seed, çözünürlük ve süresiyle videoya çevrilir; bedeli yalnızca videodur. Aynı kare yeniden yüklenirse yeniden ödenmez.
- İstek içindeki ücretli çağrının hataları Türkçe yanıtlanır: kilit 409, bütçe 423, sağlayıcının geçici hatası (3 denemeden sonra) 503.

Bilinmesi gerekenler:

- fal yanıtı fatura tutarı döndürmez; harcama `pricing.yaml` tahminidir. `tts_per_1k_chars` ve `llm_per_million_tokens` kalemleri doğrulanmış değildir: ücretli moda geçmeden önce kendi planınıza göre güncelleyin.
- Seedance en az 4 sn üretir ve tohum kabul etmez; 3 sn'lik bir çekim 4 sn faturalanır. MiniMax H3 de en az 4 sn üretir (kesilir), tohum alır. Wan (eski çekimler) 17–161 kare (16 fps) üretir.
- **Ücretli modda kalite kapısı gerçek skorlayıcıyı kullanır** (bkz. *Kalite skoru*). Sahte skor ücretli modda hiçbir koşulda kabul edilmez; gerçek skorlayıcı kullanılamıyorsa (paket ya da model dosyası yok) kademeli üretim para harcanmadan önce durur. Sırada: düşük skorda istemi revize eden düzeltme grafiği, Pinecone ile karakter vektörleri ve iki katmanlı önbellek, kürasyon kayıtlarından projeye özel kalite modeli.
- Ücretli entegrasyonları denemek için ayrıca `pip install -r backend/requirements-live.txt` gerekir. Bunu açık onay olmadan yapmayın; `DRY_RUN=0` ile hiçbir şeyi açık onay olmadan çalıştırmayın.

## Ayarlar

`.env` dosyasından okunur (örnek: `.env.example`). Başlıcaları:

| Ayar | Varsayılan | Anlamı |
|---|---|---|
| `DRY_RUN` | `1` | 1: hiçbir ücretli çağrı yapılmaz |
| `DRY_RUN_DEFECT_RATE` | `0.12` | Bilerek bozuk (siyah/donmuş) klip oranı: kalite kapısını sınamak için |
| `DRY_RUN_SCORE_MEAN` / `_SD` | `0.89` / `0.05` | Sahte CLIP skor dağılımı |
| `DRY_RUN_FAIL_RATE` | `0` | Geçici hata oranı (yeniden deneme testi için) |
| `GEN_CONCURRENCY` | `3` | Bir dalgada eşzamanlı üretim sayısı |
| `PURGE_MODE` | `quarantine` | `quarantine`: elenenler `quarantine/` önekine taşınır; `hard`: silinir |
| `TTS_ENGINE` | `espeak` | `espeak`, `tone` (yapay ton) ya da `elevenlabs` (ücretli; ücretsiz modda espeak kullanılır) |
| `QUALITY_SCORER` | `auto` | `auto`: ücretsiz modda sahte, ücretli modda gerçek skor; `dry`: her zaman sahte (ücretli modda üretim başlamaz); `real`: her zaman gerçek (ücretsiz modda da denenebilir) |
| `SCORE_FACE_WEIGHT` | `0.4` | Birleşik skorda yüz kimliğinin payı (karede yüz görünüyorsa) |
| `PROMPT_TRANSLATION` | `auto` | `auto`: ücretli modda Türkçe tarifler İngilizceye çevrilir; `on`: her zaman; `off`: hiç (senaryoyu İngilizce yazın) |
| `VAST_API_KEY`, `FAL_KEY`, `ELEVENLABS_API_KEY`, `ANTHROPIC_API_KEY` | boş | Ücretli servis anahtarları (panelin Ayarlar sayfasından da girilir); MiniMax H3'ün kilidi vast.ai API anahtarıdır |
| `H3_PLATFORM` | `runpod` | MiniMax H3'ün platformu: `runpod` (Serverless) ya da `vast` (deneysel, kapalı) |
| `RUNPOD_API_KEY`, `RUNPOD_ENDPOINT_ID`, `RUNPOD_NETWORK_VOLUME_ID` | boş | RunPod anahtarı, Serverless uç nokta ve modellerin volume'u |
| `RUNPOD_DATACENTER` / `H3_ALLOW_NORDIC` | `CA-MTL-1` / `false` | Volume'un veri merkezi; Kuzey merkezlerini (EUR-NO-1, EUR-IS-*) izinli listeye ekle |
| `RUNPOD_GPU_TYPES` / `RUNPOD_IDLE_TIMEOUT_S` / `RUNPOD_MIN_BALANCE_USD` | `RTX 5090,RTX PRO 6000` / `60` / `10` | Uç noktanın GPU tipleri, worker'ın boşta bekleme süresi (sn), bakiye eşiği ($) |
| `RUNPOD_GPU_POOLS` | `auto` | **Ayarları uç noktaya uygula**'nın yazdığı GPU havuzları: `auto` (GPU tiplerinden) ya da ör. yalnızca `ADA_32_PRO` (RTX 5090; RTX PRO 6000 havuzu eklenmeden `RUNPOD_WORKERS_MAX` 2-3 worker) |
| `RUNPOD_REVIEW_IDLE_S` | `300` | Storyboard şeridi incelemesinde (son kare işinden sonra 15 dk) geçici boşta bekleme, sn; `0` kapalı |
| `H3_UNET_VARIANT` | `fp8_scaled` | H3 DiT dosyası: `fp8_scaled` (cu128 worker imajı) ya da `int8_convrot` (cu130 torch'lu imaj gerekir); klip meta verisine yazılır |
| `RUNPOD_MAX_PENDING_JOBS` | `6` | Bir projede RunPod'da aynı anda en çok bekleyen/çalışan iş; fazlası yer açılana dek gönderilmez |
| `KEYFRAME_CHARACTER_PROVIDER` | `qwen_edit` | Karakterli çekimin ilk karesi: `qwen_edit` (Qwen-Image-Edit-2511, karakterin onaylı referansından) ya da `qwen` (Qwen-Image, yalnızca metinden; kimlik kayabilir) |
| `QWEN_EDIT_ANGLES` | `off` | Multiple Angles LoRA (kesmede açı değişimi); `on` yalnızca dosya volume'da `models/loras/` altındaysa |
| `QWEN_EDIT_ANGLES_LORA` | `qwen-image-edit-2511-multiple-angles-lora.safetensors` | LoRA dosya adı (fal/Qwen-Image-Edit-2511-Multiple-Angles-LoRA, Apache-2.0) |
| `QWEN_EDIT_ANGLES_STRENGTH` | `0.9` | LoRA gücü (önerilen 0,8-1,0) |
| `QWEN_EDIT_ANGLES_MODE` | `full` | Açı karesinin kalitesi (`full` / `medium` / `lightning`; Lightning ile uyumu ilk gerçek koşuda karşılaştırılacak) |
| `QWEN_EDIT_MODE` | `full` | Karakterli ilk kare kalitesi: `full` 40 adım cfg 4 (~195 sn), `medium` 20 adım cfg 4 (~100 sn), `lightning` 4 adım cfg 1 (~15 sn); proje ayarı `qwen_edit_mode` önce gelir |
| `REFERENCE_FAL_FALLBACK` | `false` | Referans görseli için RunPod'da Qwen-Image yoksa fal.ai Flux'a düş (FLUX.1 [dev] ağırlıkları ticari olmayan lisanslı) |
| `VAST_INSTANCE_ID` | boş | H3 makinesinin vast.ai kimliği (boşta durdurma, otomatik başlatma, konum) |
| `H3_BACKEND` | `comfyui` | `comfyui` ya da `sglang` (deneysel) |
| `H3_ENDPOINT_URL` / `H3_REF_ENDPOINT_URL` | boş | SGLang FL2VA ya da ComfyUI adresi; Ref2VA sunucusu (ComfyUI'de boşsa aynı sunucu) |
| `H3_API_KEY` | boş | Portal belirteci (isteğe bağlı; SSH tünelinde gerekmez) |
| `H3_MODEL` / `H3_MODEL_REVISION` / `H3_STEPS` | `MiniMaxAI/MiniMax-H3` / boş / `9` | Model kimliği, klip meta verisine yazılan sürüm, adım sayısı (sunucudaki kurulumla aynı) |
| `H3_IDLE_STOP_MINUTES` / `H3_AUTO_START` | `10` / `false` | Boşta durdurma süresi (0: kapalı) ve iş gelince durmuş makineyi başlatma |
| `H3_TLS_MODE` | `verify` | Sunucu sertifikası: `verify`, `pin` (ilk bağlantıda parmak izini sabitle; panelden kiralanan makine), `off` |
| `H3_REPLACE_MODE` | `ask` | Yedek makine: `ask` (her zaman sor), `auto` (fiyat tavanı altındaysa sormadan), `off` |
| `H3_MAX_DPH` / `H3_MIN_RAM_GB` / `H3_MIN_DISK_GB` / `H3_MIN_INET_MBPS` | `0.95` / `128` / `200` / `1000` | Yeni ve yedek makine koşulları (RAM en az 96, disk en az 150) |
| `H3_GPU_NAME` / `H3_BOX_IMAGE` / `H3_SETUP_SCRIPT_URL` | boş / `vastai/base-image:@vastai-automatic-tag` / boş | Yedek makinenin GPU'su (boş: eskisininki), imajı ve setup.sh adresi (boş: isteğe gömülür) |
| `H3_IMAGE_MODEL` / `H3_SETUP_TIMEOUT_S` | `qwen-image-2512` / `5400` | setup.sh'nin kurduğu storyboard görsel modeli; kurulumun en uzun süresi |
| `H3_TIMEOUT_S` / `H3_START_TIMEOUT_S` / `H3_COMFY_WORKFLOW_DIR` | `3600` / `900` / `/config/comfy` | Tek işin en uzun süresi, otomatik başlatmada bekleme, ComfyUI iş akışı klasörü (`./comfy-workflows`) |
| `FAL_FLUX_ENDPOINT`, `FAL_WAN_ENDPOINT`, `FAL_SEEDANCE_ENDPOINT`, `FAL_LIPSYNC_ENDPOINT`, `FAL_WAN_V2V_ENDPOINT` | fal.ai model kimlikleri | Ücretli üretimin uç noktaları; `.env.example`'daki değerler varsayılandır |
| `ELEVENLABS_MODEL` / `HERMES_MODEL` | `eleven_multilingual_v2` / `claude-sonnet-5-5` | Seslendirme ve senaryo ayrıştırma modelleri |
| `HERMES_MODE` | `rules` | `rules` (ücretsiz) ya da `llm` (Anthropic) |
| `PROMPT_WRITER_MODEL` | `claude-opus-5-5` | İstem yazıcının modeli (boşsa `HERMES_MODEL`) |
| `PROMPT_WRITER` | `llm` | Sahne istemi: `llm` (Hermes modeli yönetmen tarzı istem yazar; ücretli, anahtar yoksa ya da ücretsiz modda kurallara düşer) ya da `rules` |
| `CONFIG_ENV_FILE` | `/config/.env` (docker-compose) | Panelin yazdığı ayar dosyası; boşsa panelden düzenleme kapalıdır |

Proje ayarları (proje oluştururken verilir; Senaryo › Yoğunluk'tan aday sayıları, çözünürlükler ve kimlik eşiği değiştirilir): `clip_threshold` 0,85 · `identity_threshold` 0,60 (insan karakterde 0-1 yüz kimliği — SFace kosinüsünün aynı kişi eşiği 0,363 çevresinde — bunun altında kalan ya da yüzü hiç görünmeyen aday rafine havuza giremez; kısa kenarı 80 px'ten küçük yüzde kimlik ölçülmez, eleme olmaz) · `look_threshold` 0,80 ve `look_dino_threshold` 0,84 (insan olmayan karakter — robot, hayvan, yaratık —: kare/klip ile referans arasındaki OpenCLIP görüntü benzerliği ve karenin kırpımlarıyla DINOv2 benzerliği; ikisi de geçmeli; yüz aranmaz) · `continuity_threshold` 0,80 · `cut_continuity_threshold` 0,60 · `transition_threshold` 0,80 · `qwen_edit_mode` boş (Ayarlar'daki QWEN_EDIT_MODE; full / medium / lightning) · `excellent_threshold` 0,92 · `min_pass_rate` 0,3 · `probe_n` 10 · `full_n` 50 · `wave_size` 10 · `refined_k` 3 · `max_correction_rounds` 2 · `probe_resolution` 480p · `full_resolution` 720p · `soft_budget_ratio` 0,8 · `max_spend_per_10min` 20 · `loudness_lufs` −23 · `lint_strictness` block (`off`: kontrol yok, `warn`: olay yazılır ama üretilir, `block`: engellenir) · `asset_min_status` locked (ya da `still_approved`, `motion_tested`).

## Depo yapısı

```
docker-compose.yml  Makefile  .env.example
db/init/001_core.sql           ilk şema (PostgreSQL ilk açılışta uygular)
comfy-workflows/               MiniMax H3 ComfyUI iş akışları: i2v, r2v, keyframe (ComfyUI 0.39.0 resmî şablonlarından;
                               exported/: dışa aktarılan hâller ve make_templates.py), panel yazar
frontend/                      yapım stüdyosu paneli: Next.js (App Router) + TypeScript + SWR, statik dışa aktarım
  src/app/                     sayfa, kök düzen, globals.css (tasarım belirteçleri)
  src/components/              AppFrame (üst bant, sayfa çubuğu), SettingsPage (ücretli servisler), HistoryPage (servis geçmişi), ScriptPage, StylePage, AssetsPage, BoardPage, EditPage
                               (havuz, izleyici, adaylar, zaman çizelgesi), Inspector, ComparePage, ColorPage, DeliverPage
  src/lib/                     api (istemci), panel (ortak durum, URL), text, compare ve timeline (saf mantık), media, i18n, types
  tests/                       vitest: metinler, karşılaştırma, zaman çizelgesi, tasarım sözleşmesi
backend/
  pricing.yaml  migrations/    fiyat tablosu, şema değişiklikleri
  app/
    config.py db.py models.py schemas.py storage.py migrate.py main.py
    budget/      killswitch (rezervasyon, dondurma, bekçiler), pricing, history (servis geçmişi)
    core/        hashing, rerender (Stateless Re-render planlayıcısı), state
    hermes/      rules (ayrıştırıcı), ingest (veritabanına yazma), lint (üretim öncesi referans kontrolü)
    engine/      procedural (yerel render), providers, iteration (kademeli üretim),
                 references (kare örnekleme, vektör, yerel taslak), motion_test (hareket testi),
                 storyboard (kare, onay kapısı, PDF), methods (previs rehberi, dış çekim, elle düzeltme),
                 paid (ücretli çağrıların ortak yolu: kilit, rezervasyon, yeniden deneme)
    style/       bible (sürümler, etkin sürüm, etki önizlemesi), composer (prompt bestecisi)
    curation.py  kürasyon kaydı (@curated: her durum değiştiren uç bir olay yazar)
    quality/     anomaly (FFmpeg), gate (skor, rafine havuz), scorer (gerçek skor: OpenCLIP, YuNet + SFace),
                 consistency (renk sıçraması, öneri), metrics (verimlilik)
    post/        audio, mux, grade (LUT ve renk ayarı), pipeline (LIPSYNC, GRADE, MUX), timeline (film ve sekans müziği),
                 export (EDL, OpenTimelineIO, çekim paketi)
    api/         projects, scenes, methods, grade, sequences, storyboard, assets, style, budget, history, export, services, panel,
                 plan, deps, uploads
    worker/      celery_app, tasks, dispatch
    live/        ücretli kilit (guard), runpod_h3 (MiniMax H3, RunPod Serverless), vast_h3, vast_box ve vast_api (deneysel),
                 h3_outputs (çıktı güvencesi), tls_pin, fal_video, elevenlabs, hermes_llm, probe
    core/regions izinli bölgeler (RunPod veri merkezleri, vast.ai ülkeleri)
  requirements-live.txt        yalnızca ücretli entegrasyonlar için ek bağımlılıklar
  requirements-quality.txt     gerçek kalite skorlayıcısı (ücretsiz, yerel modeller; imajda kurulu)
  scripts/smoke.py  scripts/fetch_models.py   duman testi; model dosyalarını indirip doğrulama
  runpod/                      RunPod: setup_volume.sh (modeller Network Volume'a), worker/ (Dockerfile, handler.py), README.md
  vast/setup.sh                MiniMax H3 makinesi kurulumu (vast.ai, deneysel: ComfyUI, H3 int8 modelleri, görsel modeli)
  tests/
```

## Sorun giderme

- **Panel boş ya da "Sunucuya ulaşılamıyor" diyor:** `docker compose ps` ile servisleri, `docker compose logs api worker` ile günlükleri kontrol edin.
- **Sahneler "Engellendi" diyor:** çekimdeki bir varlık henüz kilitli değil. Satırdaki eksik referans bağlantısına tıklayıp varlığı onay akışından geçirin, sonra *Engellenen çekimleri yeniden dene*'ye basın. Referans kontrolünü bir proje için kapatmak isterseniz proje ayarlarında `lint_strictness: "off"` verin.
- **Üretim başlamıyor:** worker çalışıyor mu? Proje dondurulmuş olabilir (bütçe rayındaki uyarı bandı); *Üretime devam et*'e basın.
- **Sıfırdan başlamak:** `make reset` veritabanı ve MinIO dahil **her şeyi siler**; çalıştırmadan önce emin olun.
- **`/panel/` "Panel derlenmemiş" diyor:** imaj panelsiz derlenmiş (ör. `docker build backend` ile, compose'un `frontend` bağlamı olmadan). `docker compose up -d --build` paneli imajın ilk aşamasında derler.
- **Paneli geliştirmek:** `make web-dev` Next.js geliştirme sunucusunu `http://localhost:3000/panel/` adresinde açar ve `/api` isteklerini `HG_API_URL` (varsayılan `http://localhost:8000`) adresine yönlendirir; API Docker'da çalışırken anında yenilenen bir panel elde edilir.
- **Yazı tipi yüklenmiyor:** panel Archivo'yu Google Fonts'tan alır; çevrimdışıyken sistem yazı tipine düşer, işlev etkilenmez.
